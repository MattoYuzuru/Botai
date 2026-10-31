"""Owner: W1. Which source of a university decides its schedule (FR-054).

A university (a space) can have several live sources with schedule events: a cabinet and a
calendar. One of them is the primary: the student's choice (`spaces.primary_schedule_connection_id`),
which is respected whatever the state of that source, or, when nothing is chosen, the oldest live
source that has schedule events, healthy ones (`HEALTHY`) before those that need a login or are
broken, so the answer is always defined. A disabled source is never primary and is passed over; a
deleted one clears the choice by the foreign key. Everything else with schedule events is secondary.

Reads only; setting the choice is `spaces.service.set_primary_source`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Connection, ScheduleEvent, Space
from botai.domain.merge import COVERAGE_GAP, NEARBY, Coverage, is_regular

ScheduleRole = Literal["primary", "secondary"]
# Sources whose data is current enough to decide by default; the others need the student first.
HEALTHY = frozenset({"active", "syncing", "stale"})
# Regular classes are read this far beyond the window asked for, to know where a stretch of them ends.
COVERAGE_MARGIN = NEARBY + COVERAGE_GAP + timedelta(days=1)


class LiveConnection(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def space_id(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def status(self) -> str: ...


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
    # One EXISTS per connection (an index probe), not a pass over every stored class.
    with_events = set(
        await session.scalars(
            select(Connection.id).where(
                Connection.id.in_(every_id),
                exists().where(
                    ScheduleEvent.connection_id == Connection.id, ScheduleEvent.user_id == user_id
                ),
            )
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
        conns = sorted(
            by_space[space_id], key=lambda c: (c.status not in HEALTHY, c.created_at, c.id)
        )
        ids = [c.id for c in conns]
        chosen = pointer if pointer in ids else None
        rest = [i for i in ids if i in with_events and i != chosen]
        order = tuple(([chosen] if chosen else []) + rest)
        if order:
            out[space_id] = ScheduleSources(space_id, time_zone, order, chosen is not None)
    return out


async def coverage_of(
    session: AsyncSession,
    user_id: str,
    connection_ids: Iterable[str],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Coverage]:
    """Where each source's regular classes reach around [start, end): a bounded read, joined into
    stretches (`Coverage.of_classes`). An exam, a consultation or a reminder does not count: one
    far exam would otherwise make every day up to it look "missing" from the source."""
    ids = list(connection_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ScheduleEvent.connection_id,
            ScheduleEvent.starts_at,
            ScheduleEvent.ends_at,
            ScheduleEvent.kind,
            ScheduleEvent.title,
        ).where(
            ScheduleEvent.user_id == user_id,
            ScheduleEvent.connection_id.in_(ids),
            ScheduleEvent.starts_at >= start - COVERAGE_MARGIN,
            ScheduleEvent.starts_at < end + COVERAGE_MARGIN,
        )
    )
    regular: dict[str, list[tuple[datetime, datetime]]] = {}
    for connection_id, first, last, kind, title in rows.all():
        if connection_id is None:  # rows are selected by connection_id.in_(ids), never NULL
            continue
        if is_regular(kind, title, first, last):
            regular.setdefault(connection_id, []).append((first, last))
    found = {cid: Coverage.of_classes(classes) for cid, classes in regular.items()}
    return {cid: coverage for cid, coverage in found.items() if coverage is not None}


async def live_schedule_sources(
    session: AsyncSession, user_id: str, *, space_ids: Iterable[str] | None = None
) -> dict[str, ScheduleSources]:
    """`schedule_sources` over the live connections of the student (of the given spaces)."""
    stmt = select(
        Connection.id, Connection.space_id, Connection.created_at, Connection.status
    ).where(Connection.user_id == user_id, Connection.status != "disabled")
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
    status: str
