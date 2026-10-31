"""Run one v4 runtime task inside a job: wiring, ask channel, phases, error mapping.

Every v4 job kind (`AGENT`, `CONNECT`, `REPAIR` fallback) does the same mechanical work: build the
wiring behind `agent_engine=v4`, map the job's question channel onto `ask_user`/`secret_request`,
turn the trace-store run into a job result and release the browser/executor. Keeping it in one
place means a fix (limits, masking, cancellation, result shape) reaches every kind and no handler
invents a second version of it.

Phases: `CONNECT`/`REPAIR` must show what is happening — login or research — not a permanent
loader (docs/v4/09-prod-login-incidents.md §2). The runtime has no notion of a task phase, so the
handlers derive it from the tools the model actually called: a `browser_*`/`secret_*` call means the
agent is working on the entry, a `source_probe`/`source_read_live` call means research, a
`source_sync` call means synchronization. The phase only moves forward and is written to the job
status/events, so the UI can show it without a new contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from botai.agent.v4.factory import (
    AgentWiring,
    AgentWiringError,
    build_agent_wiring,
    run_limits_from_settings,
)
from botai.agent.v4.protocol import (
    AskAnswer,
    AskQuestion,
    AskUser,
    ToolContext,
    ToolHandler,
    ToolResult,
)
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.runtime import RunResult, RunTask
from botai.contracts.agent_ui import ChoiceBlock, ChoiceOption, InputBlock, TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobContext
from botai.core.llm import LlmMessage, Purpose, Tier
from botai.core.services import Services

MAX_ANSWER_CHARS = 8000

# Which evidence moves a task to which phase, and how the phase is named in the job status.
TOOL_PHASES: dict[str, str] = {
    "secret_request": "login",
    "secret_use": "login",
    "browser_open": "login",
    "browser_navigate": "login",
    "browser_click": "login",
    "browser_type": "login",
    "browser_press": "login",
    "browser_select": "login",
    "browser_wait": "login",
    "browser_session": "login",
    "source_create": "research",
    "source_probe": "research",
    "source_read_live": "research",
    "source_update_playbook": "research",
    "source_repair": "research",
    "source_sync": "sync",
    "rag_ingest": "sync",
}
PHASE_ORDER: tuple[str, ...] = ("login", "research", "sync")
PHASE_LABELS: dict[str, str] = {
    "login": "вход",
    "research": "исследование",
    "sync": "синхронизация",
}


@dataclass(slots=True)
class JobRun:
    """The run and what the handler needs after `aclose` (the workspace survives the wiring)."""

    run_id: str
    result: RunResult
    workspace: Path


class PhaseReporter:
    """Forward-only phase of a job, derived from the tools the agent calls (job status/events).

    `steps` says how concrete the wording is: the tool's phase is reported once per transition,
    not on every call, so the status does not flicker while the agent works.
    """

    def __init__(self, ctx: JobContext, *, prefix: str) -> None:
        self._ctx = ctx
        self._prefix = prefix
        self._rank = -1

    async def observe(self, tool_name: str) -> None:
        phase = TOOL_PHASES.get(tool_name)
        if phase is None:
            return
        rank = PHASE_ORDER.index(phase)
        if rank <= self._rank:
            return
        self._rank = rank
        await self._ctx.status(f"{self._prefix}: {PHASE_LABELS[phase]}")

    def wrap(self, tools: Sequence[ToolHandler]) -> list[ToolHandler]:
        return [_PhaseTool(tool, self) for tool in tools]


class _PhaseTool:
    """`ToolHandler` decorator: reports the phase before the real handler runs."""

    def __init__(self, inner: ToolHandler, phases: PhaseReporter) -> None:
        self._inner = inner
        self._phases = phases
        self.spec = inner.spec

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        await self._phases.observe(self.spec.name)
        return await self._inner.run(arguments, context)


async def run_v4_job(
    ctx: JobContext,
    services: Services,
    *,
    task: str,
    system: str,
    provider: Provider | None = None,
    run_id: str | None = None,
    messages: Sequence[LlmMessage] = (),
    purpose: Purpose = "agent",
    tier: Tier = "fast",
    workspace_root: str | Path | None = None,
    with_browser: bool = True,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
    phase_prefix: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> JobRun:
    """Build the wiring, run the task and close everything; a config refusal is a `BotaiError`."""
    active_run_id = run_id or new_id()
    wiring: AgentWiring | None = None
    try:
        try:
            wiring = await build_agent_wiring(
                services,
                user_id=ctx.user_id,
                run_id=active_run_id,
                job_id=ctx.job_id,
                workspace_root=str(workspace_root) if workspace_root is not None else None,
                provider=provider,
                with_browser=with_browser,
                http_transport=http_transport,
                artifact_root=artifact_root,
                source_cookies=source_cookies,
            )
        except AgentWiringError as exc:
            # A configuration refusal (prod local-executor, missing sources DB, broken RAG) is not a
            # model failure: report it as a clear configuration error, never silently run without.
            raise BotaiError(
                ErrorCode.NOT_IMPLEMENTED,
                f"Агент v4 не может запуститься в этой конфигурации: {exc}",
            ) from None
        await ctx.status("Агент v4: собираю окружение")
        tools: Sequence[ToolHandler] = wiring.tools
        if phase_prefix is not None:
            reporter = PhaseReporter(ctx, prefix=phase_prefix)
            tools = reporter.wrap(tools)
        await ctx.status("Агент v4: выполняю задачу")
        result = await wiring.runtime.run(
            RunTask(
                run_id=active_run_id,
                task=task,
                messages=tuple(messages),
                system=system,
                purpose=purpose,
                tier=tier,
                workspace=str(wiring.workspace.root),
                user_id=ctx.user_id,
                metadata={"job_id": ctx.job_id, **(metadata or {})},
            ),
            wiring.provider,
            tools,
            trace_store=wiring.trace_store,
            limits=run_limits_from_settings(services.settings),
            ask=job_ask_channel(ctx, services),
        )
        workspace = wiring.workspace.root
    finally:
        if wiring is not None:
            await wiring.aclose()
    return JobRun(run_id=result.run_id, result=result, workspace=workspace)


def job_ask_channel(ctx: JobContext, services: Services) -> AskUser:
    """Map the runtime's AskQuestion onto the job's UI blocks (secret answers stay out of text)."""

    async def ask(question: AskQuestion) -> AskAnswer:
        block_id = new_id()
        at = services.clock.now()
        try:
            if question.input_kind == "secret":
                secret = await ctx.ask_secret(
                    InputBlock(
                        id=block_id,
                        at=at,
                        question=question.question[:500],
                        input_kind="password",
                        placeholder=question.placeholder,
                        required=False,
                        max_length=2000,
                        secret=True,
                    )
                )
                return AskAnswer(text=secret.get_secret_value())
            if question.input_kind == "choice" and len(question.options) >= 2:
                options = [
                    ChoiceOption(value=str(index), label=option[:200])
                    for index, option in enumerate(question.options[:20])
                ]
                answer = await ctx.ask(
                    ChoiceBlock(
                        id=block_id,
                        at=at,
                        question=question.question[:500],
                        mode="single",
                        required=False,
                        options=options,
                    )
                )
                return AskAnswer(choice=_chosen_index(answer.values, question.options))
            answer = await ctx.ask(
                InputBlock(
                    id=block_id,
                    at=at,
                    question=question.question[:500],
                    input_kind="text",
                    placeholder=question.placeholder,
                    required=False,
                    max_length=2000,
                )
            )
            return AskAnswer(text=_answer_text(answer.values))
        except JobCancelled:
            return AskAnswer(cancelled=True)

    return ask


async def emit_answer(ctx: JobContext, services: Services, result: RunResult) -> None:
    """The run's final answer as a normal chat block (with its own trace still in the DB)."""
    if not result.content:
        return
    await ctx.emit(
        TextBlock(
            id=new_id(),
            at=services.clock.now(),
            markdown=result.content[:MAX_ANSWER_CHARS],
        )
    )


def run_result_dict(run: JobRun, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The job result of a finished v4 run: status, counters, trace id, workspace (secret-free)."""
    result = run.result
    body: dict[str, Any] = {
        "status": result.status,
        "run_id": result.run_id,
        "content": result.content[:MAX_ANSWER_CHARS],
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "total_tokens": result.total_tokens,
        "cost_usd": result.cost_usd,
        "limit_reached": result.limit_reached,
        "workspace": str(run.workspace),
    }
    if extra:
        body.update(extra)
    return body


def stage_of(result: RunResult, *, prefix: str = "Агент v4") -> str:
    """A short honest final stage for the job status (not a fake percentage)."""
    if result.status == "answer":
        return f"{prefix}: готово"
    if result.limit_reached:
        return f"{prefix}: остановлен лимитом {result.limit_reached}"
    return f"{prefix}: {result.status}"


def error_of(result: RunResult) -> BotaiError:
    """The runtime's failure as the job's failure: the same code, a human detail."""
    code = ErrorCode.INTERNAL
    if result.error_code:
        try:
            code = ErrorCode(result.error_code)
        except ValueError:
            code = ErrorCode.INTERNAL
    return BotaiError(code, result.error_detail or "Агент v4 завершился ошибкой.")


def _answer_text(values: object) -> str:
    if isinstance(values, str):
        return values
    if isinstance(values, list) and values:
        return str(values[0])
    if isinstance(values, bool):
        return "да" if values else "нет"
    return ""


def _chosen_index(values: object, options: tuple[str, ...]) -> int | None:
    raw: object = values[0] if isinstance(values, list) and values else values
    if isinstance(raw, str):
        if raw.isdigit() and 0 <= int(raw) < len(options):
            return int(raw)
        if raw in options:
            return list(options).index(raw)
    return None


__all__ = [
    "JobRun",
    "PhaseReporter",
    "emit_answer",
    "error_of",
    "job_ask_channel",
    "run_result_dict",
    "run_v4_job",
    "stage_of",
]
