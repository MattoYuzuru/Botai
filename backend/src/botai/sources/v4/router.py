"""REST and SSE of v4 sources: `/api/v1/sources`, playbooks, artifacts, state, priorities.

The router is registered always (stable OpenAPI), but every endpoint is disabled unless
`BOTAI_AGENT_ENGINE=v4`: on the v3 engine the dependency answers 404, so production is safe and
enabling v4 is one env change. The runner is built through an overridable dependency
(`get_runner_builder`), so tests inject a mock transport and an artifact store without touching the
network; the default builder uses the real HTTP client.

Nothing here returns a secret: `auth_ref` is only a Vault handle name, `identity_key` is a digest
or a normalized address, and run reports never contain request headers or cookies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse

from botai.api.deps import ServicesDep, SettingsDep
from botai.auth.deps import AuthUser
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    Playbook,
    PlaybookVersion,
    Source,
    SourceArtifact,
    SourcePriority,
    SourceState,
)
from botai.db.session import DbSession
from botai.sources.v4 import artifacts, playbooks, priorities, service, state
from botai.sources.v4.runner import RunReport, SourceRunner
from botai.sources.v4.schemas import (
    ArtifactOut,
    CanaryIn,
    PlaybookCreateIn,
    PlaybookOut,
    PlaybookVersionIn,
    PlaybookVersionOut,
    PriorityIn,
    PriorityOut,
    ProbeIn,
    RepairIn,
    RunReportOut,
    SourceCreateIn,
    SourceOut,
    SourcePatch,
    SourceStateOut,
    SyncIn,
)

router = APIRouter(prefix="/api/v1", tags=["sources-v4"])

RunnerBuilder = Callable[[AsyncSession, str], SourceRunner]


def require_sources_v4(settings: SettingsDep) -> None:
    """The phase-2 API exists only on the v4 engine; on v3 it is not found, not partially on."""
    if settings.agent_engine != "v4":
        raise BotaiError(
            ErrorCode.NOT_FOUND,
            "Источники v4 выключены: включите BOTAI_AGENT_ENGINE=v4",
        )


def default_runner_builder(session: AsyncSession, user_id: str) -> SourceRunner:
    """Production runner: the real HTTP client, no artifact bytes sink configured yet."""
    return SourceRunner(session, user_id=user_id)


def get_runner_builder() -> RunnerBuilder:
    """Overridable in tests: returns the function that builds a runner for a session and user."""
    return default_runner_builder


V4 = Depends(require_sources_v4)
RunnerDep = Annotated[RunnerBuilder, Depends(get_runner_builder)]


def _source_out(source: Source) -> SourceOut:
    return SourceOut(
        id=source.id,
        space_id=source.space_id,
        university_id=source.university_id,
        title=source.title,
        entry_url=source.entry_url,
        kinds=[kind for kind in source.kinds if kind],
        status=source.status,
        playbook_id=source.playbook_id,
        config=source.config,
        auth_ref=source.auth_ref,
        priority=source.priority,
        trust=source.trust,
        last_sync_at=source.last_sync_at,
        last_error=source.last_error,
        disabled_from=source.disabled_from,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _playbook_out(playbook: Playbook) -> PlaybookOut:
    return PlaybookOut(
        id=playbook.id,
        university_id=playbook.university_id,
        owner_id=playbook.owner_id,
        title=playbook.title,
        language=playbook.language,
        current_version=playbook.current_version,
        health=playbook.health,
        provenance=playbook.provenance,
        created_at=playbook.created_at,
    )


def _version_out(version: PlaybookVersion) -> PlaybookVersionOut:
    return PlaybookVersionOut(
        id=version.id,
        playbook_id=version.playbook_id,
        version=version.version,
        spec=version.spec,
        code=version.code,
        tests=version.tests,
        canary=version.canary,
        active=version.active,
        checksum=version.checksum,
        created_by_run=version.created_by_run,
        created_at=version.created_at,
    )


def _artifact_out(row: SourceArtifact) -> ArtifactOut:
    return ArtifactOut(
        id=row.id,
        source_id=row.source_id,
        kind=row.kind,
        url=row.url,
        sha256=row.sha256,
        content_type=row.content_type,
        size=row.size,
        metadata=row.metadata_json,
        captured_at=row.captured_at,
        run_id=row.run_id,
        step=row.step,
    )


def _state_out(row: SourceState) -> SourceStateOut:
    return SourceStateOut(
        source_id=row.source_id,
        last_revision=row.last_revision,
        cursor=row.cursor,
        site_map=row.site_map,
        endpoints=row.endpoints,
        updated_at=row.updated_at,
    )


def _report_out(report: RunReport) -> RunReportOut:
    return RunReportOut.model_validate(report)


def _priority_out(row: SourcePriority) -> PriorityOut:
    return PriorityOut(space_id=row.space_id, kind=row.kind, source_ids=list(row.source_ids))


# --- sources ------------------------------------------------------------------------------------


@router.get("/sources", response_model=list[SourceOut], dependencies=[V4])
async def list_sources(
    user: AuthUser,
    session: DbSession,
    space_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[SourceOut]:
    """Sources of the user; optionally of one space, one status and one kind."""
    rows = await service.list_sources(
        session, user_id=user.id, space_id=space_id, status=status, kind=kind
    )
    return [_source_out(row) for row in rows]


@router.post("/sources", response_model=SourceOut, status_code=201, dependencies=[V4])
async def create_source(body: SourceCreateIn, user: AuthUser, session: DbSession) -> SourceOut:
    """Create a source. A duplicate identity answers 409, never a silent replace."""
    source = await service.create_source(
        session,
        user_id=user.id,
        space_id=body.space_id,
        title=body.title,
        entry_url=body.url,
        link=body.link,
        file_sha256=body.file_sha256,
        mailbox=body.mailbox,
        kinds=body.kinds,
        playbook_id=body.playbook_id,
        config=body.config,
        auth_ref=body.auth_ref,
    )
    await session.commit()
    return _source_out(source)


@router.get("/sources/{source_id}", response_model=SourceOut, dependencies=[V4])
async def get_source(source_id: str, user: AuthUser, session: DbSession) -> SourceOut:
    return _source_out(await service.get_source(session, user_id=user.id, source_id=source_id))


@router.patch("/sources/{source_id}", response_model=SourceOut, dependencies=[V4])
async def update_source(
    source_id: str, body: SourcePatch, user: AuthUser, session: DbSession
) -> SourceOut:
    source = await service.update_source(session, user_id=user.id, source_id=source_id, patch=body)
    await session.commit()
    return _source_out(source)


@router.post("/sources/{source_id}/disable", response_model=SourceOut, dependencies=[V4])
async def disable_source(source_id: str, user: AuthUser, session: DbSession) -> SourceOut:
    source = await service.disable_source(session, user_id=user.id, source_id=source_id)
    await session.commit()
    return _source_out(source)


@router.post("/sources/{source_id}/enable", response_model=SourceOut, dependencies=[V4])
async def enable_source(source_id: str, user: AuthUser, session: DbSession) -> SourceOut:
    source = await service.enable_source(session, user_id=user.id, source_id=source_id)
    await session.commit()
    return _source_out(source)


# --- runs ---------------------------------------------------------------------------------------


@router.post("/sources/{source_id}/probe", response_model=RunReportOut, dependencies=[V4])
async def probe_source(
    source_id: str,
    body: ProbeIn,
    user: AuthUser,
    session: DbSession,
    runner_builder: RunnerDep,
) -> RunReportOut:
    """Try a candidate spec/code (or the active version) without changing the source status."""
    runner = runner_builder(session, user.id)
    report = await runner.probe(
        source_id,
        spec=body.spec,
        code=body.code,
        language=body.language,
        version=body.version,
        persist=body.persist,
    )
    await session.commit()
    return _report_out(report)


@router.post("/sources/{source_id}/sync", response_model=RunReportOut, dependencies=[V4])
async def sync_source(
    source_id: str,
    body: SyncIn,
    user: AuthUser,
    session: DbSession,
    runner_builder: RunnerDep,
) -> RunReportOut:
    """Run the active (or the named) version; the runner records freshness and state itself."""
    runner = runner_builder(session, user.id)
    return _report_out(await runner.sync(source_id, version=body.version))


@router.post("/sources/{source_id}/canary", response_model=RunReportOut, dependencies=[V4])
async def canary_source(
    source_id: str,
    body: CanaryIn,
    user: AuthUser,
    session: DbSession,
    runner_builder: RunnerDep,
) -> RunReportOut:
    """Run a version as a canary; it becomes activatable only after a successful canary."""
    runner = runner_builder(session, user.id)
    return _report_out(await runner.canary(source_id, version=body.version))


@router.post("/sources/{source_id}/repair", response_model=RunReportOut, dependencies=[V4])
async def repair_source(
    source_id: str,
    body: RepairIn,
    user: AuthUser,
    session: DbSession,
    runner_builder: RunnerDep,
) -> RunReportOut:
    """Run a candidate spec/code or a version; on `activate` persist it as the new version first.

    The candidate is written as an inactive version, its canary result is stored, and only a
    successful run is activated. A failed candidate stays for inspection and changes nothing.
    """
    runner = runner_builder(session, user.id)
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if body.spec is None and body.code is None and body.version is None:
        return _report_out(await runner.repair(source_id))
    if body.spec is not None or body.code is not None:
        if source.playbook_id is None:
            raise BotaiError(
                ErrorCode.CONFLICT, "У источника нет playbook: сначала подключите источник"
            )
        version_row = await playbooks.add_playbook_version(
            session,
            user_id=user.id,
            playbook_id=source.playbook_id,
            spec=body.spec,
            code=body.code,
        )
        report = await runner.canary(source_id, version=version_row.version)
        if body.activate and report.ok:
            await playbooks.activate_playbook_version(
                session,
                user_id=user.id,
                playbook_id=source.playbook_id,
                version=version_row.version,
            )
        await session.commit()
        return _report_out(report)
    assert body.version is not None
    if source.playbook_id is None:
        raise BotaiError(ErrorCode.CONFLICT, "У источника нет playbook")
    report = await runner.repair(source_id, version=body.version)
    if body.activate and report.ok:
        await playbooks.activate_playbook_version(
            session, user_id=user.id, playbook_id=source.playbook_id or "", version=body.version
        )
    await session.commit()
    return _report_out(report)


@router.get(
    "/sources/{source_id}/sync/stream",
    dependencies=[V4],
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def sync_stream(
    source_id: str,
    user: AuthUser,
    services: ServicesDep,
    runner_builder: RunnerDep,
    version: int | None = Query(default=None, ge=1),
) -> EventSourceResponse:
    """SSE progress of one sync: `status`, `read` per endpoint, then `done` or `error`."""

    async def events() -> AsyncIterator[dict[str, str]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def progress(event: Mapping[str, Any]) -> None:
            await queue.put(dict(event))

        async def work() -> None:
            try:
                async with services.db() as session:
                    runner = runner_builder(session, user.id)
                    report = await runner.sync(source_id, version=version, progress=progress)
                await queue.put(
                    {
                        "event": "done",
                        "report": RunReportOut.model_validate(report).model_dump(
                            mode="json", by_alias=True
                        ),
                    }
                )
            except BotaiError as exc:
                await queue.put({"event": "error", "code": exc.code.value, "detail": exc.detail})
            except Exception as exc:  # the stream boundary reports, it does not crash
                await queue.put(
                    {"event": "error", "code": "internal_error", "detail": str(exc)[:500]}
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(work())
        try:
            while (item := await queue.get()) is not None:
                name = str(item.pop("event", "message"))
                yield {
                    "event": name,
                    "data": json.dumps(item, ensure_ascii=False, default=str),
                }
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return EventSourceResponse(events(), ping=15)


# --- playbooks ----------------------------------------------------------------------------------


@router.post(
    "/sources/{source_id}/playbook",
    response_model=PlaybookOut,
    status_code=201,
    dependencies=[V4],
)
async def create_source_playbook(
    source_id: str, body: PlaybookCreateIn, user: AuthUser, session: DbSession
) -> PlaybookOut:
    """Create and attach a playbook record; versions are added separately."""
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if source.playbook_id is not None:
        raise BotaiError(ErrorCode.CONFLICT, "У источника уже есть playbook")
    playbook = await playbooks.create_playbook(
        session,
        user_id=user.id,
        title=body.title,
        university_id=body.university_id or source.university_id,
        language=body.language,
        shared=body.shared,
    )
    await service.update_source(
        session,
        user_id=user.id,
        source_id=source.id,
        patch=SourcePatch(playbook_id=playbook.id),
    )
    await session.commit()
    return _playbook_out(playbook)


@router.get("/sources/{source_id}/playbook", response_model=PlaybookOut, dependencies=[V4])
async def get_source_playbook(source_id: str, user: AuthUser, session: DbSession) -> PlaybookOut:
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if source.playbook_id is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "У источника нет playbook")
    return _playbook_out(
        await playbooks.get_playbook(session, user_id=user.id, playbook_id=source.playbook_id)
    )


@router.get(
    "/sources/{source_id}/playbook/versions",
    response_model=list[PlaybookVersionOut],
    dependencies=[V4],
)
async def list_source_versions(
    source_id: str, user: AuthUser, session: DbSession
) -> list[PlaybookVersionOut]:
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if source.playbook_id is None:
        return []
    rows = await playbooks.list_playbook_versions(
        session, user_id=user.id, playbook_id=source.playbook_id
    )
    return [_version_out(row) for row in rows]


@router.post(
    "/sources/{source_id}/playbook/versions",
    response_model=PlaybookVersionOut,
    status_code=201,
    dependencies=[V4],
)
async def add_source_version(
    source_id: str, body: PlaybookVersionIn, user: AuthUser, session: DbSession
) -> PlaybookVersionOut:
    """Add an inactive version; `activate` requires a successful canary of that version."""
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if source.playbook_id is None:
        raise BotaiError(ErrorCode.CONFLICT, "У источника нет playbook")
    row = await playbooks.add_playbook_version(
        session,
        user_id=user.id,
        playbook_id=source.playbook_id,
        spec=body.spec,
        code=body.code,
        tests=body.tests,
        canary=body.canary,
    )
    if body.activate:
        await playbooks.activate_playbook_version(
            session, user_id=user.id, playbook_id=source.playbook_id, version=row.version
        )
    await session.commit()
    return _version_out(row)


@router.post(
    "/sources/{source_id}/playbook/versions/{version}/activate",
    response_model=PlaybookVersionOut,
    dependencies=[V4],
)
async def activate_source_version(
    source_id: str,
    version: int,
    user: AuthUser,
    session: DbSession,
    require_canary: bool = Query(default=True),
) -> PlaybookVersionOut:
    source = await service.get_source(session, user_id=user.id, source_id=source_id)
    if source.playbook_id is None:
        raise BotaiError(ErrorCode.CONFLICT, "У источника нет playbook")
    row = await playbooks.activate_playbook_version(
        session,
        user_id=user.id,
        playbook_id=source.playbook_id,
        version=version,
        require_canary=require_canary,
    )
    await session.commit()
    return _version_out(row)


# --- artifacts and state ------------------------------------------------------------------------


@router.get(
    "/sources/{source_id}/artifacts",
    response_model=list[ArtifactOut],
    dependencies=[V4],
)
async def list_source_artifacts(
    source_id: str,
    user: AuthUser,
    session: DbSession,
    kind: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ArtifactOut]:
    rows = await artifacts.list_artifacts(
        session, user_id=user.id, source_id=source_id, kind=kind, limit=limit
    )
    return [_artifact_out(row) for row in rows]


@router.get("/sources/{source_id}/state", response_model=SourceStateOut, dependencies=[V4])
async def get_source_state(source_id: str, user: AuthUser, session: DbSession) -> SourceStateOut:
    row = await state.get_source_state(session, user_id=user.id, source_id=source_id)
    if row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Источник ещё не читался")
    return _state_out(row)


# --- priorities ---------------------------------------------------------------------------------


@router.get("/spaces/{space_id}/priorities/{kind}", response_model=PriorityOut, dependencies=[V4])
async def get_priority(space_id: str, kind: str, user: AuthUser, session: DbSession) -> PriorityOut:
    source_ids = await priorities.get_priority(
        session, user_id=user.id, space_id=space_id, kind=kind
    )
    return PriorityOut(space_id=space_id, kind=kind, source_ids=source_ids)


@router.put("/spaces/{space_id}/priorities/{kind}", response_model=PriorityOut, dependencies=[V4])
async def set_priority(
    space_id: str,
    kind: str,
    body: PriorityIn,
    user: AuthUser,
    session: DbSession,
) -> PriorityOut:
    row = await priorities.set_priority(
        session,
        user_id=user.id,
        space_id=space_id,
        kind=kind,
        source_ids=body.source_ids,
    )
    await session.commit()
    return _priority_out(row)


__all__ = ["RunnerBuilder", "get_runner_builder", "require_sources_v4", "router"]
