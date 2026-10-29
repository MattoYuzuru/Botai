"""Chat files: store, bind to a message, read, index.

Every function takes the user's id and reports a foreign attachment exactly like a missing one
(404, NFR-001). Callers own the transaction: nothing here commits, so a message and the files it
carries become visible together.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from botai.attachments import kinds
from botai.attachments.kinds import AttachmentKind
from botai.attachments.schemas import AttachmentOut
from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobKind
from botai.db.models import Attachment, ChatMessage, ChatThread, Chunk, Connection
from botai.jobs.queue import enqueue_in_session
from botai.sources.documents import (
    MAX_UPLOAD,
    UPLOAD_ORIGIN,
    detect_format,
    extract,
    plain_text,
)
from botai.spaces import repo

MAX_ATTACHMENT = MAX_UPLOAD
MAX_USER_BYTES = 512 * 1024 * 1024  # everything one student keeps in chat files
MAX_PER_MESSAGE = 10
STALE_AFTER = "1 day"  # an upload nobody sent or used is dropped on the next upload
_DEFAULT_NAMES = {"image": "Изображение", "audio": "Аудио", "document": "Документ"}


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """What an `attachment` block of a message needs."""

    id: str
    name: str
    mime: str
    size: int
    kind: AttachmentKind


@dataclass(frozen=True, slots=True)
class Extract:
    """The text of an attachment for the agent. `error` says why a document could not be read."""

    text: str
    truncated: bool
    kind: AttachmentKind
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IngestStarted:
    connection_id: str
    job_id: str


def out(att: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=att.id,
        name=att.name,
        mime=att.mime,
        size=att.size,
        kind=att.kind,  # type: ignore[arg-type]  # validated by the response model
        created_at=att.created_at,
        document_id=att.document_id,
        ingest=att.ingest_state,  # type: ignore[arg-type]
    )


def ref(att: Attachment) -> AttachmentRef:
    return AttachmentRef(att.id, att.name, att.mime, att.size, att.kind)  # type: ignore[arg-type]


async def create(
    session: AsyncSession,
    user_id: str,
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    thread_id: str | None = None,
) -> Attachment:
    """Stores an unbound attachment. The type comes from the bytes; unsupported files are refused."""
    if len(data) > MAX_ATTACHMENT:
        raise BotaiError(ErrorCode.VALIDATION, "Файл больше 20 МБ", status=413)
    name = kinds.clean_filename(filename)
    detected = kinds.detect(data, name, content_type)
    if thread_id is not None and not await session.scalar(
        select(ChatThread.id).where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
    ):
        raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
    await session.execute(  # uploads nobody used would otherwise fill the quota for good
        delete(Attachment).where(
            Attachment.user_id == user_id,
            Attachment.message_id.is_(None),
            Attachment.document_id.is_(None),
            Attachment.created_at < func.now() - text(f"interval '{STALE_AFTER}'"),
        )
    )
    used = await session.scalar(
        select(func.coalesce(func.sum(Attachment.size), 0)).where(Attachment.user_id == user_id)
    )
    if (used or 0) + len(data) > MAX_USER_BYTES:
        raise BotaiError(
            ErrorCode.QUOTA_EXCEEDED,
            "Место для файлов закончилось: удалите ненужные вложения",
            status=413,
        )
    att = Attachment(
        user_id=user_id,
        thread_id=thread_id,
        name=name or _DEFAULT_NAMES[detected.kind],
        mime=detected.mime,
        size=len(data),
        kind=detected.kind,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(att)
    await session.flush()
    return att


async def get(
    session: AsyncSession, user_id: str, attachment_id: str, *, with_data: bool = False
) -> Attachment:
    query = select(Attachment).where(Attachment.id == attachment_id, Attachment.user_id == user_id)
    if with_data:
        query = query.options(undefer(Attachment.data))
    att = await session.scalar(query)
    if att is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Вложение не найдено")
    return att


async def delete_unsent(session: AsyncSession, user_id: str, attachment_id: str) -> None:
    att = await get(session, user_id, attachment_id)
    if att.message_id is not None or att.ingest_state == "queued":
        raise BotaiError(ErrorCode.CONFLICT, "Это вложение уже отправлено, его нельзя удалить")
    await session.delete(att)


async def bind_to_message(
    session: AsyncSession,
    user_id: str,
    thread_id: str,
    message_id: str,
    ids: Sequence[str],
) -> list[AttachmentRef]:
    """Attaches unsent files of the user to the message they send, in the order given.

    A file that is not the user's (or is gone) is "not found"; one that belongs to another
    message or another thread is a conflict. Nothing is bound unless every file is.
    """
    unique = list(dict.fromkeys(ids))
    if len(unique) > MAX_PER_MESSAGE:
        raise BotaiError(
            ErrorCode.VALIDATION, f"Можно приложить не больше {MAX_PER_MESSAGE} файлов"
        )
    if not unique:
        return []
    if not await session.scalar(
        select(ChatMessage.id).where(
            ChatMessage.id == message_id,
            ChatMessage.user_id == user_id,
            ChatMessage.thread_id == thread_id,
        )
    ):
        raise BotaiError(ErrorCode.NOT_FOUND, "Сообщение не найдено")
    rows = {
        a.id: a
        for a in await session.scalars(
            select(Attachment).where(Attachment.user_id == user_id, Attachment.id.in_(unique))
        )
    }
    if len(rows) != len(unique):
        raise BotaiError(ErrorCode.NOT_FOUND, "Вложение не найдено")
    for att in rows.values():
        if att.message_id is not None or att.thread_id not in (None, thread_id):
            raise BotaiError(ErrorCode.CONFLICT, "Вложение уже отправлено")
    for att in rows.values():
        att.thread_id, att.message_id = thread_id, message_id
    await session.flush()
    return [ref(rows[i]) for i in unique]


def _source_kind(mime: str) -> str:
    return (
        "sheet" if mime in {kinds.DOCUMENT_MIME["xlsx"], kinds.DOCUMENT_MIME["csv"]} else "document"
    )


async def start_ingest(
    session: AsyncSession,
    attachment: Attachment,
    space_id: str,
    thread_id: str | None,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> IngestStarted:
    """Makes the document source of an attachment in the space and queues its INGEST job.

    The job reads the bytes from the attachment, stores the text and its search fragments first
    (the document is usable from then on) and only afterwards asks about tables. Runs in the
    caller's transaction, together with whatever else the caller writes.
    """
    if attachment.kind != "document":
        raise BotaiError(ErrorCode.VALIDATION, "Этот файл не содержит текста для индексации")
    if attachment.ingest_state in {"queued", "indexed"}:
        raise BotaiError(ErrorCode.CONFLICT, "Файл уже обрабатывается")
    space = await repo.get_space(session, attachment.user_id, space_id)
    if thread_id is not None and not await session.scalar(
        select(ChatThread.id).where(
            ChatThread.id == thread_id, ChatThread.user_id == attachment.user_id
        )
    ):
        raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
    stem = attachment.name.rsplit(".", 1)[0] if "." in attachment.name else attachment.name
    conn = Connection(
        user_id=attachment.user_id,
        space_id=space.id,
        origin=UPLOAD_ORIGIN,
        kind=_source_kind(attachment.mime),
        title=" ".join(stem.split())[:120] or "Документ",
        status="draft",
        allow_hosts=[],
    )
    session.add(conn)
    await session.flush()
    job = await enqueue_in_session(
        session,
        JobKind.INGEST,
        user_id=attachment.user_id,
        connection_id=conn.id,
        thread_id=thread_id,
        payload={
            "source": {"kind": "upload"},
            "attachment_id": attachment.id,
            "filename": attachment.name,
            "note": (note or "")[:1000],
        },
        now=now or SystemClock().now(),
        dedupe=True,
    )
    attachment.ingest_state = "queued"
    await session.flush()
    return IngestStarted(connection_id=conn.id, job_id=job.id)


async def _indexed_text(
    session: AsyncSession, user_id: str, document_id: str, max_chars: int
) -> tuple[str, bool]:
    limit = max_chars // 100 + 5
    pieces = list(
        await session.scalars(
            select(Chunk.text)
            .where(Chunk.document_id == document_id, Chunk.user_id == user_id)
            .order_by(Chunk.ord)
            .limit(limit)
        )
    )
    joined = "\n".join(pieces)
    return joined[:max_chars], len(joined) > max_chars or len(pieces) == limit


async def extract_text(
    session: AsyncSession, user_id: str, attachment_id: str, max_chars: int
) -> Extract:
    """The text of a document for the agent's context, cut to `max_chars` (`truncated` says if it
    was). Images and audio have none: recognition is a later stage. Text is data, not commands."""
    att = await get(session, user_id, attachment_id, with_data=True)
    kind: AttachmentKind = att.kind  # type: ignore[assignment]
    if kind != "document":
        return Extract("", False, kind)
    if att.document_id is not None:
        content, truncated = await _indexed_text(session, user_id, att.document_id, max_chars)
        if content:
            return Extract(content, truncated, kind)
    try:
        fmt = detect_format(att.data, att.name, att.mime)
        extracted = await asyncio.to_thread(extract, att.data, fmt)
    except BotaiError as err:
        return Extract("", False, kind, error=err.detail or "Файл не удалось прочитать")
    content = plain_text(extracted)
    return Extract(content[:max_chars], len(content) > max_chars, kind)
