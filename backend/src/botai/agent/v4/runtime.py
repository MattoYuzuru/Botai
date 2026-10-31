"""The v4 agent runtime: one loop for every task (D-001, D-021, architecture §4.1).

Cycle: build the conversation → call the provider → if it asked for tools, execute them and
append their results as data → repeat until the model answers or a limit/stop ends the run. Every
step is written to a `TraceStore` (D-014), tokens and dollars are counted (§4.6), and the run
returns a checkpoint (`RunResult.messages`) that can be resumed after a pause, a restart or a
raised limit (§4.5).

The runtime is DB-free: the provider, the trace store, the ask channel and the policy decider are
protocols/callables supplied by the caller. Tool failures are data, not exceptions: an unknown
tool, a schema violation, a timeout or a handler crash become a `ToolResult` the model can react
to. Soft limits are visible counters with an optional warning; a stop happens only when the caller
set `auto_continue=False` (D-009), while `hard_stop_usd` is the emergency stop that always wins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from botai.agent.v4.protocol import (
    AskUser,
    PolicyDecider,
    PolicyDecision,
    PolicyRequest,
    RunControlView,
    ToolContext,
    ToolCost,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    validate_arguments,
)
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.traces import (
    MemoryTraceStore,
    RunStatus,
    RunTrace,
    TraceEvent,
    TraceKind,
    TraceStore,
)
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.core.llm import (
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmToolCall,
    LlmUsage,
    Purpose,
    Tier,
)

log = logging.getLogger("botai.agent.v4.runtime")

Pricing = Callable[[LlmUsage, datetime], float]
Clock = Callable[[], datetime]
Mask = Callable[[str], str]
Monotonic = Callable[[], float]
Emit = Callable[..., Awaitable[None]]

LIMIT_STATUS: dict[str, RunStatus] = {
    "steps": "step_limit",
    "seconds": "time_limit",
    "tokens": "token_limit",
    "usd": "cost_limit",
    "hard_stop_usd": "hard_stop",
}

CONTENT_IN_TRACE_CHARS = 4_000
RESULT_IN_TRACE_CHARS = 4_000


def _carries_task(messages: Sequence[LlmMessage], task: str) -> bool:
    """Whether the checkpoint already contains this exact instruction as a user message."""
    wanted = " ".join(task.split())
    return any(
        message.role == "user" and message.content and " ".join(message.content.split()) == wanted
        for message in messages
    )


def default_pricing(usage: LlmUsage, at: datetime) -> float:
    """USD by the shared tariff table (peak doubling included).

    Unknown models fall back to the cheap DeepSeek row of `botai.usage.service.PRICES`; the
    wiring should inject a provider-specific `Pricing` when Claude/OpenAI tariffs matter. Kept as
    a lazy import so the runtime module stays free of the DB/usage import chain.
    """
    from botai.agent.llm import is_peak
    from botai.usage.service import cost_usd

    return cost_usd(usage, peak=is_peak(at))


class RunLimits(BaseModel):
    """Soft limits of one run (D-009, §4.6): `None` = no limit, a number = a visible counter.

    With `auto_continue=False` crossing a soft limit stops the run with a resumable checkpoint;
    with `auto_continue=True` the run only writes a warning and continues. `hard_stop_usd` is
    never softened: it is the emergency stop against a runaway agent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int | None = Field(default=None, ge=1, le=1_000_000)
    max_seconds: float | None = Field(default=None, gt=0, le=86_400)
    max_tokens: int | None = Field(default=None, ge=1000, le=1_000_000_000)
    max_usd: float | None = Field(default=None, gt=0, le=1000)
    hard_stop_usd: float | None = Field(default=None, gt=0, le=1000)
    auto_continue: bool = False
    max_calls_per_step: int | None = Field(default=16, ge=1, le=256)
    max_parallel_tools: int = Field(default=8, ge=1, le=32)
    tool_result_chars: int = Field(default=20_000, ge=500, le=1_000_000)


class RunTask(BaseModel):
    """Everything the runtime needs to start (or resume) one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default_factory=new_id)
    task: str = Field(min_length=1, max_length=20_000)
    messages: tuple[LlmMessage, ...] = ()
    system: str = ""
    purpose: Purpose = "agent"
    tier: Tier = "fast"
    model: str | None = None
    max_tokens: int = Field(default=4000, ge=1, le=64_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    json_schema: dict[str, Any] | None = None
    workspace: str = "."
    user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """The outcome of one run: answer, counters and a checkpoint for resume (§4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: RunStatus
    content: str = ""
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    usage: tuple[LlmUsage, ...] = ()
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    limit_reached: str | None = None
    error_code: str | None = None
    error_detail: str = ""
    messages: tuple[LlmMessage, ...] = ()
    trace: RunTrace

    @property
    def answered(self) -> bool:
        return self.status == "answer"

    @property
    def can_resume(self) -> bool:
        return self.status in (
            "paused",
            "step_limit",
            "time_limit",
            "token_limit",
            "cost_limit",
            "hard_stop",
        )


class ManualControl:
    """A `RunControlView` a test or the UI can flip (the worker uses its own implementation)."""

    def __init__(self) -> None:
        self._cancelled = False
        self._paused = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return self._paused

    def cancel(self) -> None:
        self._cancelled = True

    def pause(self) -> None:
        self._paused = True

    def unpause(self) -> None:
        self._paused = False


class _NullControl:
    @property
    def cancelled(self) -> bool:
        return False

    @property
    def paused(self) -> bool:
        return False


class _ExecutedCall(BaseModel):
    """One tool call after execution: the result and the text appended to the conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call: LlmToolCall
    result: ToolResult
    text: str
    duration_ms: int = 0


_SOURCE_TAG = re.compile(r"<\s*(/?)\s*source_data", re.IGNORECASE)


def _neutralize_source_tags(text: str) -> str:
    """A tool result can never close the `<source_data>` wrapper and speak as the system (§4.3)."""
    return _SOURCE_TAG.sub(lambda match: "‹" + (match.group(1) or "") + "source_data", text)


def _mask_value(value: Any, mask: Mask) -> Any:
    """Mask every string inside tool data (dicts/lists keep their shape); numbers stay numbers."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [_mask_value(item, mask) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_value(item, mask) for item in value)
    if isinstance(value, dict):
        return {key: _mask_value(item, mask) for key, item in value.items()}
    return value


def mask_result(result: ToolResult, mask: Mask) -> ToolResult:
    """Mask a tool result before it reaches the model or the trace (D-008/D-014, one place).

    Tools are independent processes/files/HTTP clients: a parser can print a profile e-mail, a file
    can contain a password, an API can echo a token. Every handler may mask on its own (the browser
    tools do), but the boundary that actually hands the result to the model is the runtime, so the
    run's masker is applied here to the summary, data, error text and artifact URLs. This keeps a
    resumed run and a trace written later just as masked as the live conversation.
    """
    error = result.error
    masked_error = (
        error.model_copy(
            update={
                "detail": mask(error.detail),
                "hint": mask(error.hint) if error.hint is not None else None,
            }
        )
        if error is not None
        else None
    )
    artifacts = tuple(
        artifact.model_copy(update={"url": mask(artifact.url)})
        if artifact.url is not None
        else artifact
        for artifact in result.artifacts
    )
    return result.model_copy(
        update={
            "summary": mask(result.summary),
            "data": _mask_value(result.data, mask),
            "error": masked_error,
            "artifacts": artifacts,
        }
    )


def _wrap_result(tool_name: str, result: ToolResult, max_chars: int) -> tuple[str, bool]:
    body = _neutralize_source_tags(result.to_model_text())
    truncated = result.truncated or len(body) > max_chars
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n[…обрезано: результат не влез целиком]"
    safe_name = re.sub(r"[^a-z0-9_]", "", tool_name.lower())[:40] or "tool"
    return f'<source_data tool="{safe_name}">\n{body}\n</source_data>', truncated


def _mask_json(value: Any, mask: Mask) -> Any:
    """Mask strings inside a trace payload while keeping its JSON shape (arguments, results)."""
    return _json_safe(_mask_value(value, mask))


def _mask_message(message: LlmMessage, mask: Mask) -> LlmMessage:
    """The checkpoint copy of a message: no primary secret in content or tool-call arguments."""
    tool_calls = [
        call.model_copy(update={"arguments": _mask_value(dict(call.arguments), mask)})
        for call in message.tool_calls
    ]
    return message.model_copy(update={"content": mask(message.content), "tool_calls": tool_calls})


def _json_safe(value: Any) -> Any:
    """Payloads for the trace: JSON-able, no arbitrary Python objects."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _soft_limit(
    limits: RunLimits, *, steps: int, elapsed_s: float, total_tokens: int, cost_usd: float
) -> str | None:
    if limits.max_steps is not None and steps >= limits.max_steps:
        return "steps"
    if limits.max_seconds is not None and elapsed_s >= limits.max_seconds:
        return "seconds"
    if limits.max_tokens is not None and total_tokens >= limits.max_tokens:
        return "tokens"
    if limits.max_usd is not None and cost_usd >= limits.max_usd:
        return "usd"
    return None


class AgentRuntime:
    """Executes `RunTask`s. Construct once per worker/process; `run` is safe to call concurrently."""

    def __init__(
        self,
        *,
        control: RunControlView | None = None,
        approve: PolicyDecider | None = None,
        pricing: Pricing | None = None,
        clock: Clock | None = None,
        monotonic: Monotonic = time.monotonic,
        mask: Mask | None = None,
    ) -> None:
        self._control: RunControlView = control or _NullControl()
        self._approve = approve
        self._pricing: Pricing = pricing or default_pricing
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        # The run's secret masker (secret broker): applied to every tool result before the model
        # and the trace see it. Identity when the caller has no secrets (tests, read-only runs).
        self._mask: Mask = mask or (lambda text: text)

    async def run(
        self,
        task: RunTask,
        provider: Provider,
        tools: Sequence[ToolHandler] | ToolRegistry,
        *,
        trace_store: TraceStore | None = None,
        limits: RunLimits | None = None,
        ask: AskUser | None = None,
    ) -> RunResult:
        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        store: TraceStore = trace_store or MemoryTraceStore()
        limits = limits or RunLimits()
        events: list[TraceEvent] = []
        seq = 0
        usage_list: list[LlmUsage] = []
        started_at = self._clock()
        started_mono = self._monotonic()
        steps = 0
        tool_calls = 0
        total_tokens = 0
        cost_usd = 0.0
        content = ""
        status: RunStatus = "answer"
        limit_reached: str | None = None
        error_code: str | None = None
        error_detail = ""
        model = task.model or ""
        warned: set[str] = set()
        system = task.system.strip()
        convo: list[LlmMessage] = []
        if system:
            convo.append(LlmMessage(role="system", content=system))
        convo.extend(task.messages)
        # The task instruction is the first thing the model must see; a resumed run passes its
        # checkpoint in `messages` (which already ends with the instruction) and must not get it
        # twice, or the model would see the same request as two separate asks.
        if not _carries_task(task.messages, task.task):
            convo.append(LlmMessage(role="user", content=task.task))
        context = ToolContext(
            run_id=task.run_id,
            task=task.task,
            workspace=Path(task.workspace),
            user_id=task.user_id,
            metadata=dict(task.metadata),
            trace_store=store,
            ask=ask,
        )

        async def emit(
            kind: TraceKind,
            *,
            step: int = 0,
            duration_ms: int = 0,
            payload: dict[str, Any] | None = None,
        ) -> None:
            nonlocal seq
            event = TraceEvent(
                run_id=task.run_id,
                seq=seq,
                at=self._clock(),
                kind=kind,
                step=step,
                duration_ms=duration_ms,
                payload=payload or {},
            )
            seq += 1
            events.append(event)
            await store.append(event)

        await emit(
            "run_started",
            payload={
                "task": task.task,
                "provider": provider.name,
                "model": model,
                "purpose": task.purpose,
                "limits": limits.model_dump(mode="json", exclude_none=True),
                "tool_count": len(registry),
                "metadata": _json_safe(task.metadata),
            },
        )

        try:
            while True:
                if self._control.cancelled:
                    status = "cancelled"
                    await emit("cancel", step=steps, payload={"reason": "control"})
                    break
                if self._control.paused:
                    status = "paused"
                    await emit("pause", step=steps, payload={"reason": "control"})
                    break
                elapsed_s = self._monotonic() - started_mono
                if limits.hard_stop_usd is not None and cost_usd >= limits.hard_stop_usd:
                    status = "hard_stop"
                    limit_reached = "hard_stop_usd"
                    await emit(
                        "limit",
                        step=steps,
                        payload=_limit_payload(
                            "hard_stop_usd", steps, elapsed_s, total_tokens, cost_usd
                        ),
                    )
                    break
                soft = _soft_limit(
                    limits,
                    steps=steps,
                    elapsed_s=elapsed_s,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                )
                if soft is not None:
                    if not limits.auto_continue:
                        status = LIMIT_STATUS[soft]
                        limit_reached = soft
                        await emit(
                            "limit",
                            step=steps,
                            payload=_limit_payload(soft, steps, elapsed_s, total_tokens, cost_usd),
                        )
                        break
                    if soft not in warned:
                        warned.add(soft)
                        await emit(
                            "limit",
                            step=steps,
                            payload={
                                **_limit_payload(soft, steps, elapsed_s, total_tokens, cost_usd),
                                "auto_continue": True,
                            },
                        )

                request = LlmRequest(
                    purpose=task.purpose,
                    tier=task.tier,
                    model=task.model,
                    messages=list(convo),
                    tools=registry.llm_tools(),
                    json_schema=task.json_schema,
                    max_tokens=task.max_tokens,
                    temperature=task.temperature,
                )
                call_started = self._monotonic()
                try:
                    response = await provider.complete(request)
                except BotaiError as exc:
                    status = "error"
                    error_code = exc.code.value
                    error_detail = exc.detail
                    await emit(
                        "error",
                        step=steps,
                        payload={"code": error_code, "detail": error_detail, "retryable": False},
                    )
                    break
                except Exception:
                    log.exception("v4 provider failed for run %s", task.run_id)
                    status = "error"
                    error_code = "llm_error"
                    error_detail = "Провайдер модели завершился ошибкой."
                    await emit("error", step=steps, payload={"code": error_code})
                    break
                call_ms = int((self._monotonic() - call_started) * 1000)
                steps += 1
                call_cost = self._account(usage_list, response, call_ms)
                total_tokens = sum(
                    item.input_cache_hit + item.input_cache_miss + item.output
                    for item in usage_list
                )
                cost_usd += call_cost
                model = response.usage.model or model
                await emit(
                    "model_call",
                    step=steps,
                    duration_ms=call_ms,
                    payload=_model_payload(response, call_cost, self._mask),
                )
                if response.tool_calls:
                    convo.append(
                        LlmMessage(
                            role="assistant",
                            content=response.content,
                            reasoning_content=response.reasoning_content,
                            tool_calls=response.tool_calls,
                        )
                    )
                    content = response.content
                    executed = await self._execute_calls(
                        response=response,
                        registry=registry,
                        context=context,
                        step=steps,
                        limits=limits,
                        emit=emit,
                    )
                    for item in executed:
                        convo.append(
                            LlmMessage(role="tool", tool_call_id=item.call.id, content=item.text)
                        )
                    tool_calls += len(executed)
                    continue
                convo.append(
                    LlmMessage(
                        role="assistant",
                        content=response.content,
                        reasoning_content=response.reasoning_content,
                    )
                )
                content = response.content
                status = "answer"
                break
        except asyncio.CancelledError:
            status = "cancelled"
            await emit("cancel", step=steps, payload={"reason": "cancelled"})
            raise
        except Exception:
            log.exception("v4 run %s failed unexpectedly", task.run_id)
            status = "error"
            error_code = "internal_error"
            error_detail = "Внутренняя ошибка агентского цикла."
            await emit("error", step=steps, payload={"code": error_code})

        finished_at = self._clock()
        await emit(
            "run_finished",
            step=steps,
            payload={
                "status": status,
                "steps": steps,
                "tool_calls": tool_calls,
                "total_tokens": total_tokens,
                "cost_usd": round(cost_usd, 6),
                "error_code": error_code,
            },
        )
        trace = RunTrace(
            run_id=task.run_id,
            task=task.task,
            provider=provider.name,
            model=model,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            steps=steps,
            total_tokens=total_tokens,
            cost_usd=round(cost_usd, 6),
            events=tuple(events),
        )

        # The checkpoint is persisted/resumed and shown in traces: mask it even though the live
        # conversation keeps the model's own turns exactly as the provider produced them.
        checkpoint = tuple(
            _mask_message(message, self._mask) for message in convo if message.role != "system"
        )
        log.info(
            "v4 run %s status=%s steps=%d tools=%d tokens=%d cost_usd=%.6f",
            task.run_id,
            status,
            steps,
            tool_calls,
            total_tokens,
            cost_usd,
        )
        return RunResult(
            run_id=task.run_id,
            status=status,
            content=content,
            steps=steps,
            tool_calls=tool_calls,
            usage=tuple(usage_list),
            total_tokens=total_tokens,
            cost_usd=round(cost_usd, 6),
            limit_reached=limit_reached,
            error_code=error_code,
            error_detail=error_detail,
            messages=checkpoint,
            trace=trace,
        )

    # -- internals -----------------------------------------------------------------------------

    def _account(self, usage_list: list[LlmUsage], response: LlmResponse, call_ms: int) -> float:
        usage = response.usage
        if usage.latency_ms == 0:
            usage = usage.model_copy(update={"latency_ms": call_ms})
        usage_list.append(usage)
        return self._pricing(usage, self._clock())

    async def _execute_calls(
        self,
        *,
        response: LlmResponse,
        registry: ToolRegistry,
        context: ToolContext,
        step: int,
        limits: RunLimits,
        emit: Emit,
    ) -> list[_ExecutedCall]:
        calls = list(response.tool_calls)
        tools = [registry.get(call.name) for call in calls]
        parallel = len(calls) > 1 and all(
            tool is not None and tool.spec.parallel_safe() for tool in tools
        )
        if parallel:
            semaphore = asyncio.Semaphore(limits.max_parallel_tools)

            async def guarded(index: int) -> _ExecutedCall:
                async with semaphore:
                    return await self._execute_one(
                        call=calls[index],
                        tool=tools[index],
                        context=context,
                        step=step,
                        limits=limits,
                        index=index,
                        emit=emit,
                    )

            return list(await asyncio.gather(*(guarded(index) for index in range(len(calls)))))
        executed: list[_ExecutedCall] = []
        for index, call in enumerate(calls):
            executed.append(
                await self._execute_one(
                    call=call,
                    tool=tools[index],
                    context=context,
                    step=step,
                    limits=limits,
                    index=index,
                    emit=emit,
                )
            )
        return executed

    async def _execute_one(
        self,
        *,
        call: LlmToolCall,
        tool: ToolHandler | None,
        context: ToolContext,
        step: int,
        limits: RunLimits,
        index: int,
        emit: Emit,
    ) -> _ExecutedCall:
        started = self._monotonic()
        if limits.max_calls_per_step is not None and index >= limits.max_calls_per_step:
            result = ToolResult.failure(
                "too_many_calls",
                f"За один шаг разрешено не больше {limits.max_calls_per_step} вызовов.",
                hint="Повторите нужный вызов в следующем шаге.",
            )
            await emit(
                "tool_call",
                step=step,
                payload={
                    "call_id": call.id,
                    "tool": call.name,
                    "arguments": _mask_json(call.arguments, self._mask),
                    "skipped": "too_many_calls",
                },
            )
            result = mask_result(result, self._mask)
            text, _ = _wrap_result(call.name, result, 2_000)
            await emit(
                "tool_result",
                step=step,
                payload=_tool_result_payload(call, result, len(text)),
            )
            return _ExecutedCall(call=call, result=result, text=text, duration_ms=0)

        await emit(
            "tool_call",
            step=step,
            payload={
                "call_id": call.id,
                "tool": call.name,
                "arguments": _mask_json(call.arguments, self._mask),
                "policy": tool.spec.policy if tool else None,
            },
        )
        result = await self._run_tool(call, tool, context, step)
        duration_ms = int((self._monotonic() - started) * 1000)
        result = result.model_copy(update={"cost": ToolCost(ms=duration_ms)})
        result = mask_result(result, self._mask)
        max_chars = _effective_chars(tool, limits)
        text, truncated = _wrap_result(call.name, result, max_chars)
        if truncated and not result.truncated:
            result = result.model_copy(update={"truncated": True})
        await emit(
            "tool_result",
            step=step,
            duration_ms=duration_ms,
            payload=_tool_result_payload(call, result, len(text)),
        )
        return _ExecutedCall(call=call, result=result, text=text, duration_ms=duration_ms)

    async def _run_tool(
        self,
        call: LlmToolCall,
        tool: ToolHandler | None,
        context: ToolContext,
        step: int,
    ) -> ToolResult:
        if tool is None:
            return ToolResult.failure(
                "unknown_tool",
                f"Инструмента «{call.name}» нет.",
                hint="Используйте инструменты из списка; вызов не выполнен.",
            )
        if "__invalid_arguments__" in call.arguments:
            return ToolResult.failure(
                "invalid_arguments",
                "Аргументы вызова не разобраны как JSON.",
                hint="Повторите вызов с корректным JSON-объектом аргументов.",
            )
        problems = validate_arguments(tool.spec, call.arguments)
        if problems:
            return ToolResult.failure(
                "invalid_arguments",
                "Аргументы не проходят JSON-Schema: " + "; ".join(problems),
            )
        decision = await self._decide_policy(call, tool.spec, context.run_id)
        if not decision.allowed:
            return ToolResult.failure(
                "policy_denied",
                decision.reason or "Действие запрещено политикой.",
                hint="Предложите владельцу разрешить действие или выберите другой способ.",
            )
        call_context = context.model_copy(update={"step": step})
        try:
            async with asyncio.timeout(tool.spec.timeout_s):
                return await tool.run(call.arguments, call_context)
        except TimeoutError:
            return ToolResult.failure(
                "timeout",
                f"Инструмент не ответил за {tool.spec.timeout_s:g} с.",
                retryable=True,
            )
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        except Exception:
            log.exception("v4 tool %s failed", call.name)
            return ToolResult.failure("internal_error", "Инструмент завершился ошибкой.")

    async def _decide_policy(
        self, call: LlmToolCall, spec: ToolSpec, run_id: str
    ) -> PolicyDecision:
        if spec.policy == "read" or self._approve is None:
            return PolicyDecision()
        request = PolicyRequest(
            run_id=run_id,
            tool=call.name,
            policy=spec.policy,
            arguments=_json_safe(call.arguments),
        )
        try:
            return await self._approve(request)
        except Exception:
            log.exception("v4 policy decider failed")
            return PolicyDecision(allowed=False, reason="Политика действий недоступна.")


def _effective_chars(tool: ToolHandler | None, limits: RunLimits) -> int:
    if tool is None:
        return min(2_000, limits.tool_result_chars)
    return min(tool.spec.max_result_chars, limits.tool_result_chars)


def _limit_payload(
    limit: str, steps: int, elapsed_s: float, total_tokens: int, cost_usd: float
) -> dict[str, Any]:
    return {
        "limit": limit,
        "steps": steps,
        "elapsed_s": round(elapsed_s, 3),
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
    }


def _model_payload(response: LlmResponse, cost_usd: float, mask: Mask) -> dict[str, Any]:
    usage = response.usage
    return {
        "model": usage.model,
        "input_cache_hit": usage.input_cache_hit,
        "input_cache_miss": usage.input_cache_miss,
        "output": usage.output,
        "reasoning": usage.reasoning,
        "latency_ms": usage.latency_ms,
        "cost_usd": round(cost_usd, 6),
        "content": mask(response.content[:CONTENT_IN_TRACE_CHARS]),
        "reasoning_present": response.reasoning_content is not None,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": _mask_json(call.arguments, mask)}
            for call in response.tool_calls
        ],
    }


def _tool_result_payload(call: LlmToolCall, result: ToolResult, text_chars: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "call_id": call.id,
        "tool": call.name,
        "ok": result.ok,
        "summary": result.summary[:1_000],
        "truncated": result.truncated,
        "text_chars": text_chars,
        "duration_ms": result.cost.ms,
        "error": result.error.code if result.error else None,
    }
    wire = _json_safe(result.to_wire())
    serialized = json.dumps(wire, ensure_ascii=False, default=str)
    if len(serialized) <= RESULT_IN_TRACE_CHARS:
        payload["result"] = wire
    else:
        payload["result_truncated_in_trace"] = True
    return payload


__all__ = [
    "LIMIT_STATUS",
    "AgentRuntime",
    "Clock",
    "Emit",
    "ManualControl",
    "Monotonic",
    "Pricing",
    "RunLimits",
    "RunResult",
    "RunTask",
    "default_pricing",
]
