"""Owner: W5. Chat threads, messages, context meter, action approvals."""

from __future__ import annotations

from fastapi import APIRouter

from botai.agent.schemas import (
    ApproveIn,
    ContextOut,
    MessageAccepted,
    MessageIn,
    MessageOut,
    ThreadCreate,
    ThreadOut,
)
from botai.core.errors import not_implemented

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.get("/chat/threads", response_model=list[ThreadOut])
async def list_threads() -> list[ThreadOut]:
    raise not_implemented("list_threads")


@router.post("/chat/threads", response_model=ThreadOut, status_code=201)
async def create_thread(body: ThreadCreate) -> ThreadOut:
    raise not_implemented("create_thread")


@router.get("/chat/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_messages(thread_id: str) -> list[MessageOut]:
    raise not_implemented("list_messages")


@router.post("/chat/threads/{thread_id}/messages", response_model=MessageAccepted, status_code=202)
async def post_message(thread_id: str, body: MessageIn) -> MessageAccepted:
    raise not_implemented("post_message")


@router.get("/chat/threads/{thread_id}/context", response_model=ContextOut)
async def thread_context(thread_id: str) -> ContextOut:
    raise not_implemented("thread_context")


@router.post("/actions/{proposal_id}/approve", status_code=202)
async def approve_action(proposal_id: str, body: ApproveIn) -> None:
    raise not_implemented("approve_action")


@router.post("/actions/{proposal_id}/reject", status_code=204)
async def reject_action(proposal_id: str) -> None:
    raise not_implemented("reject_action")
