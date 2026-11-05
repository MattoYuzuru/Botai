"""Read-only links to a thread for other BotAI users (`/shared/<id>`).

Any signed-in user with the link reads the thread as it is now: the words of the student and the
agent and the names of attached files. Reasoning steps, questions, task cards and the files
themselves stay with the owner (attachments, threads and documents keep their own access checks, so
a link inside an answer opens only for someone who already has access to it).
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.schemas import (
    SharedAttachment,
    SharedMessage,
    SharedThreadOut,
    ShareOut,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import ChatMessage, ChatShare, ChatThread, User
from botai.spaces.conversations import owned_thread

SHARE_ID_BYTES = 16  # 22 URL-safe characters
_GONE = "Ссылка недействительна: чат удалён или владелец закрыл доступ"


def _share_out(share: ChatShare) -> ShareOut:
    return ShareOut(share_id=share.id, created_at=share.created_at)


async def get_share(session: AsyncSession, user_id: str, thread_id: str) -> ShareOut | None:
    await owned_thread(session, user_id, thread_id)
    share = await session.scalar(select(ChatShare).where(ChatShare.thread_id == thread_id))
    return _share_out(share) if share else None


async def share_thread(session: AsyncSession, user_id: str, thread_id: str) -> ShareOut:
    """Idempotent: a thread that is already shared keeps its link."""
    await owned_thread(session, user_id, thread_id)
    share = await session.scalar(select(ChatShare).where(ChatShare.thread_id == thread_id))
    if share is None:
        share = ChatShare(
            id=secrets.token_urlsafe(SHARE_ID_BYTES), thread_id=thread_id, user_id=user_id
        )
        session.add(share)
        await session.commit()
        await session.refresh(share)
    return _share_out(share)


async def unshare_thread(session: AsyncSession, user_id: str, thread_id: str) -> None:
    await owned_thread(session, user_id, thread_id)
    await session.execute(delete(ChatShare).where(ChatShare.thread_id == thread_id))
    await session.commit()


def _shared_message(message: ChatMessage) -> SharedMessage | None:
    texts: list[str] = []
    quote: str | None = None
    files: list[SharedAttachment] = []
    for raw in message.blocks:
        kind = raw.get("type")
        if kind == "text" and isinstance(raw.get("markdown"), str):
            texts.append(raw["markdown"])
            q: Any = raw.get("quote")
            if quote is None and isinstance(q, dict) and isinstance(q.get("text"), str):
                quote = q["text"]
        elif kind == "attachment":
            try:
                files.append(SharedAttachment.model_validate(raw))
            except ValidationError:
                continue
    markdown = "\n\n".join(t for t in texts if t.strip())
    if not markdown and not files:
        return None
    return SharedMessage(
        id=message.id,
        role="user" if message.role == "user" else "agent",
        markdown=markdown,
        quote=quote,
        attachments=files,
        created_at=message.created_at,
    )


async def shared_thread(session: AsyncSession, viewer_id: str, share_id: str) -> SharedThreadOut:
    row = (
        await session.execute(
            select(ChatShare, ChatThread, User)
            .join(ChatThread, ChatThread.id == ChatShare.thread_id)
            .join(User, User.id == ChatShare.user_id)
            .where(ChatShare.id == share_id, ChatThread.user_id == ChatShare.user_id)
        )
    ).first()
    if row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, _GONE)
    share, thread, owner = row
    if owner.disabled:
        raise BotaiError(ErrorCode.NOT_FOUND, _GONE)
    messages = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id, ChatMessage.user_id == owner.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars()
    mine = owner.id == viewer_id
    return SharedThreadOut(
        share_id=share.id,
        title=thread.title,
        owner_name=owner.display_name,
        mine=mine,
        thread_id=thread.id if mine else None,
        shared_at=share.created_at,
        messages=[m for m in map(_shared_message, messages) if m is not None],
    )
