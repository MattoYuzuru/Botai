"""Owner: WS1. Chat threads, messages, runtime, context meter, memory notes, action approvals."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from botai.agent import actions, conversations, notes, threads
from botai.agent.context_meter import build_context
from botai.agent.schemas import (
    ApproveIn,
    ContextOut,
    MemoryNoteOut,
    MessageAccepted,
    MessageIn,
    MessageOut,
    ThreadCreate,
    ThreadOut,
    ThreadPatch,
    ThreadRuntime,
)
from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.db.models import ChatMessage
from botai.db.session import DbSession
from botai.spaces.conversations import owned_thread

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.get("/chat/threads", response_model=list[ThreadOut])
async def list_threads(user: AuthUser, session: DbSession) -> list[ThreadOut]:
    return await threads.list_threads(session, user.id)


@router.post("/chat/threads", response_model=ThreadOut, status_code=201)
async def create_thread(
    body: ThreadCreate, user: AuthUser, session: DbSession, services: ServicesDep
) -> ThreadOut:
    thread = await conversations.create_thread(session, services, user.id, body)
    return await threads.thread_out(session, thread)


@router.patch("/chat/threads/{thread_id}", response_model=ThreadOut)
async def rename_thread(
    thread_id: str, body: ThreadPatch, user: AuthUser, session: DbSession
) -> ThreadOut:
    return await threads.rename_thread(session, user.id, thread_id, body.title)


@router.delete("/chat/threads/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    """Stops the tasks and the running turn of the thread, then deletes it with its messages."""
    await threads.delete_thread(session, services, user.id, thread_id)


@router.post("/chat/threads/{thread_id}/read", status_code=204)
async def read_thread(
    thread_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    await threads.mark_read(session, services, user.id, thread_id)


@router.get("/chat/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_messages(thread_id: str, user: AuthUser, session: DbSession) -> list[MessageOut]:
    await owned_thread(session, user.id, thread_id)
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id, ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars()
    return [conversations.message_out(m) for m in rows]


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
    thread = await owned_thread(session, user.id, thread_id)
    return await build_context(session, services, user.id, thread)


@router.get("/me/memory", response_model=list[MemoryNoteOut])
async def list_memory(user: AuthUser, session: DbSession) -> list[MemoryNoteOut]:
    return [MemoryNoteOut.model_validate(n) for n in await notes.list_notes(session, user.id)]


@router.delete("/me/memory", status_code=204)
async def clear_memory(user: AuthUser, session: DbSession) -> None:
    await notes.clear_notes(session, user.id)


@router.delete("/me/memory/{note_id}", status_code=204)
async def forget_memory(note_id: str, user: AuthUser, session: DbSession) -> None:
    await notes.delete_note(session, user.id, note_id)


@router.post("/actions/{proposal_id}/approve", status_code=202)
async def approve_action(
    proposal_id: str, body: ApproveIn, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    await actions.approve(session, services, user.id, proposal_id, body)


@router.post("/actions/{proposal_id}/reject", status_code=204)
async def reject_action(proposal_id: str, user: AuthUser, session: DbSession) -> None:
    await actions.reject(session, user.id, proposal_id)
