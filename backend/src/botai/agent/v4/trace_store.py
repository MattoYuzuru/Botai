"""DB-backed `TraceStore` for the v4 runtime: `agent_runs` + `agent_run_events` (D-014, §4.8).

The runtime appends one `TraceEvent` per step; this adapter writes the run row (status, counters)
and the steps to Postgres, so the owner, the API/UI and the agent's own `trace_read` tool see the
same trace after a restart. It is deliberately one new adapter next to `MemoryTraceStore` and
implements the same protocol; the runtime does not know which store it got.

Secrets: the adapter stores exactly what the runtime gives it. Tool handlers are responsible for
masking primary secrets in their results (the secret broker does that); the adapter never writes
job payloads, prompts, cookies or raw auth material by itself.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.traces import (
    RunInfo,
    RunStatus,
    RunTrace,
    TraceEvent,
    TraceKind,
    status_of,
)
from botai.db.models import AgentRun, AgentRunEvent


def _run_status(value: str) -> RunStatus:
    return status_of({"status": value}, "running")


class DbTraceStore:
    """`TraceStore` over an async session factory, scoped to one user (and optionally one job)."""

    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        *,
        user_id: str,
        job_id: str | None = None,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._job_id = job_id

    async def append(self, event: TraceEvent) -> None:
        payload = event.payload
        async with self._db() as session, session.begin():
            if event.kind == "run_started":
                await session.execute(
                    pg_insert(AgentRun)
                    .values(
                        run_id=event.run_id,
                        user_id=self._user_id,
                        job_id=self._job_id,
                        task=_text(payload.get("task")),
                        provider=_text(payload.get("provider")),
                        model=_text(payload.get("model")),
                        status=status_of(payload, "running"),
                        started_at=event.at,
                        updated_at=event.at,
                        event_count=0,
                    )
                    .on_conflict_do_update(
                        index_elements=[AgentRun.run_id],
                        set_={
                            "task": _text(payload.get("task")),
                            "provider": _text(payload.get("provider")),
                            "model": _text(payload.get("model")),
                            "status": status_of(payload, "running"),
                            "started_at": event.at,
                            "updated_at": event.at,
                        },
                    )
                )
            else:
                # A late event (e.g. after an interrupted run) still gets a run row, so the trace
                # is never written without its parent.
                await session.execute(
                    pg_insert(AgentRun)
                    .values(
                        run_id=event.run_id,
                        user_id=self._user_id,
                        job_id=self._job_id,
                        status="running",
                        started_at=event.at,
                        updated_at=event.at,
                        event_count=0,
                    )
                    .on_conflict_do_nothing(index_elements=[AgentRun.run_id])
                )
            inserted = await session.scalar(
                pg_insert(AgentRunEvent)
                .values(
                    run_id=event.run_id,
                    seq=event.seq,
                    kind=event.kind,
                    step=event.step,
                    duration_ms=event.duration_ms,
                    at=event.at,
                    payload=_json_safe(payload),
                )
                .on_conflict_do_nothing(index_elements=[AgentRunEvent.run_id, AgentRunEvent.seq])
                .returning(AgentRunEvent.id)
            )
            if inserted is not None:
                await session.execute(
                    update(AgentRun)
                    .where(AgentRun.run_id == event.run_id)
                    .values(event_count=AgentRun.event_count + 1, updated_at=event.at)
                )
            if event.kind == "run_finished":
                await session.execute(
                    update(AgentRun)
                    .where(AgentRun.run_id == event.run_id)
                    .values(
                        status=status_of(payload, "running"),
                        finished_at=event.at,
                        updated_at=event.at,
                        steps=_int(payload.get("steps")),
                        total_tokens=_int(payload.get("total_tokens")),
                        cost_usd=_float(payload.get("cost_usd")),
                    )
                )

    async def read(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[TraceEvent]:
        async with self._db() as session:
            rows = (
                await session.scalars(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq >= after_seq)
                    .order_by(AgentRunEvent.seq)
                    .limit(max(limit, 0))
                )
            ).all()
        return [_event(row) for row in rows]

    async def get_run(self, run_id: str) -> RunTrace | None:
        async with self._db() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.user_id != self._user_id:
                return None
            rows = (
                await session.scalars(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id)
                    .order_by(AgentRunEvent.seq)
                )
            ).all()
        return RunTrace(
            run_id=run.run_id,
            task=run.task,
            provider=run.provider,
            model=run.model,
            status=_run_status(run.status),
            started_at=run.started_at,
            finished_at=run.finished_at,
            steps=run.steps,
            total_tokens=run.total_tokens,
            cost_usd=run.cost_usd,
            events=tuple(_event(row) for row in rows),
        )

    async def list_runs(self, *, limit: int = 20) -> list[RunInfo]:
        async with self._db() as session:
            rows = (
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.user_id == self._user_id)
                    .order_by(
                        AgentRun.started_at.desc().nullslast(),
                        AgentRun.run_id.desc(),
                    )
                    .limit(max(limit, 0))
                )
            ).all()
            counts = dict(
                (
                    await session.execute(
                        select(AgentRunEvent.run_id, func.count())
                        .where(AgentRunEvent.run_id.in_([row.run_id for row in rows]))
                        .group_by(AgentRunEvent.run_id)
                    )
                ).all()
            )
        return [
            RunInfo(
                run_id=row.run_id,
                task=row.task,
                provider=row.provider,
                model=row.model,
                status=_run_status(row.status),
                started_at=row.started_at,
                finished_at=row.finished_at,
                steps=row.steps,
                total_tokens=row.total_tokens,
                cost_usd=row.cost_usd,
                event_count=int(counts.get(row.run_id, row.event_count)),
            )
            for row in rows
        ]


def _event(row: AgentRunEvent) -> TraceEvent:
    return TraceEvent(
        run_id=row.run_id,
        seq=row.seq,
        at=row.at,
        kind=cast(TraceKind, row.kind),  # only TraceEvent writes this column
        step=row.step,
        duration_ms=row.duration_ms,
        payload=dict(row.payload),
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _json_safe(value: Any) -> Any:
    """JSONB cannot store arbitrary objects; traces are stored as JSON data."""
    import json

    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return {"truncated_in_trace": True}
    return value


__all__ = ["DbTraceStore"]
