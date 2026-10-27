"""Owner: W1. Spaces (universities of a user) and connections (sources inside a space)."""

from __future__ import annotations

from fastapi import APIRouter

from botai.core.errors import not_implemented
from botai.spaces.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionStarted,
    JobRef,
    SecretIn,
    SectionOut,
    SpaceCreate,
    SpaceOut,
    SpaceUpdate,
    UniversityOut,
)

router = APIRouter(prefix="/api/v1", tags=["spaces"])


@router.get("/universities", response_model=list[UniversityOut])
async def search_universities(q: str = "") -> list[UniversityOut]:
    raise not_implemented("search_universities")


@router.get("/spaces", response_model=list[SpaceOut])
async def list_spaces() -> list[SpaceOut]:
    raise not_implemented("list_spaces")


@router.post("/spaces", response_model=SpaceOut, status_code=201)
async def create_space(body: SpaceCreate) -> SpaceOut:
    raise not_implemented("create_space")


@router.patch("/spaces/{space_id}", response_model=SpaceOut)
async def update_space(space_id: str, body: SpaceUpdate) -> SpaceOut:
    raise not_implemented("update_space")


@router.delete("/spaces/{space_id}", status_code=204)
async def delete_space(space_id: str) -> None:
    raise not_implemented("delete_space")


@router.get("/spaces/{space_id}/sections", response_model=list[SectionOut])
async def list_sections(space_id: str) -> list[SectionOut]:
    raise not_implemented("list_sections")


@router.get("/spaces/{space_id}/connections", response_model=list[ConnectionOut])
async def list_connections(space_id: str) -> list[ConnectionOut]:
    raise not_implemented("list_connections")


@router.post("/spaces/{space_id}/connections", response_model=ConnectionStarted, status_code=201)
async def create_connection(space_id: str, body: ConnectionCreate) -> ConnectionStarted:
    """Creates the connection and enqueues a `connect` job (login + learn)."""
    raise not_implemented("create_connection")


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: str) -> ConnectionOut:
    raise not_implemented("get_connection")


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: str) -> None:
    raise not_implemented("delete_connection")


@router.post("/connections/{connection_id}/sync", response_model=JobRef, status_code=202)
async def sync_connection(connection_id: str) -> JobRef:
    raise not_implemented("sync_connection")


@router.post("/connections/{connection_id}/repair", response_model=JobRef, status_code=202)
async def repair_connection(connection_id: str) -> JobRef:
    raise not_implemented("repair_connection")


@router.put("/connections/{connection_id}/secrets", status_code=204)
async def put_secret(connection_id: str, body: SecretIn) -> None:
    """User-supplied tokens (Moodle token, iCal URL, API key). Never returned back."""
    raise not_implemented("put_secret")


@router.delete("/connections/{connection_id}/password", status_code=204)
async def forget_password(connection_id: str) -> None:
    raise not_implemented("forget_password")
