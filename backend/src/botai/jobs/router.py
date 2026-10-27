"""Owner: W1. Job status, SSE event stream (contracts/events.md §1), answers to blocks."""

from __future__ import annotations

from fastapi import APIRouter, Request

from botai.core.errors import not_implemented
from botai.jobs.schemas import AnswerIn, JobOut

router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    raise not_implemented("get_job")


@router.get("/jobs/{job_id}/events", responses={200: {"content": {"text/event-stream": {}}}})
async def job_events(job_id: str, request: Request) -> None:
    """text/event-stream: status | block | block_update | error | done; resume by Last-Event-ID."""
    raise not_implemented("job_events")


@router.post("/jobs/{job_id}/answers", status_code=204)
async def answer(job_id: str, body: AnswerIn) -> None:
    raise not_implemented("answer")


@router.post("/jobs/{job_id}/cancel", status_code=204)
async def cancel(job_id: str) -> None:
    raise not_implemented("cancel")
