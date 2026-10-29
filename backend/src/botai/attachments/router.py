"""Chat files (docs/plan/agent-hub-v3.md §3.6): upload, metadata, bytes, delete an unsent one."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, UploadFile

from botai.attachments import kinds, service
from botai.attachments.schemas import AttachmentOut
from botai.auth.deps import AuthUser
from botai.db.session import DbSession

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


@router.post("", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    file: UploadFile,
    user: AuthUser,
    session: DbSession,
    thread_id: Annotated[str | None, Form(alias="threadId", max_length=26)] = None,
) -> AttachmentOut:
    """A file up to 20 MiB: image, PDF, DOCX, XLSX, CSV, text, HTML or audio. The type is read
    from the bytes. The file stays unsent until a message carries it."""
    data = await file.read(service.MAX_ATTACHMENT + 1)
    att = await service.create(
        session,
        user.id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        thread_id=thread_id or None,  # an empty form field is no thread
    )
    await session.commit()
    return service.out(att)


@router.get("/{attachment_id}", response_model=AttachmentOut)
async def get_attachment(attachment_id: str, user: AuthUser, session: DbSession) -> AttachmentOut:
    return service.out(await service.get(session, user.id, attachment_id))


@router.get("/{attachment_id}/content", response_class=Response)
async def attachment_content(
    attachment_id: str, request: Request, user: AuthUser, session: DbSession
) -> Response:
    """The bytes, shown in place when a browser can show them safely (HTML never is), with
    byte ranges so that audio can be played and sought on every browser."""
    att = await service.get(session, user.id, attachment_id, with_data=True)
    content_type, disposition = kinds.served_as(att.name, att.mime)
    headers = {
        "content-type": content_type,
        "content-disposition": disposition,
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
        "accept-ranges": "bytes",
    }
    data = att.data
    start, end = 0, len(data) - 1
    status = 200
    if (wanted := request.headers.get("range")) is not None:
        match = _RANGE.match(wanted.strip())
        first, last = (match.group(1), match.group(2)) if match else ("", "")
        if not match or (first == "" and last == ""):
            return Response(status_code=416, headers={"content-range": f"bytes */{len(data)}"})
        if first == "":  # the last N bytes
            start = max(0, len(data) - int(last))
        else:
            start = int(first)
            end = min(end, int(last)) if last else end
        if start > end or start >= len(data):
            return Response(status_code=416, headers={"content-range": f"bytes */{len(data)}"})
        headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
        status = 206
    return Response(content=data[start : end + 1], status_code=status, headers=headers)


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: str, user: AuthUser, session: DbSession) -> None:
    """Only a file that no message carries yet."""
    await service.delete_unsent(session, user.id, attachment_id)
    await session.commit()
