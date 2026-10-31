"""HTTP API of the v4 agent runs: start a run, read its status and its trace (task-19).

`POST /api/v1/agent/runs` takes a natural-language task and enqueues the existing `AGENT` job (the
same queue, blocks, SSE events and result handling as every other task): no second execution path.
The run id is generated before the job starts and returned immediately, so the client can read the
trace while the run is still going.

The router is registered always (a stable OpenAPI contract), but it is disabled unless
`BOTAI_AGENT_ENGINE=v4`, like the sources-v4 REST API: on the v3 engine every endpoint answers 404.
The DB trace is read through the owner only (`agent_runs.user_id`); events are returned after an
event sequence number so a client can page a long run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.factory import enqueue_agent_run
from botai.api.deps import ServicesDep, SettingsDep
from botai.auth.deps import AuthUser
from botai.core.api import ApiModel
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import AgentRun, AgentRunEvent, Connection
from botai.db.session import DbSession
from botai.spaces import repo as spaces_repo
from botai.spaces.conversations import owned_thread

router = APIRouter(prefix="/api/v1", tags=["agent-v4"])

MAX_TASK_CHARS = 20_000
MAX_EVENTS_LIMIT = 500
DEFAULT_EVENTS_LIMIT = 100


def require_agent_v4(settings: SettingsDep) -> None:
    """The v4 agent API exists only on the v4 engine; on v3 it is not found, not half-working."""
    if settings.agent_engine != "v4":
        raise BotaiError(
            ErrorCode.NOT_FOUND,
            "Агент v4 выключен: включите BOTAI_AGENT_ENGINE=v4",
        )


V4 = Depends(require_agent_v4)


class AgentRunCreate(ApiModel):
    """A natural-language task; the thread/space/connection only give it a place and provenance."""

    task: str = Field(min_length=1, max_length=MAX_TASK_CHARS)
    thread_id: str | None = Field(default=None, max_length=26)
    space_id: str | None = Field(default=None, max_length=26)
    connection_id: str | None = Field(default=None, max_length=26)


class AgentRunAccepted(ApiModel):
    """The queued run: the job to follow with the existing job SSE and the trace to read here."""

    job_id: str
    run_id: str
    status: Literal["queued"] = "queued"


class AgentRunOut(ApiModel):
    """The run's durable row (`agent_runs`), not a live in-memory state."""

    run_id: str
    job_id: str | None
    task: str
    provider: str
    model: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    steps: int
    total_tokens: int
    cost_usd: float
    event_count: int


class AgentRunEventOut(ApiModel):
    """One trace step: model call, tool call/result, limit, pause, error or finish (D-014)."""

    seq: int
    kind: str
    step: int
    duration_ms: int
    at: datetime
    payload: dict[str, Any]


class AgentRunEventsOut(ApiModel):
    """A page of the trace; `has_more` tells the caller to repeat with `after_seq=last seq`."""

    run_id: str
    events: list[AgentRunEventOut]
    has_more: bool


@router.post("/agent/runs", response_model=AgentRunAccepted, status_code=202, dependencies=[V4])
async def create_run(
    body: AgentRunCreate,
    user: AuthUser,
    session: DbSession,
    services: ServicesDep,
) -> AgentRunAccepted:
    """Enqueue one `AGENT` run; the response carries the ids needed to read its trace."""
    task = body.task.strip()
    if not task:
        raise BotaiError(ErrorCode.VALIDATION, "Задаче нужен непустой текст")
    if body.thread_id is not None:
        await owned_thread(session, user.id, body.thread_id)
    if body.space_id is not None:
        await spaces_repo.get_space(session, user.id, body.space_id)
    if body.connection_id is not None:
        connection = await session.scalar(
            select(Connection).where(
                Connection.id == body.connection_id, Connection.user_id == user.id
            )
        )
        if connection is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
        if body.space_id is not None and connection.space_id != body.space_id:
            raise BotaiError(ErrorCode.CONFLICT, "Подключение из другого пространства")
    await session.rollback()  # the queue works in its own transaction; nothing to hold here

    run_id = new_id()
    job_id = await enqueue_agent_run(
        services,
        user_id=user.id,
        task=task,
        connection_id=body.connection_id,
        thread_id=body.thread_id,
        run_id=run_id,
        payload={
            key: value
            for key, value in {
                "_space_id": body.space_id,
                "_connection_id": body.connection_id,
            }.items()
            if value is not None
        },
    )
    return AgentRunAccepted(job_id=job_id, run_id=run_id)


@router.get("/agent/runs/{run_id}", response_model=AgentRunOut, dependencies=[V4])
async def get_run(run_id: str, user: AuthUser, session: DbSession) -> AgentRunOut:
    """The run row of this owner; another user's run is not found, never leaked."""
    row = await _owned_run(session, user.id, run_id)
    return AgentRunOut(
        run_id=row.run_id,
        job_id=row.job_id,
        task=row.task,
        provider=row.provider,
        model=row.model,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        steps=row.steps,
        total_tokens=row.total_tokens,
        cost_usd=row.cost_usd,
        event_count=row.event_count,
    )


@router.get(
    "/agent/runs/{run_id}/events",
    response_model=AgentRunEventsOut,
    dependencies=[V4],
)
async def get_run_events(
    run_id: str,
    user: AuthUser,
    session: DbSession,
    after_seq: Annotated[int, Query(ge=0, alias="afterSeq")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_LIMIT)] = DEFAULT_EVENTS_LIMIT,
) -> AgentRunEventsOut:
    """Trace steps after `afterSeq`, oldest first; a replayed sequence is never written twice."""
    await _owned_run(session, user.id, run_id)
    rows = list(
        await session.scalars(
            select(AgentRunEvent)
            .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq >= after_seq)
            .order_by(AgentRunEvent.seq)
            .limit(limit + 1)
        )
    )
    has_more = len(rows) > limit
    return AgentRunEventsOut(
        run_id=run_id,
        events=[
            AgentRunEventOut(
                seq=row.seq,
                kind=row.kind,
                step=row.step,
                duration_ms=row.duration_ms,
                at=row.at,
                payload=dict(row.payload),
            )
            for row in rows[:limit]
        ],
        has_more=has_more,
    )


async def _owned_run(session: AsyncSession, user_id: str, run_id: str) -> AgentRun:
    row = await session.get(AgentRun, run_id)
    if row is None or row.user_id != user_id:
        raise BotaiError(ErrorCode.NOT_FOUND, "Прогон не найден")
    return row


__all__ = [
    "AgentRunAccepted",
    "AgentRunCreate",
    "AgentRunEventOut",
    "AgentRunEventsOut",
    "AgentRunOut",
    "require_agent_v4",
    "router",
]
