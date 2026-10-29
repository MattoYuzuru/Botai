"""PgJobQueue: the `JobQueue` protocol over the `jobs` table (NFR-006, NFR-012)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.clock import Clock, SystemClock
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db.models import Job
from botai.jobs.events import IN_FLIGHT, append_event, status_data
from botai.jobs.notify import CHANNEL_JOBS

SOURCE_KINDS = ("connect", "learn", "repair", "sync")


async def lock_sources(session: AsyncSession, connection_ids: Iterable[str]) -> None:
    """Acquire source groups before existing Connection/Job row locks, in stable order.

    Callers that bind or continue a connection use this before locking its rows. New,
    uncommitted connections are not visible to competing producers yet.
    """
    with session.no_autoflush:
        for connection_id in sorted(set(connection_ids)):
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(func.hashtextextended(f"source:{connection_id}", 0))
                )
            )


class PgJobQueue:
    def __init__(self, db: async_sessionmaker[AsyncSession], clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    async def enqueue(
        self,
        kind: JobKind,
        *,
        user_id: str,
        connection_id: str | None = None,
        thread_id: str | None = None,
        payload: dict[str, Any] | None = None,
        run_after: datetime | None = None,
        dedupe: bool = True,
    ) -> str:
        """Returns the job id. With `dedupe`, a job of the same kind for the same connection that
        is queued, running or waiting for the student is returned instead of a second one. A queued
        one that is only waiting for a later `run_after` (a retry) is pulled forward."""
        async with self._db() as session:
            job = await enqueue_in_session(
                session,
                kind,
                user_id=user_id,
                connection_id=connection_id,
                thread_id=thread_id,
                payload=payload,
                run_after=run_after,
                dedupe=dedupe,
                now=self._clock.now(),
            )
            await session.commit()
            return job.id


async def enqueue_in_session(
    session: AsyncSession,
    kind: JobKind,
    *,
    user_id: str,
    now: datetime,
    connection_id: str | None = None,
    thread_id: str | None = None,
    payload: dict[str, Any] | None = None,
    run_after: datetime | None = None,
    dedupe: bool = True,
) -> Job:
    """The same queue semantics in the caller's transaction; NOTIFY is delivered on its commit."""
    due = run_after or now
    if connection_id and kind.value in SOURCE_KINDS:
        # Scheduler and explicit source actions share this lock before any Job row lock.
        # A requested human login must not race another source browser into existence.
        await lock_sources(session, [connection_id])
        incoming_manual = kind == JobKind.CONNECT and (payload or {}).get("manual_login") is True
        query = select(Job).where(
            Job.user_id == user_id,
            Job.connection_id == connection_id,
            Job.kind.in_(SOURCE_KINDS),
            Job.state.in_(sorted(IN_FLIGHT)),
        )
        if not incoming_manual:
            query = query.where(
                Job.kind == JobKind.CONNECT.value,
                Job.payload["manual_login"].as_boolean().is_(True),
            )
        existing_source = await session.scalar(
            query.order_by(Job.created_at, Job.id).limit(1).with_for_update()
        )
        if existing_source is not None:
            return existing_source
    if dedupe and connection_id:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{kind.value}:{connection_id}", 0)
                )
            )
        )
        existing = await session.scalar(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.kind == kind.value,
                Job.connection_id == connection_id,
                Job.state.in_(sorted(IN_FLIGHT)),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
            .with_for_update()
        )
        if existing is not None:
            if existing.state == "queued" and existing.run_after > due:
                existing.run_after = due
                await session.execute(select(func.pg_notify(CHANNEL_JOBS, existing.id)))
            return existing
    job = Job(
        id=new_id(),
        user_id=user_id,
        kind=kind.value,
        state="queued",
        connection_id=connection_id,
        thread_id=thread_id,
        payload=payload or {},
        run_after=due,
    )
    session.add(job)
    await session.flush()
    await append_event(session, job.id, "status", status_data(job, now))
    await session.execute(select(func.pg_notify(CHANNEL_JOBS, job.id)))
    return job
