"""Owner: W1. Tenant-scoped reads of the domain. `user_id` is always the first argument (NFR-001).

W5's chat tools and W1's routers use these; W3's normalizer writes the tables directly.

Rules shared by every read:

* the user's own rows only, and only from connections that are not `disabled` (the student
  switched them off: their data leaves every view);
* `space_id`, when given, must be the user's space, otherwise `not_found`: a caller that guesses a
  foreign id learns nothing and an assistant that hallucinates one is told so instead of being
  handed an empty list;
* the schedule of one university read from several sources is merged here, on every read and
  nowhere else (`domain.merge`, FR-054): stored rows stay per source;
* every row carries provenance (FR-012, NFR-007). `stale` is true when the connection needs
  attention (`stale`, `needs_reauth`, `repairing`) or when its last sync is older than the
  freshness of its adapter spec (`staleAfterMinutes`, see `sources.schedule`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    AdapterSpecRow,
    Assignment,
    Connection,
    Course,
    Deadline,
    Document,
    Grade,
    Location,
    NewsItem,
    Person,
    ScheduleEvent,
    Space,
)
from botai.domain import primary
from botai.domain.merge import NEARBY, SpacePlan, merge_events
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    EventAttribute,
    EventLink,
    EventSource,
    GradeOut,
    NewsOut,
    PersonOut,
    Provenance,
    ScheduleEventOut,
    TodayOut,
)
from botai.sources.schedule import freshness_of, is_stale
from botai.spaces.views import display_title

# Section kinds of the API (FR-014) and the tables that feed them, in display order.
_SECTION_TABLES = (
    ("courses", Course),
    ("scheduleEvents", ScheduleEvent),
    ("deadlines", Deadline),
    ("assignments", Assignment),
    ("grades", Grade),
    ("people", Person),
    ("locations", Location),
    ("news", NewsItem),
)

NEEDS_ATTENTION = frozenset({"stale", "needs_reauth", "repairing"})
# "Not ok" on the home screen: the student may have to do or wait for something.
ATTENTION_STATUSES = frozenset({"stale", "needs_reauth", "waiting_user", "repairing", "error"})
DONE_STATES = frozenset({"submitted", "graded"})

MAX_ROWS = 2000
TODAY_DEADLINES = 10
DEFAULT_TZ = "Europe/Moscow"

# Rows are read this far beyond the window so that a class near its edge still finds, in the
# primary source, the same class a week away (`merge.NEARBY`) and the one that overlaps it.
MERGE_MARGIN = NEARBY + timedelta(days=1)

_EVENT_KINDS = ("lecture", "seminar", "lab", "exam", "consultation", "other")
_EVENT_FORMATS = ("offline", "online", "hybrid", "unknown")
_DEADLINE_STATES = ("open", "submitted", "graded", "missed", "unknown")


@dataclass(frozen=True, slots=True)
class _Conn:
    id: str
    space_id: str
    status: str
    stale: bool
    created_at: datetime
    title: str


def _choice(value: str | None, allowed: tuple[str, ...], default: str) -> Any:
    return value if value in allowed else default


def _aware(value: datetime) -> datetime:
    """A naive datetime from a caller is taken as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _require_space(session: AsyncSession, user_id: str, space_id: str | None) -> None:
    if space_id is None:
        return
    found = await session.scalar(
        select(Space.id).where(Space.id == space_id, Space.user_id == user_id)
    )
    if found is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")


async def _connections(
    session: AsyncSession, user_id: str, *, now: datetime, space_id: str | None = None
) -> dict[str, _Conn]:
    stmt = (
        select(Connection, AdapterSpecRow.spec)
        .outerjoin(AdapterSpecRow, AdapterSpecRow.id == Connection.spec_id)
        .where(Connection.user_id == user_id, Connection.status != "disabled")
        .order_by(Connection.created_at, Connection.id)
    )
    if space_id is not None:
        stmt = stmt.where(Connection.space_id == space_id)
    out: dict[str, _Conn] = {}
    for conn, spec in (await session.execute(stmt)).all():
        stale = conn.status in NEEDS_ATTENTION or is_stale(
            now, conn.last_sync_at, freshness_of(spec or {})
        )
        out[conn.id] = _Conn(
            conn.id, conn.space_id, conn.status, stale, conn.created_at, display_title(conn)
        )
    return out


def _prov(row: Any, conn: _Conn) -> Provenance:
    return Provenance(
        connection_id=conn.id,
        space_id=conn.space_id,
        source_url=row.source_url,
        open_url=row.open_url,
        fetched_at=row.fetched_at,
        stale=conn.stale,
    )


async def _course_titles(
    session: AsyncSession, user_id: str, pairs: Iterable[tuple[str, str | None]]
) -> dict[tuple[str, str], str]:
    wanted = {(c, e) for c, e in pairs if e}
    if not wanted:
        return {}
    rows = await session.execute(
        select(Course.connection_id, Course.external_id, Course.title).where(
            Course.user_id == user_id,
            Course.connection_id.in_({c for c, _ in wanted}),
            Course.external_id.in_({e for _, e in wanted}),
        )
    )
    return {(c, e): t for c, e, t in rows.all() if (c, e) in wanted}


async def _assignment_descriptions(
    session: AsyncSession, user_id: str, pairs: Iterable[tuple[str, str | None]]
) -> dict[tuple[str, str], str]:
    """Task text of a deadline comes from its assignment, when the source gave one."""
    wanted = {(c, e) for c, e in pairs if e}
    if not wanted:
        return {}
    rows = await session.execute(
        select(Assignment.connection_id, Assignment.external_id, Assignment.description).where(
            Assignment.user_id == user_id,
            Assignment.connection_id.in_({c for c, _ in wanted}),
            Assignment.external_id.in_({e for _, e in wanted}),
            Assignment.description.is_not(None),
        )
    )
    return {(c, e): d for c, e, d in rows.all() if (c, e) in wanted and d}


# --- sections -----------------------------------------------------------------------------------


async def section_counts(
    session: AsyncSession, user_id: str, space_id: str
) -> list[tuple[str, int]]:
    """How many rows of each kind the live connections of one space have (documents: the space)."""
    connection_ids = select(Connection.id).where(
        Connection.user_id == user_id,
        Connection.space_id == space_id,
        Connection.status != "disabled",
    )
    counts: list[tuple[str, int]] = []
    for kind, model in _SECTION_TABLES:
        n = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id, model.connection_id.in_(connection_ids))
        )
        counts.append((kind, int(n or 0)))
    docs = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.user_id == user_id, Document.space_id == space_id)
    )
    counts.append(("documents", int(docs or 0)))
    return counts


# --- schedule -----------------------------------------------------------------------------------


def _mark_conflicts(events: list[ScheduleEventOut]) -> None:
    """Fills `conflicts_with`: overlapping events of *different* spaces (FR-013). Touching ends
    (one class ends when the next starts) are not a conflict."""
    ordered = sorted(events, key=lambda e: (e.starts_at, e.ends_at, e.id))
    active: list[ScheduleEventOut] = []
    for ev in ordered:
        active = [a for a in active if a.ends_at > ev.starts_at]
        for other in active:
            if other.provenance.space_id != ev.provenance.space_id:
                ev.conflicts_with.append(other.id)
                other.conflicts_with.append(ev.id)
        active.append(ev)
    for ev in ordered:
        ev.conflicts_with.sort()


async def _merge_plans(
    session: AsyncSession, user_id: str, conns: dict[str, _Conn]
) -> dict[str, SpacePlan]:
    """How each university with more than one schedule source is merged (`domain.merge`)."""
    sources = await primary.schedule_sources(session, user_id, conns.values())
    several = {sid: found for sid, found in sources.items() if len(found.order) > 1}
    if not several:
        return {}
    coverage = await primary.coverage_of(
        session, user_id, [found.order[0] for found in several.values()]
    )
    return {
        sid: SpacePlan(
            order=found.order,
            titles={cid: conns[cid].title for cid in found.order},
            zone=_zone(found.time_zone),
            coverage=coverage.get(found.order[0]),
        )
        for sid, found in several.items()
    }


async def _event_outs(
    session: AsyncSession, user_id: str, rows: Sequence[ScheduleEvent], conns: dict[str, _Conn]
) -> list[ScheduleEventOut]:
    titles = await _course_titles(
        session, user_id, ((r.connection_id, r.course_external_id) for r in rows)
    )
    return [
        ScheduleEventOut(
            id=r.id,
            title=r.title,
            kind=_choice(r.kind, _EVENT_KINDS, "other"),
            starts_at=r.starts_at,
            ends_at=r.ends_at,
            format=_choice(r.format, _EVENT_FORMATS, "unknown"),
            location=r.location_text,
            teacher=r.teacher_text,
            course_title=titles.get((r.connection_id, r.course_external_id or "")),
            url=r.url,
            kind_text=r.kind_text,
            address=r.address,
            group=r.group_text,
            lesson_number=r.lesson_number,
            links=[EventLink(**link) for link in r.links],
            attributes=[EventAttribute(**attribute) for attribute in r.attributes],
            changed=r.changed,
            conflicts_with=[],
            provenance=_prov(r, conns[r.connection_id]),
            sources=[
                EventSource(connection_id=r.connection_id, title=conns[r.connection_id].title)
            ],
        )
        for r in rows
    ]


async def _events(
    session: AsyncSession,
    user_id: str,
    conns: dict[str, _Conn],
    *,
    start: datetime,
    end: datetime,
) -> list[ScheduleEventOut]:
    """Events overlapping [start, end) with conflicts computed against *all* live connections,
    including events just outside the window that overlap one inside it. Classes of one university
    that several of its sources describe are merged first (FR-054): the primary source decides."""
    if not conns:
        return []
    overlap = (
        ScheduleEvent.user_id == user_id,
        ScheduleEvent.connection_id.in_(list(conns)),
    )
    bounds = (
        await session.execute(
            select(func.min(ScheduleEvent.starts_at), func.max(ScheduleEvent.ends_at)).where(
                *overlap, ScheduleEvent.starts_at < end, ScheduleEvent.ends_at > start
            )
        )
    ).one()
    # min/max over an empty set are NULL, which the driver typing does not admit
    first, last = cast("tuple[datetime | None, datetime | None]", tuple(bounds))
    if first is None or last is None:  # nothing in the window
        return []
    plans = await _merge_plans(session, user_id, conns)
    margin = MERGE_MARGIN if plans else timedelta(0)
    lo, hi = min(first, start) - margin, max(last, end) + margin
    rows = (
        await session.scalars(
            select(ScheduleEvent)
            .where(*overlap, ScheduleEvent.starts_at < hi, ScheduleEvent.ends_at > lo)
            .order_by(ScheduleEvent.starts_at, ScheduleEvent.ends_at, ScheduleEvent.id)
            .limit(MAX_ROWS)
        )
    ).all()
    events = merge_events(await _event_outs(session, user_id, rows, conns), plans)
    _mark_conflicts(events)
    return [e for e in events if e.starts_at < end and e.ends_at > start]


async def source_events(
    session: AsyncSession,
    user_id: str,
    connection_id: str,
    *,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> list[ScheduleEventOut]:
    """The classes of ONE source overlapping [start, end), as the source has them: no merge, no
    conflicts (the agent compares two sources with it). A disabled or foreign source is empty."""
    now = now or SystemClock().now()
    conns = await _connections(session, user_id, now=now)
    if connection_id not in conns:
        return []
    rows = (
        await session.scalars(
            select(ScheduleEvent)
            .where(
                ScheduleEvent.user_id == user_id,
                ScheduleEvent.connection_id == connection_id,
                ScheduleEvent.starts_at < _aware(end),
                ScheduleEvent.ends_at > _aware(start),
            )
            .order_by(ScheduleEvent.starts_at, ScheduleEvent.ends_at, ScheduleEvent.id)
            .limit(MAX_ROWS)
        )
    ).all()
    return await _event_outs(session, user_id, rows, conns)


async def list_events(
    session: AsyncSession,
    user_id: str,
    *,
    start: datetime,
    end: datetime,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[ScheduleEventOut]:
    """Classes overlapping [start, end), earliest first. `conflicts_with` lists the ids of
    overlapping classes from other spaces, also when `space_id` hides those from the result."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now)
    events = await _events(session, user_id, conns, start=_aware(start), end=_aware(end))
    if space_id is not None:
        events = [e for e in events if e.provenance.space_id == space_id]
    return events


# --- deadlines ----------------------------------------------------------------------------------


async def list_deadlines(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    include_done: bool = False,
    now: datetime | None = None,
) -> list[DeadlineOut]:
    """Deadlines by due date (`effective_due_at`, else `due_at`), undated ones last. Submitted and
    graded work is left out unless `include_done`."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now, space_id=space_id)
    return await _deadlines(session, user_id, conns, include_done=include_done)


async def _deadlines(
    session: AsyncSession,
    user_id: str,
    conns: dict[str, _Conn],
    *,
    include_done: bool,
    due_from: datetime | None = None,
    limit: int = MAX_ROWS,
) -> list[DeadlineOut]:
    if not conns:
        return []
    due = func.coalesce(Deadline.effective_due_at, Deadline.due_at)
    stmt = select(Deadline).where(
        Deadline.user_id == user_id, Deadline.connection_id.in_(list(conns))
    )
    if not include_done:
        stmt = stmt.where(Deadline.state.not_in(sorted(DONE_STATES)))
    if due_from is not None:
        stmt = stmt.where(due >= due_from)
    rows = (
        await session.scalars(stmt.order_by(due.asc().nulls_last(), Deadline.id).limit(limit))
    ).all()
    titles = await _course_titles(
        session, user_id, ((r.connection_id, r.course_external_id) for r in rows)
    )
    texts = await _assignment_descriptions(
        session, user_id, ((r.connection_id, r.assignment_external_id) for r in rows)
    )
    return [
        DeadlineOut(
            id=r.id,
            title=r.title,
            due_at=r.due_at,
            effective_due_at=r.effective_due_at,
            state=_choice(r.state, _DEADLINE_STATES, "unknown"),
            course_title=titles.get((r.connection_id, r.course_external_id or "")),
            description=texts.get((r.connection_id, r.assignment_external_id or "")),
            url=r.url,
            provenance=_prov(r, conns[r.connection_id]),
        )
        for r in rows
    ]


# --- the rest of the domain ---------------------------------------------------------------------


async def list_courses(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[CourseOut]:
    """Courses the sources still list (archived ones, which vanished from the source, are out)."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now, space_id=space_id)
    if not conns:
        return []
    rows = await session.scalars(
        select(Course)
        .where(
            Course.user_id == user_id,
            Course.connection_id.in_(list(conns)),
            Course.archived.is_(False),
        )
        .order_by(Course.title, Course.id)
        .limit(MAX_ROWS)
    )
    return [
        CourseOut(
            id=r.id,
            title=r.title,
            code=r.code,
            term=r.term,
            url=r.url,
            provenance=_prov(r, conns[r.connection_id]),
        )
        for r in rows
    ]


async def list_grades(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[GradeOut]:
    """Newest grades first; ungraded-date rows last."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now, space_id=space_id)
    if not conns:
        return []
    rows = (
        await session.scalars(
            select(Grade)
            .where(Grade.user_id == user_id, Grade.connection_id.in_(list(conns)))
            .order_by(Grade.graded_at.desc().nulls_last(), Grade.title, Grade.id)
            .limit(MAX_ROWS)
        )
    ).all()
    titles = await _course_titles(
        session, user_id, ((r.connection_id, r.course_external_id) for r in rows)
    )
    return [
        GradeOut(
            id=r.id,
            title=r.title,
            value=r.value,
            scale=r.scale,
            weight=r.weight,
            course_title=titles.get((r.connection_id, r.course_external_id or "")),
            graded_at=r.graded_at,
            provenance=_prov(r, conns[r.connection_id]),
        )
        for r in rows
    ]


async def list_people(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[PersonOut]:
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now, space_id=space_id)
    if not conns:
        return []
    rows = await session.scalars(
        select(Person)
        .where(Person.user_id == user_id, Person.connection_id.in_(list(conns)))
        .order_by(Person.name, Person.id)
        .limit(MAX_ROWS)
    )
    return [
        PersonOut(
            id=r.id,
            name=r.name,
            role=r.role,
            email=r.email,
            provenance=_prov(r, conns[r.connection_id]),
        )
        for r in rows
    ]


async def list_news(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[NewsOut]:
    """Newest first."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    conns = await _connections(session, user_id, now=now, space_id=space_id)
    if not conns:
        return []
    rows = await session.scalars(
        select(NewsItem)
        .where(NewsItem.user_id == user_id, NewsItem.connection_id.in_(list(conns)))
        .order_by(NewsItem.published_at.desc(), NewsItem.id)
        .limit(MAX_ROWS)
    )
    return [
        NewsOut(
            id=r.id,
            title=r.title,
            body=r.body,
            published_at=r.published_at,
            url=r.url,
            provenance=_prov(r, conns[r.connection_id]),
        )
        for r in rows
    ]


# --- home screen --------------------------------------------------------------------------------


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TZ)


async def today(session: AsyncSession, user_id: str, *, now: datetime | None = None) -> TodayOut:
    """Today's classes across spaces (each space's own calendar day), the nearest open deadlines
    and the connections that need attention. `date` is today in the oldest space's time zone."""
    now = _aware(now or SystemClock().now())
    spaces = (
        await session.execute(
            select(Space.id, Space.time_zone)
            .where(Space.user_id == user_id)
            .order_by(Space.created_at, Space.id)
        )
    ).all()
    zones = {sid: _zone(tz) for sid, tz in spaces}
    home = zones[spaces[0][0]] if spaces else _zone(None)

    def day_of(zone: ZoneInfo) -> tuple[datetime, datetime]:
        local = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return local, local + timedelta(days=1)

    windows = {sid: day_of(z) for sid, z in zones.items()}
    conns = await _connections(session, user_id, now=now)

    events: list[ScheduleEventOut] = []
    if windows and conns:
        lo = min(w[0] for w in windows.values())
        hi = max(w[1] for w in windows.values())
        events = [
            e
            for e in await _events(session, user_id, conns, start=lo, end=hi)
            if e.starts_at < windows[e.provenance.space_id][1]
            and e.ends_at > windows[e.provenance.space_id][0]
        ]

    deadlines = await _deadlines(
        session,
        user_id,
        conns,
        include_done=False,
        due_from=day_of(home)[0],
        limit=TODAY_DEADLINES,
    )
    attention = [c.id for c in conns.values() if c.status in ATTENTION_STATUSES or c.stale]
    return TodayOut(
        date=now.astimezone(home).date().isoformat(),
        events=events,
        deadlines=deadlines,
        attention=attention,
    )
