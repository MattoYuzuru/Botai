"""PgJobQueue: the `JobQueue` protocol over the `jobs` table (NFR-006, NFR-012)."""

from __future__ import annotations

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
        now = self._clock.now()
        due = run_after or now
        async with self._db() as session:
            if dedupe and connection_id:
                # Serialises racing enqueues of the same (kind, connection); released on commit.
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
                    await session.commit()
                    return existing.id

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
            await session.commit()
            return job.id
