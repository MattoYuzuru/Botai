"""The run history of one v4 source: jobs, their outcome and the agent trace of each run.

A v4 source has no v3 connection of its own: jobs point at it through the linked connection
(`Source.config["connection_id"]`, written by the v4 dispatch), through the job payload
(`source_id`), or through the artifact the run saved (`source_artifacts.run_id`). One query covers
the three links; the agent trace ids of the page are one more query, so the endpoint stays bounded
and never loops over the runs.

Owner scope is the caller's: `user_id` is in every predicate; the source itself was already
resolved by the service (a foreign id is 404, not 403).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import AgentRun, Job, Source, SourceArtifact

MAX_RUNS = 100


def _counts(result: Any) -> dict[str, int]:
    """Records a run wrote, by v4 data kind (the sync/repair job body keeps them under `counts`)."""
    if not isinstance(result, dict):
        return {}
    raw = result.get("counts")
    if not isinstance(raw, dict):
        return {}
    return {str(key): int(value) for key, value in raw.items() if isinstance(value, int)}


def _error(job: Job) -> tuple[str | None, str | None]:
    raw = job.error
    if not isinstance(raw, dict):
        return None, None
    code = raw.get("code")
    detail = raw.get("detail")
    return (
        code if isinstance(code, str) else None,
        detail if isinstance(detail, str) else None,
    )


def _ok(job: Job) -> bool | None:
    if job.state == "done":
        return True
    if job.state in {"failed", "cancelled"}:
        return False
    return None


def run_out(*, source_id: str, job: Job, run_id: str | None) -> dict[str, Any]:
    code, detail = _error(job)
    return {
        "id": job.id,
        "source_id": source_id,
        "kind": job.kind,
        "status": job.state,
        "ok": _ok(job),
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "counts": _counts(job.result),
        "error_code": code,
        "error_detail": detail,
        "thread_id": job.thread_id,
        "run_id": run_id,
    }


async def list_runs(
    session: AsyncSession,
    *,
    user_id: str,
    source: Source,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Newest first: the jobs that read this source, by connection link, payload or artifact run."""
    linked = source.config.get("connection_id")
    clauses = [Job.payload["source_id"].as_string() == source.id]
    if isinstance(linked, str) and linked:
        clauses.append(Job.connection_id == linked)
    artifact_runs = select(SourceArtifact.run_id).where(
        SourceArtifact.user_id == user_id,
        SourceArtifact.source_id == source.id,
        SourceArtifact.run_id.is_not(None),
    )
    clauses.append(Job.id.in_(artifact_runs))
    jobs = list(
        await session.scalars(
            select(Job)
            .where(Job.user_id == user_id, or_(*clauses))
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(min(max(limit, 1), MAX_RUNS))
            .offset(max(offset, 0))
        )
    )
    if not jobs:
        return []
    traces = {
        run.job_id: run.run_id
        for run in await session.scalars(
            select(AgentRun).where(
                AgentRun.user_id == user_id, AgentRun.job_id.in_([job.id for job in jobs])
            )
        )
        if run.job_id is not None
    }
    return [run_out(source_id=source.id, job=job, run_id=traces.get(job.id)) for job in jobs]


__all__ = ["MAX_RUNS", "list_runs", "run_out"]
