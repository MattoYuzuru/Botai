"""Owner: W1. Which source of a university decides its schedule (FR-054).

A university (a space) can have several live sources with schedule events: a cabinet and a
calendar. One of them is the primary: the student's choice (`spaces.primary_schedule_connection_id`)
or, when nothing is chosen, the oldest live source that has schedule events, so the answer is
always defined. A disabled source is never primary and is passed over; a deleted one clears the
choice by the foreign key. Everything else with schedule events is secondary.

Reads only; setting the choice is `spaces.service.set_primary_source`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Connection, ScheduleEvent, Space
from botai.domain.merge import Coverage

ScheduleRole = Literal["primary", "secondary"]


class LiveConnection(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def space_id(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ScheduleSources:
    """The schedule sources of one university, the primary first, the others oldest first."""

    space_id: str
    time_zone: str
    order: tuple[str, ...]
    chosen: bool  # the first of `order` is the student's choice, not the default

    @property
    def primary(self) -> str | None:
        return self.order[0] if self.order else None

    def role_of(self, connection_id: str) -> ScheduleRole | None:
        if connection_id not in self.order:
            return None
        return "primary" if connection_id == self.order[0] else "secondary"


async def schedule_sources(
    session: AsyncSession, user_id: str, live: Iterable[LiveConnection]
) -> dict[str, ScheduleSources]:
    """Per space of the live (not disabled) connections given: its schedule sources in order.
    A space with no source that has schedule events (and no choice) is absent."""
    by_space: dict[str, list[LiveConnection]] = {}
    for conn in live:
        by_space.setdefault(conn.space_id, []).append(conn)
    if not by_space:
        return {}
    every_id = [c.id for conns in by_space.values() for c in conns]
    with_events = set(
        await session.scalars(
            select(ScheduleEvent.connection_id)
            .where(ScheduleEvent.user_id == user_id, ScheduleEvent.connection_id.in_(every_id))
            .distinct()
        )
    )
    spaces = (
        await session.execute(
            select(Space.id, Space.time_zone, Space.primary_schedule_connection_id).where(
                Space.user_id == user_id, Space.id.in_(list(by_space))
            )
        )
    ).all()
    out: dict[str, ScheduleSources] = {}
    for space_id, time_zone, pointer in spaces:
        conns = sorted(by_space[space_id], key=lambda c: (c.created_at, c.id))
        ids = [c.id for c in conns]
        chosen = pointer if pointer in ids else None
        rest = [i for i in ids if i in with_events and i != chosen]
        order = tuple(([chosen] if chosen else []) + rest)
        if order:
            out[space_id] = ScheduleSources(space_id, time_zone, order, chosen is not None)
    return out


async def coverage_of(
    session: AsyncSession, user_id: str, connection_ids: Iterable[str]
) -> dict[str, Coverage]:
    """The span of schedule events each source has stored (all time), for the sources that have any."""
    ids = list(connection_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ScheduleEvent.connection_id,
            func.min(ScheduleEvent.starts_at),
            func.max(ScheduleEvent.ends_at),
        )
        .where(ScheduleEvent.user_id == user_id, ScheduleEvent.connection_id.in_(ids))
        .group_by(ScheduleEvent.connection_id)
    )
    return {cid: Coverage(first, last) for cid, first, last in rows.all()}


async def live_schedule_sources(
    session: AsyncSession, user_id: str, *, space_ids: Iterable[str] | None = None
) -> dict[str, ScheduleSources]:
    """`schedule_sources` over the live connections of the student (of the given spaces)."""
    stmt = select(Connection.id, Connection.space_id, Connection.created_at).where(
        Connection.user_id == user_id, Connection.status != "disabled"
    )
    if space_ids is not None:
        stmt = stmt.where(Connection.space_id.in_(list(space_ids)))
    live = [_Live(*row) for row in (await session.execute(stmt)).all()]
    return await schedule_sources(session, user_id, live)


async def schedule_roles(
    session: AsyncSession, user_id: str, *, space_ids: Iterable[str] | None = None
) -> dict[str, ScheduleRole]:
    """The role of every live source that has schedule events (or is chosen), by connection id."""
    roles: dict[str, ScheduleRole] = {}
    for sources in (await live_schedule_sources(session, user_id, space_ids=space_ids)).values():
        for connection_id in sources.order:
            role = sources.role_of(connection_id)
            if role:
                roles[connection_id] = role
    return roles


@dataclass(frozen=True, slots=True)
class _Live:
    id: str
    space_id: str
    created_at: datetime
