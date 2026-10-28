"""Owner: W5. Chat threads, messages, context meter, action approvals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent import actions, conversations
from botai.agent.context_meter import build_context
from botai.agent.schemas import (
    ApproveIn,
    ContextOut,
    MessageAccepted,
    MessageIn,
    MessageOut,
    ThreadCreate,
    ThreadOut,
    ThreadRuntime,
)
from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.contracts.agent_ui import AgentUiBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import ChatMessage, ChatThread
from botai.db.session import DbSession

router = APIRouter(prefix="/api/v1", tags=["agent"])

ACTIVE_JOB_STATES = ("queued", "running", "waiting_user")
_BLOCK = TypeAdapter(AgentUiBlock)


def _thread_out(t: ChatThread) -> ThreadOut:
    return ThreadOut(
        id=t.id,
        space_id=t.space_id,
        title=t.title,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _message_out(m: ChatMessage) -> MessageOut:
    blocks: list[dict[str, Any]] = []
    for raw in m.blocks:
        try:
            block = _BLOCK.validate_python(raw)
        except ValidationError:
            # Preserve future or malformed blocks for the PWA's explicit unknown-block fallback.
            blocks.append(raw)
        else:
            # Existing rows may predate the wire fix and contain sources:null (or nested nulls).
            blocks.append(block.model_dump(by_alias=True, mode="json", exclude_none=True))
    return MessageOut(
        id=m.id,
        role="user" if m.role == "user" else "agent",
        blocks=blocks,
        job_id=m.job_id,
        created_at=m.created_at,
    )


async def own_thread(session: AsyncSession, user_id: str, thread_id: str) -> ChatThread:
    """The thread if it is the user's; otherwise 404 (a foreign id is indistinguishable)."""
    thread = (
        await session.execute(
            select(ChatThread).where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
        )
    ).scalar_one_or_none()
    if thread is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
    return thread


@router.get("/chat/threads", response_model=list[ThreadOut])
async def list_threads(user: AuthUser, session: DbSession) -> list[ThreadOut]:
    rows = (
        await session.execute(
            select(ChatThread)
            .where(ChatThread.user_id == user.id)
            .order_by(ChatThread.updated_at.desc(), ChatThread.id.desc())
        )
    ).scalars()
    return [_thread_out(t) for t in rows]


@router.post("/chat/threads", response_model=ThreadOut, status_code=201)
async def create_thread(
    body: ThreadCreate, user: AuthUser, session: DbSession, services: ServicesDep
) -> ThreadOut:
    return _thread_out(await conversations.create_thread(session, services, user.id, body))


@router.get("/chat/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_messages(thread_id: str, user: AuthUser, session: DbSession) -> list[MessageOut]:
    await own_thread(session, user.id, thread_id)
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id, ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars()
    return [_message_out(m) for m in rows]


@router.post("/chat/threads/{thread_id}/messages", response_model=MessageAccepted, status_code=202)
async def post_message(
    thread_id: str, body: MessageIn, user: AuthUser, session: DbSession, services: ServicesDep
) -> MessageAccepted:
    return await conversations.post_message(session, services, user.id, thread_id, body)


@router.get("/chat/threads/{thread_id}/runtime", response_model=ThreadRuntime)
async def thread_runtime(
    thread_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> ThreadRuntime:
    return await conversations.runtime(session, services, user.id, thread_id)


@router.get("/chat/threads/{thread_id}/context", response_model=ContextOut)
async def thread_context(
    thread_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> ContextOut:
    thread = await own_thread(session, user.id, thread_id)
    return await build_context(session, services, user.id, thread)


@router.post("/actions/{proposal_id}/approve", status_code=202)
async def approve_action(
    proposal_id: str, body: ApproveIn, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    await actions.approve(session, services, user.id, proposal_id, body)


@router.post("/actions/{proposal_id}/reject", status_code=204)
async def reject_action(proposal_id: str, user: AuthUser, session: DbSession) -> None:
    await actions.reject(session, user.id, proposal_id)
