"""Owner: W3. User sources (priority 2): upload a file or give a URL; the agent structures it."""

from __future__ import annotations

from fastapi import APIRouter, UploadFile

from botai.core.errors import not_implemented
from botai.sources.schemas import DocumentFromUrl, DocumentOut, DocumentStarted

router = APIRouter(prefix="/api/v1", tags=["sources"])


@router.get("/spaces/{space_id}/documents", response_model=list[DocumentOut])
async def list_documents(space_id: str) -> list[DocumentOut]:
    raise not_implemented("list_documents")


@router.post("/spaces/{space_id}/documents/url", response_model=DocumentStarted, status_code=201)
async def add_document_url(space_id: str, body: DocumentFromUrl) -> DocumentStarted:
    raise not_implemented("add_document_url")


@router.post("/spaces/{space_id}/documents/upload", response_model=DocumentStarted, status_code=201)
async def upload_document(space_id: str, file: UploadFile) -> DocumentStarted:
    raise not_implemented("upload_document")


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str) -> None:
    raise not_implemented("delete_document")
