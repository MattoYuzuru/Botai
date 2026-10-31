"""Agent traces of the v4 runtime: first-class data, readable by the agent and the owner (D-014).

`TraceEvent` is one step (model call, tool call, result, limit, pause, finish). `RunTrace` is the
whole run with its counters. `TraceStore` is the storage protocol; `MemoryTraceStore` is the
in-memory implementation used by tests and by short-lived runs, a DB adapter follows in phase 2
without changing the runtime (docs/v4/02-architecture.md §4.8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

TraceKind = Literal[
    "run_started",
    "model_call",
    "tool_call",
    "tool_result",
    "limit",
    "pause",
    "cancel",
    "error",
    "note",
    "run_finished",
]

RunStatus = Literal[
    "running",
    "answer",
    "paused",
    "cancelled",
    "step_limit",
    "time_limit",
    "token_limit",
    "cost_limit",
    "hard_stop",
    "error",
]

RUN_STATUSES: frozenset[str] = frozenset(
    {
        "running",
        "answer",
        "paused",
        "cancelled",
        "step_limit",
        "time_limit",
        "token_limit",
        "cost_limit",
        "hard_stop",
        "error",
    }
)


class TraceEvent(BaseModel):
    """One recorded step. `seq` is stable and monotonic inside one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    at: datetime
    kind: TraceKind
    step: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunInfo(BaseModel):
    """Cheap summary for `trace_list`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    task: str = ""
    provider: str = ""
    model: str = ""
    status: RunStatus = "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    event_count: int = Field(default=0, ge=0)


class RunTrace(BaseModel):
    """Everything the runtime recorded for one run (D-014, §4.8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    task: str = ""
    provider: str = ""
    model: str = ""
    status: RunStatus = "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    events: tuple[TraceEvent, ...] = ()


@runtime_checkable
class TraceStore(Protocol):
    """Storage of traces. The runtime only appends; reading is for `trace_read` and the UI."""

    async def append(self, event: TraceEvent) -> None: ...

    async def read(
        self, run_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[TraceEvent]: ...

    async def get_run(self, run_id: str) -> RunTrace | None: ...

    async def list_runs(self, *, limit: int = 20) -> list[RunInfo]: ...


def status_of(payload: dict[str, Any], default: RunStatus = "running") -> RunStatus:
    """Read a run status out of an event payload without trusting its type."""
    raw = payload.get("status")
    if isinstance(raw, str) and raw in RUN_STATUSES:
        return raw  # type: ignore[return-value]  # checked against RUN_STATUSES
    return default


class MemoryTraceStore:
    """In-memory `TraceStore`: deterministic for tests, no DB (the runtime stays DB-free)."""

    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = {}
        self._info: dict[str, RunInfo] = {}

    async def append(self, event: TraceEvent) -> None:
        self._events.setdefault(event.run_id, []).append(event)
        self._info[event.run_id] = self._updated_info(event)

    async def read(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[TraceEvent]:
        events = self._events.get(run_id, [])
        return [event for event in events if event.seq >= after_seq][: max(limit, 0)]

    async def get_run(self, run_id: str) -> RunTrace | None:
        info = self._info.get(run_id)
        if info is None:
            return None
        events = tuple(sorted(self._events.get(run_id, []), key=lambda event: event.seq))
        return RunTrace(
            run_id=info.run_id,
            task=info.task,
            provider=info.provider,
            model=info.model,
            status=info.status,
            started_at=info.started_at,
            finished_at=info.finished_at,
            steps=info.steps,
            total_tokens=info.total_tokens,
            cost_usd=info.cost_usd,
            events=events,
        )

    async def list_runs(self, *, limit: int = 20) -> list[RunInfo]:
        runs = sorted(
            self._info.values(),
            key=lambda info: (info.started_at is None, info.started_at, info.run_id),
            reverse=True,
        )
        return runs[: max(limit, 0)]

    def _updated_info(self, event: TraceEvent) -> RunInfo:
        current = self._info.get(event.run_id) or RunInfo(run_id=event.run_id)
        update: dict[str, Any] = {"event_count": current.event_count + 1}
        payload = event.payload
        if event.kind == "run_started":
            update.update(
                task=_text(payload.get("task")),
                provider=_text(payload.get("provider")),
                model=_text(payload.get("model")),
                started_at=event.at,
                status=status_of(payload, "running"),
            )
        elif event.kind == "model_call":
            update["steps"] = current.steps + 1
        elif event.kind == "run_finished":
            update.update(
                status=status_of(payload, current.status),
                finished_at=event.at,
                steps=_int(payload.get("steps"), current.steps),
                total_tokens=_int(payload.get("total_tokens"), current.total_tokens),
                cost_usd=_float(payload.get("cost_usd"), current.cost_usd),
            )
        return current.model_copy(update=update)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) else default


def _float(value: Any, default: float) -> float:
    if isinstance(value, int | float):
        return float(value)
    return default


__all__ = [
    "RUN_STATUSES",
    "MemoryTraceStore",
    "RunInfo",
    "RunStatus",
    "RunTrace",
    "TraceEvent",
    "TraceKind",
    "TraceStore",
    "status_of",
]
