"""Owner: W1. Spaces (universities of a user) and connections (sources inside a space)."""

from __future__ import annotations

from fastapi import APIRouter

from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.core.jobs import JobKind
from botai.db.session import DbSession
from botai.domain import repo as domain_repo
from botai.spaces import repo, service, universities
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
async def search_universities(
    user: AuthUser, session: DbSession, q: str = ""
) -> list[UniversityOut]:
    """Learned registry plus name-only suggestions; searching does not create integrations."""
    rows = await universities.search(session, q)
    return [UniversityOut.model_validate(u) for u in rows]


@router.get("/spaces", response_model=list[SpaceOut])
async def list_spaces(user: AuthUser, session: DbSession) -> list[SpaceOut]:
    return [service.space_out(s) for s in await repo.list_spaces(session, user.id)]


@router.post("/spaces", response_model=SpaceOut, status_code=201)
async def create_space(body: SpaceCreate, user: AuthUser, session: DbSession) -> SpaceOut:
    return service.space_out(await service.create_space(session, user.id, body))


@router.patch("/spaces/{space_id}", response_model=SpaceOut)
async def update_space(
    space_id: str, body: SpaceUpdate, user: AuthUser, session: DbSession
) -> SpaceOut:
    return service.space_out(await service.update_space(session, user.id, space_id, body))


@router.delete("/spaces/{space_id}", status_code=204)
async def delete_space(space_id: str, user: AuthUser, session: DbSession) -> None:
    await service.delete_space(session, user.id, space_id)


@router.get("/spaces/{space_id}/sections", response_model=list[SectionOut])
async def list_sections(space_id: str, user: AuthUser, session: DbSession) -> list[SectionOut]:
    """Dynamic sections (FR-014): only the kinds of data the space actually has."""
    await repo.get_space(session, user.id, space_id)
    counts = await domain_repo.section_counts(session, user.id, space_id)
    return [SectionOut(kind=kind, count=n) for kind, n in counts if n > 0]  # type: ignore[arg-type]


@router.get("/spaces/{space_id}/connections", response_model=list[ConnectionOut])
async def list_connections(
    space_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> list[ConnectionOut]:
    await repo.get_space(session, user.id, space_id)
    rows = await repo.list_connections(session, user.id, space_id)
    return [await service.connection_out(services.vault, c) for c in rows]


@router.post("/spaces/{space_id}/connections", response_model=ConnectionStarted, status_code=201)
async def create_connection(
    space_id: str,
    body: ConnectionCreate,
    user: AuthUser,
    session: DbSession,
    services: ServicesDep,
) -> ConnectionStarted:
    """Creates the connection and enqueues a `connect` job (login + learn)."""
    conn, job_id = await service.create_connection(session, services.queue, user.id, space_id, body)
    return ConnectionStarted(
        connection=await service.connection_out(services.vault, conn), job_id=job_id
    )


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    connection_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> ConnectionOut:
    conn = await repo.get_connection(session, user.id, connection_id)
    return await service.connection_out(services.vault, conn)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, user: AuthUser, session: DbSession) -> None:
    await service.delete_connection(session, user.id, connection_id)


@router.post("/connections/{connection_id}/sync", response_model=JobRef, status_code=202)
async def sync_connection(
    connection_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> JobRef:
    job_id = await service.enqueue_for_connection(
        session, services.queue, user.id, connection_id, JobKind.SYNC
    )
    return JobRef(job_id=job_id)


@router.post("/connections/{connection_id}/repair", response_model=JobRef, status_code=202)
async def repair_connection(
    connection_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> JobRef:
    job_id = await service.enqueue_for_connection(
        session, services.queue, user.id, connection_id, JobKind.REPAIR
    )
    return JobRef(job_id=job_id)


@router.put("/connections/{connection_id}/secrets", status_code=204)
async def put_secret(
    connection_id: str, body: SecretIn, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    """User-supplied tokens (Moodle token, iCal URL, API key). Never returned back."""
    await service.put_secret(session, services.vault, user.id, connection_id, body)


@router.delete("/connections/{connection_id}/password", status_code=204)
async def forget_password(
    connection_id: str, user: AuthUser, session: DbSession, services: ServicesDep
) -> None:
    await service.forget_password(session, services.vault, user.id, connection_id)
