"""Owner: W5. Chat threads, messages, context meter, action approvals."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.chat import DEFAULT_TITLE
from botai.agent.context_meter import build_context
from botai.agent.schemas import (
    ApproveIn,
    ContextOut,
    MessageAccepted,
    MessageIn,
    MessageOut,
    ThreadCreate,
    ThreadOut,
)
from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.contracts.agent_ui import TextBlock
from botai.core.errors import BotaiError, ErrorCode, not_implemented
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db.models import ChatMessage, ChatThread, Job, Space
from botai.db.session import DbSession

router = APIRouter(prefix="/api/v1", tags=["agent"])

ACTIVE_JOB_STATES = ("queued", "running", "waiting_user")


def _thread_out(t: ChatThread) -> ThreadOut:
    return ThreadOut(
        id=t.id,
        space_id=t.space_id,
        title=t.title,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _message_out(m: ChatMessage) -> MessageOut:
    return MessageOut(
        id=m.id,
        role="user" if m.role == "user" else "agent",
        blocks=m.blocks,
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
    if body.space_id is not None:
        owned = (
            await session.execute(
                select(Space.id).where(Space.id == body.space_id, Space.user_id == user.id)
            )
        ).first()
        if owned is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")
    now = services.clock.now()
    thread = ChatThread(
        id=new_id(),
        user_id=user.id,
        space_id=body.space_id,
        title=(body.title or DEFAULT_TITLE).strip() or DEFAULT_TITLE,
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    await session.commit()
    return _thread_out(thread)


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
    thread = await own_thread(session, user.id, thread_id)
    busy = (
        await session.execute(
            select(Job.id).where(
                Job.user_id == user.id,
                Job.thread_id == thread_id,
                Job.kind == JobKind.CHAT.value,
                Job.state.in_(ACTIVE_JOB_STATES),
            )
        )
    ).first()
    if busy is not None:
        raise BotaiError(ErrorCode.CONFLICT, "Ассистент ещё отвечает на прошлое сообщение")

    now = services.clock.now()
    block = TextBlock(id=new_id(), at=now, markdown=body.text)
    message = ChatMessage(
        id=new_id(),
        user_id=user.id,
        thread_id=thread_id,
        role="user",
        blocks=[block.model_dump(by_alias=True, mode="json")],
        created_at=now,
    )
    session.add(message)
    thread.updated_at = now
    await session.commit()
    try:
        job_id = await services.queue.enqueue(
            JobKind.CHAT,
            user_id=user.id,
            thread_id=thread_id,
            payload={"thread_id": thread_id, "message_id": message.id},
            dedupe=False,
        )
    except Exception:
        await session.delete(message)  # no job will ever answer this message
        await session.commit()
        raise
    return MessageAccepted(message=_message_out(message), job_id=job_id)


@router.get("/chat/threads/{thread_id}/context", response_model=ContextOut)
async def thread_context(
    thread_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> ContextOut:
    thread = await own_thread(session, user.id, thread_id)
    return await build_context(session, services, user.id, thread)


@router.post("/actions/{proposal_id}/approve", status_code=202)
async def approve_action(proposal_id: str, body: ApproveIn) -> None:
    raise not_implemented("approve_action")


@router.post("/actions/{proposal_id}/reject", status_code=204)
async def reject_action(proposal_id: str) -> None:
    raise not_implemented("reject_action")
