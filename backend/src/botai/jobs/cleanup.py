"""Short authoritative cancellation transactions, preserving the last successful snapshot."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Connection, Job
from botai.jobs.queue import SOURCE_KINDS, lock_sources


async def lock_job_and_connection(
    session: AsyncSession, user_id: str, job_id: str
) -> tuple[Job | None, Connection | None]:
    """Start a fresh mutation transaction in Connection -> Job order.

    Binding may change between the unlocked lookup and the row locks. Retry before
    any mutation instead of acquiring a newly discovered Connection after the Job.
    """
    while True:
        candidate = await session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            return None, None
        connection_id = candidate.connection_id
        connection = None
        if connection_id is not None:
            await lock_sources(session, [connection_id])
            connection = await session.scalar(
                select(Connection)
                .where(Connection.id == connection_id, Connection.user_id == user_id)
                .with_for_update()
            )
        job = await session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or job.connection_id == connection_id:
            return job, connection
        await session.rollback()


async def restore_source_after_stop(
    session: AsyncSession, job: Job, connection: Connection | None
) -> None:
    """Only a committed terminal transition may restore its unowned transient source state."""
    if (
        job.kind not in SOURCE_KINDS
        or connection is None
        or job.connection_id != connection.id
        or job.user_id != connection.user_id
        or connection.status not in {"learning", "syncing", "repairing"}
    ):
        return
    another = await session.scalar(
        select(Job.id)
        .where(
            Job.id != job.id,
            Job.user_id == job.user_id,
            Job.connection_id == connection.id,
            Job.kind.in_(SOURCE_KINDS),
            Job.state.in_(("running", "waiting_user")),
        )
        .limit(1)
    )
    if another is None:
        configured = connection.adapter is not None or connection.spec_id is not None
        connection.status = "stale" if configured else "draft"
