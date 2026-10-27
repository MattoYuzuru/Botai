"""Owner: W5. Agent harness (docs/architecture/agent-harness.md, ADR-0007). Keep the signatures.

Learning (W3) does not need the tool loop: it calls `services.llm.complete` with `purpose="learn"`
and a JSON schema. The provider resolves the profile (model, thinking, effort) from the purpose
when the request leaves them unset.

One run = a stable prefix (system.md + the purpose's skills + tool schemas in a fixed order, no
dates) followed by the caller's variable part, then a loop "model -> tools -> model" bounded by
the purpose's budget. Tool results are data: neutralised, secret-scrubbed, stripped of
instruction-like sentences, truncated and wrapped in `<source_data>`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from botai.agent.llm import is_peak, usage_scope
from botai.agent.prompts import build_system_text
from botai.agent.safety import InjectionFinding, mask_injections, scrub_secrets, wrap_source_data
from botai.agent.tokens import estimate_messages, estimate_tokens, estimate_tools
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobContext
from botai.core.llm import (
    LlmMessage,
    LlmRequest,
    LlmTool,
    LlmToolCall,
    LlmUsage,
    Purpose,
)
from botai.core.services import Services
from botai.db.models import AuditLog
from botai.usage import service as usage_service

log = logging.getLogger("botai.agent.harness")

ToolFn = Callable[[dict[str, Any]], Awaitable[str]]

TOOL_TIMEOUT_S = 20.0
MAX_CALLS_PER_STEP = 8
WRAP_UP_NOTE = (
    "[система] Лимит шагов исчерпан. Больше инструменты не вызывайте: ответьте по уже "
    "полученным данным или честно скажите, чего не хватает."
)


@dataclass(frozen=True, slots=True)
class Budget:
    """Limits of one run (agent-harness.md §2). Token counts are measured from API usage."""

    max_steps: int
    max_total_tokens: int  # input + output over all steps
    max_input_tokens: int | None  # estimate of one request's size
    max_output_tokens: int  # answer tokens per call (reasoning headroom is the provider's)
    tool_result_chars: int  # one tool result is cut to this


BUDGETS: dict[str, Budget] = {
    "classify": Budget(1, 20_000, 8_000, 300, 4_000),
    "summarize": Budget(1, 80_000, 40_000, 1_500, 4_000),
    "chat": Budget(6, 250_000, 32_000, 3_000, 8_000),
    "extract": Budget(3, 300_000, None, 4_000, 20_000),
    "repair": Budget(20, 500_000, None, 4_000, 20_000),
    "learn": Budget(40, 1_500_000, None, 4_000, 20_000),
    "bench": Budget(6, 250_000, 32_000, 3_000, 8_000),
}


@dataclass(slots=True)
class HarnessTool:
    spec: LlmTool
    run: ToolFn  # returns data for the model; wrapped as <source_data> by the harness
    stage: str | None = None  # short line for the student while it runs ("Ищу в расписании")


@dataclass(slots=True)
class StepTrace:
    step: int
    model: str
    tool_names: list[str]
    input_miss: int
    input_hit: int
    output: int
    reasoning: int
    latency_ms: int
    cache_share: float


@dataclass(slots=True)
class ToolRecord:
    name: str
    args: dict[str, Any]
    result_chars: int
    result_tokens: int
    truncated: bool = False
    error: bool = False
    repeated: bool = False


@dataclass(slots=True)
class HarnessResult:
    content: str
    steps: int
    usage: list[LlmUsage] = field(default_factory=list)
    stopped: str = "answer"  # answer | budget | quota | error
    error_code: str | None = None
    error_detail: str = ""  # human text of the provider error, safe to show
    prefix_version: str = ""
    trace: list[StepTrace] = field(default_factory=list)
    tool_calls: list[ToolRecord] = field(default_factory=list)
    findings: list[InjectionFinding] = field(default_factory=list)
    blocked_calls: int = 0  # calls to tools that do not exist for this purpose

    @property
    def total_tokens(self) -> int:
        return sum(u.input_cache_hit + u.input_cache_miss + u.output for u in self.usage)

    @property
    def cache_share(self) -> float:
        hit = sum(u.input_cache_hit for u in self.usage)
        total = hit + sum(u.input_cache_miss for u in self.usage)
        return hit / total if total else 0.0


@dataclass(frozen=True, slots=True)
class Prefix:
    system: LlmMessage
    tools: list[LlmTool]
    version: str


def build_prefix(skill: str, tools: Sequence[HarnessTool]) -> Prefix:
    """Stable part of every request of a purpose: same bytes for the same skill and tool set."""
    specs = sorted((t.spec for t in tools), key=lambda s: s.name)
    system = LlmMessage(role="system", content=build_system_text(skill))
    canonical = json.dumps(
        [system.content, [s.model_dump(mode="json") for s in specs]],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return Prefix(system, specs, hashlib.sha256(canonical.encode()).hexdigest()[:12])


def _scrub_messages(messages: Sequence[LlmMessage], *, keep_reasoning: bool) -> list[LlmMessage]:
    out: list[LlmMessage] = []
    for m in messages:
        update: dict[str, Any] = {}
        if m.role != "system" and m.content:
            update["content"] = scrub_secrets(m.content, typed_by_user=m.role == "user")
        if not keep_reasoning and m.reasoning_content is not None:
            update["reasoning_content"] = None
        out.append(m.model_copy(update=update) if update else m)
    return out


async def _emit_stage(
    ctx: JobContext | None, services: Services, block_id: str, stage: str, state: str
) -> None:
    if ctx is None:
        return
    from botai.contracts.agent_ui import ThinkingBlock

    await ctx.emit(
        ThinkingBlock(
            id=block_id,
            at=services.clock.now(),
            stage=stage[:120],
            state=state,  # type: ignore[arg-type]
        )
    )


async def _audit_injection(
    services: Services,
    user_id: str,
    job_id: str | None,
    tool: str,
    findings: Sequence[InjectionFinding],
    blocked_calls: int,
) -> None:
    detail = {
        "tool": tool,
        "job_id": job_id,
        "patterns": sorted({f.pattern for f in findings}),
        "digests": [f.digest for f in findings][:10],
        "blocked_calls": blocked_calls,
    }
    try:
        async with services.db() as session:
            session.add(
                AuditLog(user_id=user_id, kind=ErrorCode.INJECTION_BLOCKED.value, detail=detail)
            )
            await session.commit()
    except Exception:
        log.exception("failed to write injection_blocked audit record")


def _canonical(args: dict[str, Any]) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Run:
    """State of one `run()`; keeps the module-level function short and testable."""

    def __init__(
        self,
        purpose: Purpose,
        budget: Budget,
        prefix: Prefix,
        tools: dict[str, HarnessTool],
        ctx: JobContext | None,
        services: Services,
        user_id: str,
        job_id: str | None,
    ) -> None:
        self.purpose, self.budget, self.prefix, self.tools = purpose, budget, prefix, tools
        self.ctx, self.services, self.user_id, self.job_id = ctx, services, user_id, job_id
        self.result = HarnessResult(content="", steps=0, prefix_version=prefix.version)
        self._seen: dict[tuple[str, str], str] = {}

    async def call_tool(self, call: LlmToolCall) -> str:
        """Run one tool call and return the wrapped text for the `tool` message."""
        res = self.result
        tool = self.tools.get(call.name)
        if tool is None:
            res.blocked_calls += 1
            res.tool_calls.append(ToolRecord(call.name, {}, 0, 0, error=True))
            return wrap_source_data("system", f"Инструмента «{call.name}» нет.", max_chars=300)[0]
        if "__invalid_arguments__" in call.arguments:
            res.tool_calls.append(ToolRecord(call.name, {}, 0, 0, error=True))
            return wrap_source_data(
                call.name, "Аргументы вызова не разобраны как JSON. Повторите вызов.", max_chars=300
            )[0]

        key = (call.name, _canonical(call.arguments))
        if key in self._seen:
            res.tool_calls.append(ToolRecord(call.name, call.arguments, 0, 0, repeated=True))
            return wrap_source_data(
                call.name,
                "Этот вызов с теми же аргументами уже выполнен выше; результат тот же. "
                "Используйте его.",
                max_chars=300,
            )[0]

        block_id = new_id()
        await _emit_stage(
            self.ctx, self.services, block_id, tool.stage or "Запрашиваю данные", "running"
        )
        error = False
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_S):
                raw = await tool.run(call.arguments)
        except BotaiError as exc:
            raw, error = f"Не удалось получить данные: {exc.detail}", True
        except TimeoutError:
            raw, error = "Источник не ответил вовремя.", True
        except Exception:
            log.exception("tool %s failed", call.name)
            raw, error = "Инструмент завершился ошибкой.", True
        await _emit_stage(
            self.ctx,
            self.services,
            block_id,
            tool.stage or "Запрашиваю данные",
            "failed" if error else "ok",
        )

        clean, findings = mask_injections(scrub_secrets(raw))
        if findings:
            res.findings.extend(findings)
            await _audit_injection(
                self.services, self.user_id, self.job_id, call.name, findings, res.blocked_calls
            )
        wrapped, truncated = wrap_source_data(
            call.name, clean, max_chars=self.budget.tool_result_chars
        )
        res.tool_calls.append(
            ToolRecord(
                call.name, call.arguments, len(raw), estimate_tokens(wrapped), truncated, error
            )
        )
        self._seen[key] = wrapped
        return wrapped


async def run(
    purpose: Purpose,
    *,
    skill: str,
    messages: Sequence[LlmMessage],
    tools: Sequence[HarnessTool],
    ctx: JobContext | None,
    services: Services,
    user_id: str,
    job_id: str | None = None,
) -> HarnessResult:
    budget = BUDGETS.get(purpose) or BUDGETS["bench"]
    prefix = build_prefix(skill, tools)
    state = _Run(
        purpose, budget, prefix, {t.spec.name: t for t in tools}, ctx, services, user_id, job_id
    )
    result = state.result
    with_tools = bool(tools)
    convo: list[LlmMessage] = [prefix.system, *_scrub_messages(messages, keep_reasoning=with_tools)]
    prefix_tokens = estimate_tools(prefix.tools)

    with usage_scope(user_id, job_id):
        for step in range(budget.max_steps):
            if result.total_tokens >= budget.max_total_tokens:
                result.stopped = "budget"
                break
            if (
                budget.max_input_tokens is not None
                and estimate_messages(convo) + prefix_tokens > budget.max_input_tokens
            ):
                result.stopped = "budget"
                break
            if with_tools and step == budget.max_steps - 1 and step > 0:
                convo.append(LlmMessage(role="user", content=WRAP_UP_NOTE))
            try:
                response = await services.llm.complete(
                    LlmRequest(
                        purpose=purpose,
                        messages=convo,
                        tools=prefix.tools,
                        max_tokens=budget.max_output_tokens,
                    )
                )
            except BotaiError as exc:
                result.stopped = "quota" if exc.code == ErrorCode.QUOTA_EXCEEDED else "error"
                result.error_code, result.error_detail = exc.code.value, exc.detail
                break
            result.steps += 1
            result.usage.append(response.usage)
            u = response.usage
            denom = u.input_cache_hit + u.input_cache_miss
            result.trace.append(
                StepTrace(
                    step=step + 1,
                    model=u.model,
                    tool_names=[c.name for c in response.tool_calls],
                    input_miss=u.input_cache_miss,
                    input_hit=u.input_cache_hit,
                    output=u.output,
                    reasoning=u.reasoning,
                    latency_ms=u.latency_ms,
                    cache_share=u.input_cache_hit / denom if denom else 0.0,
                )
            )
            if services.settings.llm_debug_traces and response.reasoning_content:
                log.debug("step %d reasoning: %s", step + 1, response.reasoning_content)

            if not response.tool_calls:
                result.content = response.content
                result.stopped = "answer"
                break

            convo.append(
                LlmMessage(
                    role="assistant",
                    content=response.content,
                    reasoning_content=response.reasoning_content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls[:MAX_CALLS_PER_STEP]:
                text = await state.call_tool(call)
                convo.append(LlmMessage(role="tool", tool_call_id=call.id, content=text))
            for call in response.tool_calls[MAX_CALLS_PER_STEP:]:
                convo.append(
                    LlmMessage(
                        role="tool",
                        tool_call_id=call.id,
                        content="Вызов пропущен: слишком много за шаг.",
                    )
                )
            result.content = response.content  # kept if the budget ends the loop right here
            result.stopped = "budget"  # overwritten by the next iteration's outcome

    _log_summary(result, purpose, services, user_id)
    return result


def _log_summary(result: HarnessResult, purpose: str, services: Services, user_id: str) -> None:
    peak = is_peak(services.clock.now())
    cost = sum(usage_service.cost_usd(u, peak=peak) for u in result.usage)
    log.info(
        "agent run purpose=%s prefix=%s steps=%d stopped=%s tokens=%d cache_share=%.2f "
        "tools=%d findings=%d cost_usd=%.6f",
        purpose,
        result.prefix_version,
        result.steps,
        result.stopped,
        result.total_tokens,
        result.cache_share,
        len(result.tool_calls),
        len(result.findings),
        cost,
    )
    if log.isEnabledFor(logging.DEBUG):
        # Names and sizes only: never tool results, prompts or (without llm_debug_traces) reasoning.
        log.debug(
            "agent trace %s",
            json.dumps(
                {
                    "purpose": purpose,
                    "user": user_id[-6:],
                    "steps": [asdict(s) for s in result.trace],
                    "tools": [
                        {"name": t.name, "chars": t.result_chars, "truncated": t.truncated}
                        for t in result.tool_calls
                    ],
                },
                ensure_ascii=False,
            ),
        )


__all__ = [
    "BUDGETS",
    "Budget",
    "HarnessResult",
    "HarnessTool",
    "Prefix",
    "StepTrace",
    "ToolFn",
    "ToolRecord",
    "build_prefix",
    "run",
]
