"""The event log of a job (contracts/events.md §1): what the SSE endpoint replays.

Events are append-only rows with a per-job monotonic `seq` (the SSE `id`). The buffer is bounded:
at most 500 events and 2 MB per job, oldest first out, and it is dropped 10 minutes after the job
ends. Nothing secret is ever written here: producers pass blocks and stages, never answers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Job, JobAnswer, JobEvent
from botai.jobs.notify import CHANNEL_EVENTS

MAX_EVENTS = 500
MAX_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
RETENTION_MINUTES = 10

TERMINAL = frozenset({"done", "failed", "cancelled"})
IN_FLIGHT = frozenset({"queued", "running", "waiting_user"})


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def status_data(job: Job, now: datetime) -> dict[str, Any]:
    started = job.started_at
    elapsed = max(0, int((now - started).total_seconds() * 1000)) if started else 0
    return {
        "state": job.state,
        "stage": job.stage,
        "startedAt": iso(started),
        "elapsedMs": elapsed,
    }


def error_event(
    job: Job, code: str, detail: str, status: int, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """problem+json for the `error` event: code, job id and source, never page contents.
    `extra` carries machine-readable fields of the error (a quota error's `resetsAt`)."""
    body: dict[str, Any] = {
        **(extra or {}),
        "type": f"https://botai.mnema.app/errors/{code}",
        "title": code,
        "status": status,
        "code": code,
        "detail": detail,
        "jobId": job.id,
    }
    if job.connection_id:
        body["connectionId"] = job.connection_id
    return body


async def append_event(session: AsyncSession, job_id: str, type_: str, data: dict[str, Any]) -> int:
    """Appends one event in the caller's transaction and wakes streams on commit. Returns its seq."""
    payload = json.dumps(data, ensure_ascii=False)
    if len(payload.encode()) > MAX_EVENT_BYTES:
        raise ValueError(f"job event of {len(payload)} bytes is over the {MAX_EVENT_BYTES} limit")
    # Writers of one job are serialised, so `seq` stays gap-free and unique under concurrency.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(job_id, 0))))
    last = await session.scalar(
        select(func.coalesce(func.max(JobEvent.seq), 0)).where(JobEvent.job_id == job_id)
    )
    seq = int(last or 0) + 1
    session.add(JobEvent(job_id=job_id, seq=seq, type=type_, data=data))
    await session.flush()
    await _trim(session, job_id)
    await session.execute(select(func.pg_notify(CHANNEL_EVENTS, job_id)))
    return seq


async def _trim(session: AsyncSession, job_id: str) -> None:
    """Keeps the newest events within the count and byte budget."""
    count, total = (
        await session.execute(
            select(
                func.count(), func.coalesce(func.sum(func.pg_column_size(JobEvent.data)), 0)
            ).where(JobEvent.job_id == job_id)
        )
    ).one()
    if count <= MAX_EVENTS and total <= MAX_BYTES:
        return
    rows = (
        await session.execute(
            select(JobEvent.seq, func.pg_column_size(JobEvent.data))
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.seq.desc())
        )
    ).all()
    used = 0
    cutoff = 0
    for kept, (seq, size) in enumerate(rows):
        if kept >= MAX_EVENTS or used + int(size) > MAX_BYTES:
            cutoff = seq  # this one and everything older goes
            break
        used += int(size)
    if cutoff:
        await session.execute(
            delete(JobEvent).where(JobEvent.job_id == job_id, JobEvent.seq <= cutoff)
        )


async def events_after(
    session: AsyncSession, job_id: str, after_seq: int, *, limit: int = 200
) -> list[JobEvent]:
    rows = await session.scalars(
        select(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.seq > after_seq)
        .order_by(JobEvent.seq)
        .limit(limit)
    )
    return list(rows)


async def has_events(session: AsyncSession, job_id: str) -> bool:
    found = await session.scalar(select(JobEvent.id).where(JobEvent.job_id == job_id).limit(1))
    return found is not None


async def purge_finished(session: AsyncSession, now: datetime) -> int:
    """Drops events and non-secret answers of jobs that ended more than 10 minutes ago."""
    cutoff = now - timedelta(minutes=RETENTION_MINUTES)
    finished = select(Job.id).where(Job.finished_at.is_not(None), Job.finished_at < cutoff)
    result = await session.execute(delete(JobEvent).where(JobEvent.job_id.in_(finished)))
    await session.execute(delete(JobAnswer).where(JobAnswer.job_id.in_(finished)))
    return int(getattr(result, "rowcount", 0) or 0)
