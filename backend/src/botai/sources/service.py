"""User documents: start an ingestion from a link or an upload, list and remove them.

Every document is a connection of its own (`kind=document|doc|sheet`, adapter `document`), so it
is refreshed, repaired and removed by the same machinery as any source. An uploaded file is an
attachment first (`attachments`), the same as a file sent in chat. All reads and writes carry
the user's id (NFR-001); a foreign id is reported as not found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.attachments import service as attachments
from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobKind
from botai.db import models as m
from botai.jobs.queue import enqueue_in_session
from botai.sources.ingest import check_url, source_for_url
from botai.sources.schemas import DocumentFromUrl, DocumentOut, DocumentStarted
from botai.spaces import repo, views

NOTE_MAX = 1000


async def list_documents(session: AsyncSession, user_id: str, space_id: str) -> list[DocumentOut]:
    space = await repo.get_space(session, user_id, space_id)
    rows = await session.scalars(
        select(m.Document)
        .where(m.Document.user_id == user_id, m.Document.space_id == space.id)
        .order_by(m.Document.fetched_at.desc(), m.Document.id)
    )
    return [DocumentOut.model_validate(doc) for doc in rows]


async def start_url(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    body: DocumentFromUrl,
    *,
    now: datetime | None = None,
) -> DocumentStarted:
    """A link to a file, a page, or a link-shared Google document: a source and its INGEST job."""
    host = check_url(body.url)
    source, hosts = source_for_url(body.url)
    space = await repo.get_space(session, user_id, space_id)
    kind = "sheet" if source["kind"] == "google_sheet" else "doc"
    payload: dict[str, Any] = {"source": source, "note": (body.note or "")[:NOTE_MAX]}
    conn = m.Connection(
        user_id=user_id,
        space_id=space.id,
        origin=f"https://{host}",
        kind=kind,
        # the real name comes from the document once it is read
        title=views.source_title(kind=kind, title=body.title),
        entry_url=body.url.strip()[:2000],
        status="draft",
        allow_hosts=hosts,
    )
    session.add(conn)
    await session.flush()
    job = await enqueue_in_session(
        session,
        JobKind.INGEST,
        user_id=user_id,
        connection_id=conn.id,
        payload=payload,
        now=now or SystemClock().now(),
        dedupe=True,
    )
    await session.commit()
    return DocumentStarted(document_id=None, connection_id=conn.id, job_id=job.id)


async def start_upload(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    note: str | None,
    now: datetime | None = None,
) -> DocumentStarted:
    """A file for the library of a space: stored as an attachment and indexed like one."""
    await repo.get_space(session, user_id, space_id)
    att = await attachments.create(
        session, user_id, filename=filename, content_type=content_type, data=data
    )
    if att.kind != "document":
        raise BotaiError(ErrorCode.VALIDATION, "В библиотеку добавляются документы и таблицы")
    started = await attachments.start_ingest(
        session, att, space_id, None, note=(note or "")[:NOTE_MAX], now=now
    )
    await session.commit()
    return DocumentStarted(
        document_id=None, connection_id=started.connection_id, job_id=started.job_id
    )


async def delete_document(session: AsyncSession, user_id: str, document_id: str) -> None:
    """Removes the document with its connection (so it is no longer refreshed), its text, rows
    read from it, its spec revisions and secrets, and the file it was uploaded as unless a chat
    message carries that file."""
    doc = await session.scalar(
        select(m.Document).where(m.Document.id == document_id, m.Document.user_id == user_id)
    )
    if doc is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Документ не найден")
    await session.execute(
        delete(m.Attachment).where(
            m.Attachment.user_id == user_id,
            m.Attachment.document_id == doc.id,
            m.Attachment.message_id.is_(None),
        )
    )
    conn = (
        await session.scalar(
            select(m.Connection).where(
                m.Connection.id == doc.connection_id, m.Connection.user_id == user_id
            )
        )
        if doc.connection_id
        else None
    )
    if conn is not None and conn.kind in {"document", "doc", "sheet"}:
        await repo.purge_connection_secrets(session, user_id, [conn.id])
        await session.execute(
            delete(m.AdapterSpecRow).where(m.AdapterSpecRow.origin == f"doc:{conn.id}")
        )
        await session.delete(conn)  # documents and chunks follow by ON DELETE CASCADE
    else:
        await session.delete(doc)
    await session.commit()
