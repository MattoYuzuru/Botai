"""Owner: W1. Job status, SSE event stream (contracts/events.md §1), answers to blocks."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sse_starlette import EventSourceResponse

from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.db.session import DbSession
from botai.jobs import answers, sse
from botai.jobs.notify import dsn_for_asyncpg, shared_notifier
from botai.jobs.schemas import AnswerIn, JobOut

router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, user: AuthUser, session: DbSession) -> JobOut:
    return JobOut.model_validate(await answers.get_job(session, user.id, job_id))


@router.get("/jobs/{job_id}/events", responses={200: {"content": {"text/event-stream": {}}}})
async def job_events(
    job_id: str, request: Request, user: AuthUser, session: DbSession, services: ServicesDep
) -> EventSourceResponse:
    """text/event-stream: status | block | block_update | error | done; resume by Last-Event-ID.

    Only the owner of the job may listen. The stream replays the buffered events after
    `Last-Event-ID`, then follows the job until it ends; `: keepalive` every 15 seconds."""
    await answers.get_job(session, user.id, job_id)
    await session.rollback()  # do not hold a connection for the whole stream
    notifier = await shared_notifier(dsn_for_asyncpg(services.settings.database_url))
    return EventSourceResponse(
        sse.stream(
            services.db,
            notifier,
            job_id=job_id,
            after=sse.parse_last_event_id(request.headers.get("last-event-id")),
            now=services.clock.now,
        ),
        ping=sse.KEEPALIVE_SECONDS,
        ping_message_factory=sse.keepalive,
    )


@router.post("/jobs/{job_id}/answers", status_code=204)
async def answer(
    job_id: str, body: AnswerIn, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    """Answers a block. A secret input goes straight into the Vault (`transient`, 10 minutes);
    only the fact of the answer is stored. A second answer to the same block is `409`."""
    await answers.record_answer(session, services.vault, user.id, job_id, body)


@router.post("/jobs/{job_id}/cancel", status_code=204)
async def cancel(job_id: str, user: AuthUser, session: DbSession, services: ServicesDep) -> None:
    await answers.cancel_job(session, services.vault, services.clock, user.id, job_id)
