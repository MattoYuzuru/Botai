"""Owner-only API of the panels of «Учёба» (D-039). The tenant predicate is on every query."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.api.deps import ClockDep
from botai.auth.deps import AuthUser
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Panel, Space, University
from botai.db.session import DbSession
from botai.panels import service
from botai.panels.data import DataResolver
from botai.panels.schemas import PanelOut, PanelPatch, PanelSpaceOut, parse_spec
from botai.sources.v4.router import require_sources_v4

router = APIRouter(prefix="/api/v1", tags=["panels"], dependencies=[Depends(require_sources_v4)])


async def _out(
    resolver: DataResolver | None, panel: Panel, space: Space, university: University
) -> PanelOut | None:
    """The API form of a panel, or None when its stored spec no longer validates (a client must
    never meet a spec it cannot read). Data is read for a panel that is shown or proposed."""
    try:
        spec = parse_spec(panel.spec)
    except BotaiError:
        return None
    wanted = panel.status == "proposed" or (panel.status == "active" and panel.visible)
    data = await resolver.resolve(panel) if resolver is not None and wanted else None
    return PanelOut.model_validate(
        {
            "id": panel.id,
            "key": panel.key,
            "type": panel.type,
            "spec": spec,
            "status": panel.status,
            "visible": panel.visible,
            "hidden_in_all": panel.hidden_in_all,
            "position": panel.position,
            "origin": panel.origin,
            "source_id": panel.source_id,
            "created_at": panel.created_at,
            "updated_at": panel.updated_at,
            "space": PanelSpaceOut(
                id=space.id,
                title=space.title,
                short_title=university.short_title,
                accent_hue=space.accent_hue,
            ),
            "data": data,
        }
    )


async def _one(
    session: AsyncSession, user_id: str, panel: Panel, resolver: DataResolver
) -> PanelOut:
    row = (
        await session.execute(
            select(Space, University)
            .join(University, University.id == Space.university_id)
            .where(Space.id == panel.space_id, Space.user_id == user_id)
        )
    ).one_or_none()
    out = await _out(resolver, panel, row[0], row[1]) if row is not None else None
    if out is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Панель не найдена")
    return out


@router.get("/panels", response_model=list[PanelOut])
async def list_panels(
    user: AuthUser,
    session: DbSession,
    clock: ClockDep,
    space_id: str | None = None,
    status: Literal["active", "proposed"] = "active",
) -> list[PanelOut]:
    """The panels of one university (or of all), in display order, with their resolved data.

    Active panels also include the ones the student switched off (`visible: false`, `data: null`),
    so the screen can offer the switch. The system panels a university's data deserves are made
    here on first read if the sync has not made them yet."""
    if space_id is not None:
        await service.get_space(session, user.id, space_id)
        space_ids = [space_id]
    else:
        space_ids = await service.space_ids(session, user.id)
    if status == "active":
        made = [
            key
            for sid in space_ids
            for key in await service.ensure_default_panels(session, user_id=user.id, space_id=sid)
        ]
        if made:
            await session.commit()
    resolver = DataResolver(session, user.id, clock.now())
    out: list[PanelOut] = []
    for panel, space, university in await service.list_panels(
        session, user_id=user.id, space_id=space_id, status=status
    ):
        item = await _out(resolver, panel, space, university)
        if item is not None:
            out.append(item)
    return out


@router.patch("/panels/{panel_id}", response_model=PanelOut)
async def patch_panel(
    panel_id: str, body: PanelPatch, user: AuthUser, session: DbSession, clock: ClockDep
) -> PanelOut:
    """The student's switches: `visible`, `hiddenInAll`, `position`."""
    panel = await service.update_panel(
        session,
        user_id=user.id,
        panel_id=panel_id,
        visible=body.visible,
        hidden_in_all=body.hidden_in_all,
        position=body.position,
    )
    out = await _one(session, user.id, panel, DataResolver(session, user.id, clock.now()))
    await session.commit()
    return out


@router.post("/panels/{panel_id}/accept", response_model=PanelOut)
async def accept_panel(
    panel_id: str, user: AuthUser, session: DbSession, clock: ClockDep
) -> PanelOut:
    """Accept a panel the agent proposed (or bring back a dismissed one): it becomes active and
    visible."""
    panel = await service.set_panel_status(
        session, user_id=user.id, panel_id=panel_id, status="active"
    )
    out = await _one(session, user.id, panel, DataResolver(session, user.id, clock.now()))
    await session.commit()
    return out


@router.post("/panels/{panel_id}/dismiss", response_model=PanelOut)
async def dismiss_panel(
    panel_id: str, user: AuthUser, session: DbSession, clock: ClockDep
) -> PanelOut:
    """Refuse a panel: it disappears and the agent does not propose the same key again."""
    panel = await service.set_panel_status(
        session, user_id=user.id, panel_id=panel_id, status="dismissed"
    )
    out = await _one(session, user.id, panel, DataResolver(session, user.id, clock.now()))
    await session.commit()
    return out
