"""The SSE stream of one job (contracts/events.md §1).

Events come from `job_events`; `id:` is the per-job `seq`, so `Last-Event-ID` resumes exactly after
the last event the client saw. A new event wakes the stream through NOTIFY, with a polling
fallback. The stream ends after the terminal event (`done` / `error`) or when the job is cancelled.
Nothing here reads secrets or page contents: it only replays what the job emitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette import ServerSentEvent

from botai.db.models import Job
from botai.jobs.events import TERMINAL, error_event, events_after, has_events, status_data
from botai.jobs.notify import CHANNEL_EVENTS, Notifier

KEEPALIVE_SECONDS = 15.0
POLL_SECONDS = 1.0


def parse_last_event_id(value: str | None) -> int:
    try:
        return max(0, int(value)) if value else 0
    except ValueError:
        return 0


def keepalive() -> ServerSentEvent:
    return ServerSentEvent(comment="keepalive")


def _encode(type_: str, seq: int, data: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(data, ensure_ascii=False), event=type_, id=str(seq))


def _terminal_events(job: Job, now: datetime, after: int) -> list[ServerSentEvent]:
    """The ending of a job whose buffered events are gone (they live 10 minutes after the end)."""
    out = [_encode("status", after + 1, status_data(job, now))]
    if job.state == "done":
        out.append(_encode("done", after + 2, {"result": job.result}))
    elif job.state == "failed":
        err = job.error or {}
        out.append(
            _encode(
                "error",
                after + 2,
                error_event(
                    job,
                    str(err.get("code", "internal_error")),
                    str(err.get("detail", "")),
                    int(err.get("status", 500)),
                    err.get("extra"),
                ),
            )
        )
    return out


async def stream(
    db: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    job_id: str,
    after: int,
    now: Any,
    poll_seconds: float = POLL_SECONDS,
) -> AsyncIterator[ServerSentEvent]:
    cursor = after
    with notifier.watch(CHANNEL_EVENTS, job_id) as wake:
        while True:
            wake.clear()
            async with db() as session:
                # State first, events second: a job seen as ended has all its events committed.
                job = await session.get(Job, job_id)
                if job is None:
                    return
                state = job.state
                rows = await events_after(session, job_id, cursor)
                purged = state in TERMINAL and not rows and not await has_events(session, job_id)
            for row in rows:
                cursor = row.seq
                yield _encode(row.type, row.seq, row.data)
            if rows:
                continue  # more may be waiting; read again before sleeping
            if state in TERMINAL:
                if purged:
                    for event in _terminal_events(job, now(), after):
                        yield event
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), poll_seconds)
