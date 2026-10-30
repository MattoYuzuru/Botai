"""Owner: W3. User sources (priority 2): upload a file or give a URL; the agent structures it."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, UploadFile

from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.db.session import DbSession
from botai.sources import service
from botai.sources.documents import MAX_UPLOAD
from botai.sources.schemas import DocumentFromUrl, DocumentOut, DocumentStarted

router = APIRouter(prefix="/api/v1", tags=["sources"])


@router.get("/spaces/{space_id}/documents", response_model=list[DocumentOut])
async def list_documents(space_id: str, user: AuthUser, session: DbSession) -> list[DocumentOut]:
    return await service.list_documents(session, user.id, space_id)


@router.post("/spaces/{space_id}/documents/url", response_model=DocumentStarted, status_code=201)
async def add_document_url(
    space_id: str,
    body: DocumentFromUrl,
    user: AuthUser,
    session: DbSession,
    services: ServicesDep,
) -> DocumentStarted:
    """A link to a file, a page, or a link-shared Google document or sheet."""
    return await service.start_url(session, user.id, space_id, body, now=services.clock.now())


@router.post("/spaces/{space_id}/documents/upload", response_model=DocumentStarted, status_code=201)
async def upload_document(
    space_id: str,
    file: UploadFile,
    user: AuthUser,
    session: DbSession,
    services: ServicesDep,
    note: Annotated[str | None, Form(max_length=1000)] = None,
) -> DocumentStarted:
    """PDF, DOCX, XLSX, CSV, HTML or text, up to 20 MiB."""
    data = await file.read(MAX_UPLOAD + 1)
    return await service.start_upload(
        session,
        user.id,
        space_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        note=note,
        now=services.clock.now(),
    )


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, user: AuthUser, session: DbSession) -> None:
    await service.delete_document(session, user.id, document_id)
