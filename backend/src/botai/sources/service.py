"""User documents: start an ingestion from a link or an upload, list and remove them.

Every document is a connection of its own (`kind=document|sheet`, adapter `document`), so it is
refreshed, repaired and removed by the same machinery as any source. All reads and writes carry
the user's id (NFR-001); a foreign id is reported as not found.
"""

from __future__ import annotations

import base64
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobKind
from botai.core.queue import JobQueue
from botai.db import models as m
from botai.sources.documents import detect_format
from botai.sources.ingest import MAX_UPLOAD, UPLOAD_ORIGIN, check_url, source_for_url
from botai.sources.schemas import DocumentFromUrl, DocumentOut, DocumentStarted
from botai.spaces import repo

NOTE_MAX = 1000


def _title(raw: str | None, fallback: str) -> str:
    return " ".join((raw or "").split())[:120] or fallback


def _clean_filename(name: str | None) -> str:
    leaf = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in leaf if ch.isprintable())[:200]


async def list_documents(session: AsyncSession, user_id: str, space_id: str) -> list[DocumentOut]:
    space = await repo.get_space(session, user_id, space_id)
    rows = await session.scalars(
        select(m.Document)
        .where(m.Document.user_id == user_id, m.Document.space_id == space.id)
        .order_by(m.Document.fetched_at.desc(), m.Document.id)
    )
    return [DocumentOut.model_validate(doc) for doc in rows]


async def _start(
    session: AsyncSession,
    queue: JobQueue,
    user_id: str,
    space_id: str,
    *,
    origin: str,
    allow_hosts: list[str],
    kind: str,
    title: str,
    payload: dict[str, Any],
) -> DocumentStarted:
    space = await repo.get_space(session, user_id, space_id)
    conn = m.Connection(
        user_id=user_id,
        space_id=space.id,
        origin=origin,
        kind=kind,
        title=title,
        status="draft",
        allow_hosts=allow_hosts,
    )
    session.add(conn)
    await session.commit()  # the queue works in its own transaction
    try:
        job_id = await queue.enqueue(
            JobKind.INGEST, user_id=user_id, connection_id=conn.id, payload=payload, dedupe=True
        )
    except Exception:
        await session.delete(conn)  # no job means nobody would ever finish this document
        await session.commit()
        raise
    return DocumentStarted(document_id=None, connection_id=conn.id, job_id=job_id)


async def start_url(
    session: AsyncSession, queue: JobQueue, user_id: str, space_id: str, body: DocumentFromUrl
) -> DocumentStarted:
    host = check_url(body.url)
    source, hosts = source_for_url(body.url)
    return await _start(
        session,
        queue,
        user_id,
        space_id,
        origin=f"https://{host}",
        allow_hosts=hosts,
        kind="sheet" if source["kind"] == "google_sheet" else "document",
        title=_title(body.title, host),
        payload={"source": source, "note": (body.note or "")[:NOTE_MAX]},
    )


async def start_upload(
    session: AsyncSession,
    queue: JobQueue,
    user_id: str,
    space_id: str,
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    note: str | None,
) -> DocumentStarted:
    if not data:
        raise BotaiError(ErrorCode.VALIDATION, "Файл пустой")
    if len(data) > MAX_UPLOAD:
        raise BotaiError(ErrorCode.VALIDATION, "Файл больше 8 МБ", status=413)
    name = _clean_filename(filename)
    detect_format(data, name, content_type)  # an unsupported type is refused at once
    return await _start(
        session,
        queue,
        user_id,
        space_id,
        origin=UPLOAD_ORIGIN,
        allow_hosts=[],
        kind="document",
        title=_title(name.rsplit(".", 1)[0] if "." in name else name, "Документ"),
        payload={
            "source": {"kind": "upload"},
            "content_b64": base64.b64encode(data).decode("ascii"),
            "filename": name,
            "content_type": (content_type or "")[:120],
            "note": (note or "")[:NOTE_MAX],
        },
    )


async def delete_document(session: AsyncSession, user_id: str, document_id: str) -> None:
    """Removes the document with its connection (so it is no longer refreshed), its text, rows
    read from it, its spec revisions and secrets."""
    doc = await session.scalar(
        select(m.Document).where(m.Document.id == document_id, m.Document.user_id == user_id)
    )
    if doc is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Документ не найден")
    conn = (
        await session.scalar(
            select(m.Connection).where(
                m.Connection.id == doc.connection_id, m.Connection.user_id == user_id
            )
        )
        if doc.connection_id
        else None
    )
    if conn is not None and conn.kind in {"document", "sheet"}:
        await repo.purge_connection_secrets(session, user_id, [conn.id])
        await session.execute(
            delete(m.AdapterSpecRow).where(m.AdapterSpecRow.origin == f"doc:{conn.id}")
        )
        await session.delete(conn)  # documents and chunks follow by ON DELETE CASCADE
    else:
        await session.delete(doc)
    await session.commit()
