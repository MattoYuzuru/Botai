"""Owner: W1. Tenant-scoped reads of the domain. `user_id` is always the first argument (NFR-001).

W5's chat tools and W1's routers use these; W3's normalizer writes the tables directly, and the v4
playbook runner writes the same tables with `source_id` provenance (`sources/v4/domain.py`).

Rules shared by every read (`docs/v4/03-sources-and-ux.md` §4, §6.1):

* the user's own rows only, and only from live sources: a v3 connection the student switched off,
  and a v4 source in `disabled`/`archived`, hide their data from every view — nothing is deleted;
* `space_id`, when given, must be the user's space, otherwise `not_found`: a caller that guesses a
  foreign id learns nothing and an assistant that hallucinates one is told so instead of being
  handed an empty list;
* the schedule of one university read from several sources is merged here, on every read and
  nowhere else (`domain.merge`, FR-054): stored rows stay per source, and the sources of both
  provenance paths take part, ordered by `source_priorities` (v4, FR-062) or the legacy
  `spaces.primary_schedule_connection_id` (v3), then by health and age;
* a result that involves a v4 source is deduplicated across sources by the natural key of the kind
  (title+date, code, name, url), the row of the higher-priority source kept; a v3-only result is
  returned exactly as before, so enabling v4 never rewrites the v3 answers;
* every row carries provenance (FR-012, NFR-007): `connectionId` for a v3 row, `sourceId` for a
  v4 one. `stale` is true when the source needs attention (`stale`, `needs_reauth`, `repairing` for
  a v3 connection; `stale`/`needs_reauth`/`error`/… for a v4 source) or when its last sync is
  older than the freshness of its adapter spec.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    Assignment,
    Connection,
    Course,
    Deadline,
    Document,
    Grade,
    Location,
    NewsItem,
    Person,
    ProfileField,
    RankingEntry,
    ScheduleEvent,
    Space,
)
from botai.domain import keys, overrides, sources
from botai.domain.merge import NEARBY, SpacePlan, merge_events
from botai.domain.schemas import (
    CourseOut,
    DeadlineGradeOut,
    DeadlineOut,
    EventAttribute,
    EventLink,
    EventSource,
    GradeOut,
    NewsOut,
    PersonOut,
    ProfileFieldOut,
    Provenance,
    RankingEntryOut,
    ScheduleEventOut,
    TodayOut,
    UpdateOut,
)
from botai.domain.sources import LiveSources

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
    ("profile", ProfileField),
    ("ranking", RankingEntry),
)

# A document of a source the student switched off is not shown or searched anywhere (FR-054): like
# every other record of that source. A document of no source (a file of a chat) always is.
LIVE_DOCUMENT = ~exists().where(
    Connection.id == Document.connection_id, Connection.status == "disabled"
)

DONE_STATES = frozenset({"submitted", "graded"})

MAX_ROWS = 2000
TODAY_DEADLINES = 10
TODAY_UPDATES = 5
TODAY_UPDATES_DAYS = 3
MAX_UPDATES = 100
DEFAULT_TZ = "Europe/Moscow"

# Rows are read this far beyond the window so that a class near its edge still finds, in the
# primary source, the same class a week away (`merge.NEARBY`) and the one that overlaps it.
MERGE_MARGIN = NEARBY + timedelta(days=1)

_EVENT_KINDS = ("lecture", "seminar", "lab", "exam", "consultation", "other")
_EVENT_FORMATS = ("offline", "online", "hybrid", "unknown")
_DEADLINE_STATES = ("open", "submitted", "graded", "missed", "unknown")
_GRADE_LAYERS = ("item", "current", "final", "summary")

# The `source_priorities` kind of a section: the v4 schedule vocabulary for the schedule, the
# reading-kind vocabulary of the domain tables for the rest (`sources.v4.kinds`).
_PRIORITY_KIND = {
    "courses": "courses",
    "scheduleEvents": "schedule",
    "deadlines": "deadlines",
    "grades": "grades",
    "people": "people",
    "locations": "locations",
    "news": "news",
    "profile": "profile",
    "ranking": "ranking",
}


def _choice(value: str | None, allowed: tuple[str, ...], default: str | None) -> Any:
    return value if value in allowed else default


_aware = keys.aware
_moment = keys.moment
_text_key = keys.text_key


async def _require_space(session: AsyncSession, user_id: str, space_id: str | None) -> None:
    if space_id is None:
        return
    found = await session.scalar(
        select(Space.id).where(Space.id == space_id, Space.user_id == user_id)
    )
    if found is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")


# --- provenance and joins between the two paths -------------------------------------------------


def _prov(row: Any, live: LiveSources) -> Provenance:
    source = live.resolve(row)
    return Provenance(
        connection_id=row.connection_id,
        source_id=row.source_id,
        space_id=source.space_id,
        source_url=row.source_url,
        open_url=row.open_url,
        fetched_at=row.fetched_at,
        stale=source.stale,
    )


def _event_source(row: Any, live: LiveSources) -> EventSource:
    source = live.resolve(row)
    return EventSource(connection_id=row.connection_id, source_id=row.source_id, title=source.title)


async def _course_refs(
    session: AsyncSession, user_id: str, live: LiveSources, rows: Sequence[Any]
) -> dict[tuple[str, str], tuple[str, str]]:
    """`(id, title)` of the courses the given rows refer to, looked up by the row's own
    provenance: a v3 row resolves against its connection's courses, a v4 row against its source's
    (one query)."""
    wanted = {row.course_external_id for row in rows if row.course_external_id}
    if not wanted:
        return {}
    found = await session.execute(
        select(
            Course.id, Course.connection_id, Course.source_id, Course.external_id, Course.title
        ).where(
            Course.user_id == user_id,
            live.filter_of(Course),
            Course.external_id.in_(wanted),
        )
    )
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for course_id, connection_id, source_id, external_id, title in found.all():
        key = source_id if source_id is not None and source_id in live.sources else connection_id
        if key is not None:
            out[(key, external_id)] = (course_id, title)
    return out


async def _course_titles(
    session: AsyncSession, user_id: str, live: LiveSources, rows: Sequence[Any]
) -> dict[tuple[str, str], str]:
    """Titles of the courses the given rows refer to (see `_course_refs`)."""
    refs = await _course_refs(session, user_id, live, rows)
    return {key: title for key, (_, title) in refs.items()}


async def _assignment_descriptions(
    session: AsyncSession, user_id: str, live: LiveSources, rows: Sequence[Any]
) -> dict[tuple[str, str], str]:
    """Task text of a deadline comes from its assignment, when the same source gave one."""
    wanted = {row.assignment_external_id for row in rows if row.assignment_external_id}
    if not wanted:
        return {}
    found = await session.execute(
        select(
            Assignment.connection_id,
            Assignment.source_id,
            Assignment.external_id,
            Assignment.description,
        ).where(
            Assignment.user_id == user_id,
            live.filter_of(Assignment),
            Assignment.external_id.in_(wanted),
            Assignment.description.is_not(None),
        )
    )
    out: dict[tuple[str, str], str] = {}
    for connection_id, source_id, external_id, description in found.all():
        key = source_id if source_id is not None and source_id in live.sources else connection_id
        if key is not None and description:
            out[(key, external_id)] = description
    return out


# --- sections -----------------------------------------------------------------------------------


async def section_counts(
    session: AsyncSession, user_id: str, space_id: str
) -> list[tuple[str, int]]:
    """How many rows of each kind the live sources of one space have (documents: the space's,
    but not those of a source switched off)."""
    live = await sources.load(session, user_id, space_id=space_id)
    counts: list[tuple[str, int]] = []
    for kind, model in _SECTION_TABLES:
        n = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id, live.filter_of(model))
        )
        counts.append((kind, int(n or 0)))
    docs = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.user_id == user_id, Document.space_id == space_id, LIVE_DOCUMENT)
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
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, SpacePlan]:
    """How each university with more than one schedule source is merged (`domain.merge`): the v4
    priority order first, the legacy v3 primary after it, the rest by health and age."""
    orders = await sources.schedule_orders(session, user_id, live)
    several = {sid: found for sid, found in orders.items() if len(found.order) > 1}
    if not several:
        return {}
    coverage = await sources.coverage_of(
        session,
        user_id,
        live,
        [found.order[0] for found in several.values()],
        start=start,
        end=end,
    )
    titles = live.titles()
    return {
        sid: SpacePlan(
            order=found.order,
            titles=titles,
            zone=_zone(found.time_zone),
            coverage=coverage.get(found.order[0]),
        )
        for sid, found in several.items()
    }


async def _event_outs(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    rows: Sequence[ScheduleEvent],
) -> list[ScheduleEventOut]:
    titles = await _course_titles(session, user_id, live, rows)
    out: list[ScheduleEventOut] = []
    for r in rows:
        source = live.resolve(r)
        out.append(
            ScheduleEventOut(
                id=r.id,
                title=r.title,
                kind=_choice(r.kind, _EVENT_KINDS, "other"),
                starts_at=r.starts_at,
                ends_at=r.ends_at,
                format=_choice(r.format, _EVENT_FORMATS, "unknown"),
                location=r.location_text,
                teacher=r.teacher_text,
                course_title=titles.get((source.id, r.course_external_id or "")),
                url=r.url,
                kind_text=r.kind_text,
                address=r.address,
                group=r.group_text,
                lesson_number=r.lesson_number,
                links=[EventLink(**link) for link in r.links],
                attributes=[EventAttribute(**attribute) for attribute in r.attributes],
                changed=r.changed,
                conflicts_with=[],
                provenance=_prov(r, live),
                sources=[_event_source(r, live)],
            )
        )
    return out


async def _events(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    *,
    start: datetime,
    end: datetime,
) -> list[ScheduleEventOut]:
    """Events overlapping [start, end) with conflicts computed against *all* live sources,
    including events just outside the window that overlap one inside it. Classes of one university
    that several of its sources describe are merged first (FR-054): the primary source decides."""
    if live.is_empty:
        return []
    overlap = (ScheduleEvent.user_id == user_id, live.filter_of(ScheduleEvent))
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
    plans = await _merge_plans(session, user_id, live, start=start, end=end)
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
    events = merge_events(await _event_outs(session, user_id, live, rows), plans)
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
    """The classes of ONE v3 source overlapping [start, end), as the source has them: no merge, no
    conflicts (the agent compares two sources with it). A disabled or foreign source is empty."""
    now = now or SystemClock().now()
    live = await sources.load(session, user_id, now=now)
    if connection_id not in live.connections:
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
    return await _event_outs(session, user_id, live, rows)


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
    live = await sources.load(session, user_id, now=now)
    events = await _events(session, user_id, live, start=_aware(start), end=_aware(end))
    if space_id is not None:
        events = [e for e in events if e.provenance.space_id == space_id]
    return events


# --- deadlines ----------------------------------------------------------------------------------


def _deadline_key(row: Deadline, live: LiveSources) -> tuple[str, str, str]:
    return keys.deadline_key(row, live.resolve(row).space_id)


# The effective state of a task (D-045): the student's mark, else the stored state (the source's,
# with the status rules applied at write time).
EFFECTIVE_STATE = func.coalesce(Deadline.user_state, Deadline.state)


async def _deadline_grades(
    session: AsyncSession, user_id: str, live: LiveSources, rows: Sequence[Deadline]
) -> dict[tuple[str, str], Grade]:
    """The latest grade of each task, by `(source, task id)`: a grade of the same source whose
    `assignment_external_id` is the deadline's assignment id or its own external id (one query)."""
    wanted = {
        value
        for row in rows
        if row.source_id is not None
        for value in (row.assignment_external_id, row.external_id)
        if value
    }
    if not wanted:
        return {}
    found = await session.scalars(
        select(Grade)
        .where(
            Grade.user_id == user_id,
            live.filter_of(Grade),
            Grade.source_id.is_not(None),
            Grade.assignment_external_id.in_(sorted(wanted)),
        )
        .order_by(
            func.coalesce(Grade.graded_at, Grade.changed_at, Grade.fetched_at).desc(),
            Grade.id.desc(),
        )
    )
    out: dict[tuple[str, str], Grade] = {}
    for grade in found:
        key = (grade.source_id or "", grade.assignment_external_id or "")
        out.setdefault(key, grade)  # newest first: the first one is the latest
    return out


def _grade_of(row: Deadline, grades: dict[tuple[str, str], Grade]) -> DeadlineGradeOut | None:
    if row.source_id is None:
        return None
    grade = None
    for task_id in (row.assignment_external_id, row.external_id):
        if task_id and (grade := grades.get((row.source_id, task_id))) is not None:
            break
    if grade is None:
        return None
    return DeadlineGradeOut(
        value=grade.value,
        scale=grade.scale,
        value_num=grade.value_num,
        scale_max=grade.scale_max,
        url=grade.url,
    )


async def _deadlines(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    *,
    include_done: bool,
    due_from: datetime | None = None,
    limit: int = MAX_ROWS,
) -> list[DeadlineOut]:
    """Deadlines by due date with their effective state.

    The done filter reads the effective state. A task the student marked in one source is marked
    for its copies in the other sources of the university too: the marked rows are read alongside
    (there are few) and their mark decides the state of the row the dedupe keeps."""
    if live.is_empty:
        return []
    due = func.coalesce(Deadline.effective_due_at, Deadline.due_at)
    base = (Deadline.user_id == user_id, live.filter_of(Deadline))
    stmt = select(Deadline).where(*base)
    if not include_done:
        stmt = stmt.where(EFFECTIVE_STATE.not_in(sorted(DONE_STATES)))
    if due_from is not None:
        stmt = stmt.where(due >= due_from)
    rows = list(
        (
            await session.scalars(stmt.order_by(due.asc().nulls_last(), Deadline.id).limit(limit))
        ).all()
    )
    marked_stmt = select(Deadline).where(*base, Deadline.user_state.is_not(None))
    if due_from is not None:
        marked_stmt = marked_stmt.where(due >= due_from)
    marked = list((await session.scalars(marked_stmt.limit(MAX_ROWS))).all())
    marks = {_deadline_key(row, live): row.user_state for row in marked if row.user_state}
    seen = {row.id for row in rows}
    extra = [row for row in marked if row.id not in seen]
    if extra and not include_done:  # a marked copy decides its group even when it is done itself
        rows.extend(extra)
        rows.sort(key=lambda row: (_moment(row.effective_due_at or row.due_at) or "~", row.id))
    kind = _PRIORITY_KIND["deadlines"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[Deadline] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _deadline_key(row, live),
        )
    ]
    out = await _deadline_outs(session, user_id, live, kept, marks=marks)
    if not include_done:
        out = [item for item in out if item.state not in DONE_STATES]
    return out[:limit]


async def _deadline_outs(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    rows: Sequence[Deadline],
    *,
    marks: dict[tuple[str, str, str], str] | None = None,
) -> list[DeadlineOut]:
    """The API form of deadlines: effective state, course, task text and grade (a few queries)."""
    marks = marks or {}
    rules = await overrides.rules_of(
        session, user_id, source_ids={row.source_id for row in rows if row.source_id}
    )
    titles = await _course_titles(session, user_id, live, rows)
    texts = await _assignment_descriptions(session, user_id, live, rows)
    grades = await _deadline_grades(session, user_id, live, rows)
    out: list[DeadlineOut] = []
    for r in rows:
        source = live.resolve(r)
        state, state_source = overrides.effective_state(
            user_state=r.user_state or marks.get(_deadline_key(r, live)),
            state=r.state,
            state_text=r.state_text,
            rule=overrides.rule_state(rules, r.source_id, r.state_text),
        )
        out.append(
            DeadlineOut(
                id=r.id,
                title=r.title,
                due_at=r.due_at,
                effective_due_at=r.effective_due_at,
                state=_choice(state, _DEADLINE_STATES, "unknown"),
                course_title=titles.get((source.id, r.course_external_id or "")),
                description=texts.get((source.id, r.assignment_external_id or "")),
                url=r.url,
                provenance=_prov(r, live),
                state_text=r.state_text,
                state_source=state_source,
                changed_at=r.changed_at,
                grade=_grade_of(r, grades),
            )
        )
    return out


async def get_deadline(
    session: AsyncSession, user_id: str, deadline_id: str, *, now: datetime | None = None
) -> DeadlineOut:
    """One task of a live source, in the API form, with the same effective state as the list
    (a mark on a copy of it in another source counts); a foreign or hidden one is `not_found`."""
    now = now or SystemClock().now()
    live = await sources.load(session, user_id, now=now)
    row = await session.scalar(
        select(Deadline).where(
            Deadline.id == deadline_id, Deadline.user_id == user_id, live.filter_of(Deadline)
        )
    )
    if row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    key = _deadline_key(row, live)
    marked = await session.scalars(
        select(Deadline).where(
            Deadline.user_id == user_id,
            live.filter_of(Deadline),
            Deadline.user_state.is_not(None),
            func.coalesce(Deadline.effective_due_at, Deadline.due_at).is_not_distinct_from(
                row.effective_due_at or row.due_at
            ),
        )
    )
    mark = next(
        (
            copy.user_state
            for copy in marked
            if copy.user_state and _deadline_key(copy, live) == key
        ),
        None,
    )
    marks = {key: mark} if mark else {}
    return (await _deadline_outs(session, user_id, live, [row], marks=marks))[0]


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
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    return await _deadlines(session, user_id, live, include_done=include_done)


# --- the rest of the domain ---------------------------------------------------------------------


def _course_key(row: Course, live: LiveSources) -> tuple[str, str, str]:
    return (
        live.resolve(row).space_id,
        _text_key(row.code) or _text_key(row.title),
        _text_key(row.term),
    )


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
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    rows = list(
        await session.scalars(
            select(Course)
            .where(
                Course.user_id == user_id,
                live.filter_of(Course),
                Course.archived.is_(False),
            )
            .order_by(Course.title, Course.id)
            .limit(MAX_ROWS)
        )
    )
    kind = _PRIORITY_KIND["courses"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[Course] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _course_key(row, live),
        )
    ]
    return [
        CourseOut(
            id=r.id,
            title=r.title,
            code=r.code,
            term=r.term,
            url=r.url,
            provenance=_prov(r, live),
        )
        for r in kept
    ]


def _grade_key(row: Grade, live: LiveSources) -> tuple[str, str, str, str]:
    return (
        live.resolve(row).space_id,
        _text_key(row.title),
        _text_key(row.value),
        _moment(row.graded_at),
    )


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
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    rows = list(
        await session.scalars(
            select(Grade)
            .where(Grade.user_id == user_id, live.filter_of(Grade))
            .order_by(Grade.graded_at.desc().nulls_last(), Grade.title, Grade.id)
            .limit(MAX_ROWS)
        )
    )
    kind = _PRIORITY_KIND["grades"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[Grade] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _grade_key(row, live),
        )
    ]
    courses = await _course_refs(session, user_id, live, kept)
    out: list[GradeOut] = []
    for r in kept:
        source = live.resolve(r)
        course = courses.get((source.id, r.course_external_id or ""))
        out.append(
            GradeOut(
                id=r.id,
                title=r.title,
                value=r.value,
                scale=r.scale,
                weight=r.weight,
                course_title=course[1] if course else None,
                graded_at=r.graded_at,
                provenance=_prov(r, live),
                value_num=r.value_num,
                scale_max=r.scale_max,
                course_id=course[0] if course else None,
                layer=_choice(r.layer, _GRADE_LAYERS, None),
                period=r.period,
                assignment_id=r.assignment_external_id,
                url=r.url,
                comment=r.comment,
                changed_at=r.changed_at,
            )
        )
    return out


def _person_key(row: Person, live: LiveSources) -> tuple[str, str]:
    return (live.resolve(row).space_id, _text_key(row.name))


async def list_people(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[PersonOut]:
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    rows = list(
        await session.scalars(
            select(Person)
            .where(Person.user_id == user_id, live.filter_of(Person))
            .order_by(Person.name, Person.id)
            .limit(MAX_ROWS)
        )
    )
    kind = _PRIORITY_KIND["people"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[Person] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _person_key(row, live),
        )
    ]
    return [
        PersonOut(
            id=r.id,
            name=r.name,
            role=r.role,
            email=r.email,
            provenance=_prov(r, live),
        )
        for r in kept
    ]


def _news_key(row: NewsItem, live: LiveSources) -> tuple[str, str, str]:
    return (
        live.resolve(row).space_id,
        _text_key(row.url) or _text_key(row.title),
        _moment(row.published_at),
    )


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
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    rows = list(
        await session.scalars(
            select(NewsItem)
            .where(NewsItem.user_id == user_id, live.filter_of(NewsItem))
            .order_by(NewsItem.published_at.desc(), NewsItem.id)
            .limit(MAX_ROWS)
        )
    )
    kind = _PRIORITY_KIND["news"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[NewsItem] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _news_key(row, live),
        )
    ]
    return [
        NewsOut(
            id=r.id,
            title=r.title,
            body=r.body,
            published_at=r.published_at,
            url=r.url,
            provenance=_prov(r, live),
        )
        for r in kept
    ]


def _profile_key(row: ProfileField, live: LiveSources) -> tuple[str, str]:
    return (live.resolve(row).space_id, _text_key(row.field))


async def list_profile(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> list[ProfileFieldOut]:
    """The fields of the student's card, grouped as the source groups them."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    rows = list(
        await session.scalars(
            select(ProfileField)
            .where(ProfileField.user_id == user_id, live.filter_of(ProfileField))
            .order_by(
                ProfileField.group_text.asc().nulls_first(), ProfileField.label, ProfileField.id
            )
            .limit(MAX_ROWS)
        )
    )
    kind = _PRIORITY_KIND["profile"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[ProfileField] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _profile_key(row, live),
        )
    ]
    return [
        ProfileFieldOut(
            id=r.id,
            field=r.field,
            label=r.label,
            value=r.value,
            group=r.group_text,
            provenance=_prov(r, live),
        )
        for r in kept
    ]


def _ranking_key(row: RankingEntry, live: LiveSources) -> tuple[str, str, int, str]:
    return (live.resolve(row).space_id, _text_key(row.board), row.position, _text_key(row.label))


async def list_ranking(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    board: str | None = None,
    now: datetime | None = None,
) -> list[RankingEntryOut]:
    """Places of the rating boards, by board and place; `board` narrows to one board."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    if live.is_empty:
        return []
    stmt = select(RankingEntry).where(RankingEntry.user_id == user_id, live.filter_of(RankingEntry))
    if board is not None:
        stmt = stmt.where(RankingEntry.board == board)
    rows = list(
        await session.scalars(
            stmt.order_by(RankingEntry.board, RankingEntry.position, RankingEntry.id).limit(
                MAX_ROWS
            )
        )
    )
    kind = _PRIORITY_KIND["ranking"]
    preferences = await sources.preferences(session, user_id, live, [kind])
    kept: list[RankingEntry] = [
        row
        for row in sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind=kind,
            key_of=lambda row: _ranking_key(row, live),
        )
    ]
    return [
        RankingEntryOut(
            id=r.id,
            board=r.board,
            position=r.position,
            label=r.label,
            score=r.score,
            score_num=r.score_num,
            is_me=r.is_me,
            total=r.total,
            period=r.period,
            provenance=_prov(r, live),
        )
        for r in kept
    ]


# --- recent changes («Новое», docs/v4/27 §5.5) ---------------------------------------------------


async def _updates(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    *,
    since: datetime,
    limit: int,
) -> list[UpdateOut]:
    if live.is_empty or limit <= 0:
        return []
    since = _aware(since)
    grade_rows = list(
        await session.scalars(
            select(Grade)
            .where(Grade.user_id == user_id, live.filter_of(Grade), Grade.changed_at >= since)
            .order_by(Grade.changed_at.desc(), Grade.id.desc())
            .limit(MAX_UPDATES)
        )
    )
    deadline_rows = list(
        await session.scalars(
            select(Deadline)
            .where(
                Deadline.user_id == user_id,
                live.filter_of(Deadline),
                Deadline.changed_at >= since,
            )
            .order_by(Deadline.changed_at.desc(), Deadline.id.desc())
            .limit(MAX_UPDATES)
        )
    )
    preferences = await sources.preferences(
        session, user_id, live, [_PRIORITY_KIND["grades"], _PRIORITY_KIND["deadlines"]]
    )
    grades: list[Grade] = sources.dedupe_preferring(
        grade_rows,
        live=live,
        preferences=preferences,
        kind=_PRIORITY_KIND["grades"],
        key_of=lambda row: _grade_key(row, live),
    )
    deadlines: list[Deadline] = sources.dedupe_preferring(
        deadline_rows,
        live=live,
        preferences=preferences,
        kind=_PRIORITY_KIND["deadlines"],
        key_of=lambda row: _deadline_key(row, live),
    )
    titles = await _course_titles(session, user_id, live, [*grades, *deadlines])
    out: list[UpdateOut] = []
    for g in grades:
        source = live.resolve(g)
        out.append(
            UpdateOut(
                kind="grade",
                id=g.id,
                title=g.title,
                course_title=titles.get((source.id, g.course_external_id or "")),
                changed_at=cast("datetime", g.changed_at),
                url=g.url,
                provenance=_prov(g, live),
                value=g.value,
                scale=g.scale,
                value_num=g.value_num,
                scale_max=g.scale_max,
                layer=_choice(g.layer, _GRADE_LAYERS, None),
            )
        )
    for d in deadlines:
        source = live.resolve(d)
        out.append(
            UpdateOut(
                kind="deadline",
                id=d.id,
                title=d.title,
                course_title=titles.get((source.id, d.course_external_id or "")),
                changed_at=cast("datetime", d.changed_at),
                url=d.url,
                provenance=_prov(d, live),
                state=_choice(d.user_state or d.state, _DEADLINE_STATES, "unknown"),
                state_text=d.state_text,
            )
        )
    out.sort(key=lambda u: (u.changed_at, u.id), reverse=True)
    return out[:limit]


async def list_updates(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    since: datetime,
    limit: int = 20,
    now: datetime | None = None,
) -> list[UpdateOut]:
    """Grades that appeared or changed and tasks whose state changed in the source since `since`,
    newest first (`changed_at` means "the source changed it": a student's mark or status rule is
    not an update). The student's live sources only, one row per entity across the sources of a
    university (the priority order decides, like every other read)."""
    now = now or SystemClock().now()
    await _require_space(session, user_id, space_id)
    live = await sources.load(session, user_id, now=now, space_id=space_id)
    return await _updates(session, user_id, live, since=since, limit=min(limit, MAX_UPDATES))


# --- home screen --------------------------------------------------------------------------------


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TZ)


async def today(session: AsyncSession, user_id: str, *, now: datetime | None = None) -> TodayOut:
    """Today's classes across spaces (each space's own calendar day), the nearest open deadlines
    and the sources that need attention. `date` is today in the oldest space's time zone."""
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
    live = await sources.load(session, user_id, now=now)

    events: list[ScheduleEventOut] = []
    if windows and not live.is_empty:
        lo = min(w[0] for w in windows.values())
        hi = max(w[1] for w in windows.values())
        events = [
            e
            for e in await _events(session, user_id, live, start=lo, end=hi)
            if e.starts_at < windows[e.provenance.space_id][1]
            and e.ends_at > windows[e.provenance.space_id][0]
        ]

    deadlines = await _deadlines(
        session,
        user_id,
        live,
        include_done=False,
        due_from=day_of(home)[0],
        limit=TODAY_DEADLINES,
    )
    updates = await _updates(
        session,
        user_id,
        live,
        since=now - timedelta(days=TODAY_UPDATES_DAYS),
        limit=TODAY_UPDATES,
    )
    return TodayOut(
        date=now.astimezone(home).date().isoformat(),
        events=events,
        deadlines=deadlines,
        attention=live.attention(),
        updates=updates,
    )
