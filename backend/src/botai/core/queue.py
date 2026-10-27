from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from botai.core.jobs import JobKind


class JobQueue(Protocol):
    """Implementation: botai.jobs.queue.PgJobQueue (W1)."""

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
        """Returns the job id. With `dedupe`, an existing queued/running job of the same kind for
        the same connection is returned instead of creating a duplicate (NFR-006)."""
        ...


class InMemoryJobQueue:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

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
        from botai.core.ids import new_id

        if dedupe and connection_id:
            for j in self.jobs:
                if j["kind"] == kind and j["connection_id"] == connection_id:
                    return str(j["id"])
        job_id = new_id()
        self.jobs.append(
            {
                "id": job_id,
                "kind": kind,
                "user_id": user_id,
                "connection_id": connection_id,
                "thread_id": thread_id,
                "payload": payload or {},
                "run_after": run_after,
            }
        )
        return job_id
