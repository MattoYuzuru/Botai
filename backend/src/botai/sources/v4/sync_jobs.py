"""Queueing a v4 source sync: the additive async counterpart of the inline REST sync.

`POST /sources/{id}/sync-jobs` puts a `SYNC` job in the worker queue instead of replaying the
playbook inside the API process — the same task the scheduler and the v3 «Перечитать» button
already use. Two paths:

* a source created from a v3 connection: the job points at that connection, so the dispatch finds
  the source the old way (and keeps the v3 `connections` view coherent); the payload carries
  `source_id` for honesty and for the run history;
* a source created by the agent or the REST API alone: there is no connection row, the job carries
  only `{source_id}`, and the v4 dispatch replays it by source id
  (`agent/v4/dispatch.py::_sync_source_by_id`).

Dedupe: one source at a time. A connected source reuses the queue's per-connection dedupe; a
connection-less source is deduplicated by an explicit in-flight `SYNC` lookup on the payload (the
queue dedupes by connection only). The race between two simultaneous requests of an unconnected
source can produce two queued jobs; the second one is cheap and the runner is idempotent, so it is
not worth a lock.

Scheduling (`Source.next_sync_at`, the interval and the failure backoff) is owned by the source
runner/scheduler, not here: a queued sync is the same run as any other.

The old `POST /sources/{id}/sync` keeps its 200 `RunReportOut`: the UI stays on it until it moves
to the job + SSE contract.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.db.models import Job, Source
from botai.jobs.events import IN_FLIGHT

LINK_KEY = "connection_id"


def linked_connection(source: Source) -> str | None:
    """The v3 connection a v4 source was created from (the dispatch links them in its config)."""
    linked = source.config.get(LINK_KEY)
    return linked if isinstance(linked, str) and linked else None


def job_clauses(source: Source) -> list[ColumnElement[bool]]:
    """Jobs of this source: by the dispatch's connection link or by the payload `source_id`."""
    clauses: list[ColumnElement[bool]] = [Job.payload["source_id"].as_string() == source.id]
    linked = linked_connection(source)
    if linked is not None:
        clauses.append(Job.connection_id == linked)
    return clauses


async def find_in_flight(session: AsyncSession, *, user_id: str, source: Source) -> str | None:
    """The id of a queued/running/waiting task of this source, if one is in flight."""
    return await session.scalar(
        select(Job.id)
        .where(
            Job.user_id == user_id,
            Job.state.in_(sorted(IN_FLIGHT)),
            or_(*job_clauses(source)),
        )
        .order_by(Job.created_at, Job.id)
        .limit(1)
    )


async def _running_sync(
    sm: async_sessionmaker[AsyncSession], user_id: str, source: Source
) -> str | None:
    """A `SYNC` of this source already queued or running (a connection-less source has no queue
    dedupe key, so it is looked up explicitly)."""
    async with sm() as session:
        return await session.scalar(
            select(Job.id)
            .where(
                Job.user_id == user_id,
                Job.kind == JobKind.SYNC.value,
                Job.state.in_(sorted(IN_FLIGHT)),
                or_(*job_clauses(source)),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )


async def _job_state(sm: async_sessionmaker[AsyncSession], user_id: str, job_id: str) -> str:
    async with sm() as session:
        state = await session.scalar(
            select(Job.state).where(Job.id == job_id, Job.user_id == user_id)
        )
    return str(state or "queued")


def _refuse(source: Source) -> None:
    if source.status == "disabled":
        raise BotaiError(
            ErrorCode.CONFLICT, "Источник выключен: включите его, чтобы синхронизировать"
        )
    if source.playbook_id is None:
        raise BotaiError(
            ErrorCode.CONFLICT, "У источника ещё нет playbook: сначала подключите источник"
        )


async def enqueue_sync(services: Services, *, user_id: str, source: Source) -> tuple[str, str]:
    """Put the source's sync in the queue; returns `(job_id, job state)`.

    A connected source goes through the normal `SYNC` job of its connection (the queue dedupes it
    per source); a source without a connection gets a payload-only job that the v4 dispatch runs by
    `source_id`.
    """
    _refuse(source)
    payload = {"source_id": source.id}
    linked = linked_connection(source)
    if linked is not None:
        job_id = await services.queue.enqueue(
            JobKind.SYNC,
            user_id=user_id,
            connection_id=linked,
            payload=payload,
            dedupe=True,
        )
    else:
        existing = await _running_sync(services.db, user_id, source)
        job_id = existing or await services.queue.enqueue(
            JobKind.SYNC,
            user_id=user_id,
            payload=payload,
            dedupe=False,
        )
    return job_id, await _job_state(services.db, user_id, job_id)


__all__ = ["enqueue_sync", "find_in_flight", "job_clauses", "linked_connection"]
