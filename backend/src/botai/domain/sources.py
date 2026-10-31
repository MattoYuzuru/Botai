"""Which sources feed the product-domain reads: v3 connections and v4 sources side by side.

The domain read path used to know one kind of provenance: a domain row points at a `connections`
row (`connection_id`) and the read of a university takes the rows of its live connections. The v4
sources write the same tables with `source_id` instead (`sources/v4/domain.py`), so this module
makes both visible to the read layer without changing the v3 rows or their order:

* `load` returns the live sources of the user (or one space): v3 connections and v4 sources, with
  their title, health and staleness. A disabled source is not live: its data is hidden, never
  deleted (`docs/v4/03-sources-and-ux.md` §6.2). A v4 source is hidden when its status is
  `disabled` or `archived`; `stale`/`needs_reauth`/`error` keep their data and are marked stale,
  exactly like a v3 connection that needs attention.
* one SQL predicate per model (`filter_of`) covers both provenance columns at once, so a read
  stays one query per kind: no pass over the sources and no N+1.
* `preferences` gives the order of the live sources per (space, data kind): the first one is the
  source that decides a conflict (`primary`, `docs/v4/03-sources-and-ux.md` §4.1). The v4
  `source_priorities` setting wins; a v3-only space keeps its today's order — the student's
  `spaces.primary_schedule_connection_id` first, then the healthy sources by age.
* `schedule_orders` is the same order for the schedule merge, but only for sources that have
  classes (or are chosen), so `domain.merge` sees the same plan as before when there are no v4
  sources.
* `coverage_of` is the primary source's reach for the merge's `not_in_primary` rule.

Nothing here writes. The stored rows stay per source; the overlay is computed on every read.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import AdapterSpecRow, Connection, ScheduleEvent, Source, SourcePriority, Space
from botai.domain import merge
from botai.domain.primary import COVERAGE_MARGIN
from botai.sources.schedule import freshness_of, is_stale
from botai.spaces.views import display_title

SourceKind = Literal["connection", "source"]

# `stale` and the statuses that need the student or a repair: the data is shown and marked.
ATTENTION_STATUSES = frozenset({"stale", "needs_reauth", "waiting_user", "repairing", "error"})
# A v4 source is hidden only when the student switched it off (or archived it): its data leaves
# every view, but nothing is deleted (03 §6.2). Everything else is visible, honestly marked stale.
HIDDEN_V4 = frozenset({"disabled", "archived"})
# A v4 source whose last read may be behind; `active`/`syncing` are the healthy ones.
V4_STALE = frozenset({"stale", "needs_reauth", "needs_link", "waiting_user", "error"})
# Sources whose data is current enough to decide a default conflict (same set as `domain.primary`).
HEALTHY = frozenset({"active", "syncing", "stale"})

# `source_priorities.kind` uses the v3 reading-kind vocabulary of the domain tables ("schedule"
# is the v4 name; the API-facing kinds are `scheduleEvents` in v3, see `sources.v4.kinds`).
SCHEDULE_KIND = "schedule"
SCHEDULE_READING_KIND = "scheduleEvents"


@dataclass(frozen=True, slots=True)
class LiveSource:
    """One source of the read path: a v3 connection or a v4 source, in the same shape."""

    id: str
    kind: SourceKind
    space_id: str
    status: str
    stale: bool
    created_at: datetime
    title: str
    priority: int = 0

    @property
    def healthy(self) -> bool:
        return self.status in HEALTHY

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION_STATUSES or self.stale

    @property
    def data_kind(self) -> str:
        """The `source_priorities` kind name of the schedule rows this source can provide."""
        return SCHEDULE_KIND


@dataclass(frozen=True, slots=True)
class LiveSources:
    """The live sources of one read: connections and v4 sources, each by their provenance id."""

    connections: Mapping[str, LiveSource]
    sources: Mapping[str, LiveSource]

    @property
    def is_empty(self) -> bool:
        return not self.connections and not self.sources

    @property
    def space_ids(self) -> tuple[str, ...]:
        return tuple({s.space_id for s in self})

    def __iter__(self) -> Iterator[LiveSource]:
        yield from self.connections.values()
        yield from self.sources.values()

    def all(self) -> list[LiveSource]:
        return list(self)

    def by_space(self) -> dict[str, list[LiveSource]]:
        out: dict[str, list[LiveSource]] = {}
        for source in self:
            out.setdefault(source.space_id, []).append(source)
        return out

    def get(self, provenance_id: str | None) -> LiveSource | None:
        if provenance_id is None:
            return None
        found = self.sources.get(provenance_id)
        return found if found is not None else self.connections.get(provenance_id)

    def resolve(self, row: Any) -> LiveSource:
        """The live source of a domain row; a row of no live source is a bug of a read predicate."""
        found = self.get(row.source_id) or self.get(row.connection_id)
        if found is None:
            raise BotaiError(ErrorCode.INTERNAL, "Доменная запись без действующего источника")
        return found

    def key_of(self, row: Any) -> str:
        """The provenance id of a row: the v4 source when it has one, else the v3 connection."""
        if row.source_id is not None and row.source_id in self.sources:
            return row.source_id
        if row.connection_id is not None and row.connection_id in self.connections:
            return row.connection_id
        return self.resolve(row).id

    def filter_of(self, model: Any) -> ColumnElement[bool]:
        """One predicate over both provenance columns of a domain model."""
        clauses: list[ColumnElement[bool]] = []
        if self.connections:
            clauses.append(model.connection_id.in_(list(self.connections)))
        if self.sources:
            clauses.append(model.source_id.in_(list(self.sources)))
        if not clauses:
            return false()
        return or_(*clauses)

    def titles(self) -> dict[str, str]:
        return {source.id: source.title for source in self}

    def attention(self) -> list[str]:
        return [source.id for source in self if source.needs_attention]

    def default_order(self) -> dict[str, list[LiveSource]]:
        """Per space: healthy sources first, then the oldest — the v3 default order."""
        out: dict[str, list[LiveSource]] = {}
        for space_id, sources in self.by_space().items():
            out[space_id] = sorted(sources, key=lambda s: (not s.healthy, s.created_at, s.id))
        return out


async def load(
    session: AsyncSession,
    user_id: str,
    *,
    space_id: str | None = None,
    now: datetime | None = None,
) -> LiveSources:
    """The live sources of the student (or one space), both provenance paths in one call.

    Two queries in total (v3 connections with their adapter spec, v4 sources) — the same shape as
    the v3-only read had for connections, plus one query for the v4 table. No per-source query.
    """
    now = now or SystemClock().now()
    conn_stmt = (
        select(Connection, AdapterSpecRow.spec)
        .outerjoin(AdapterSpecRow, AdapterSpecRow.id == Connection.spec_id)
        .where(Connection.user_id == user_id, Connection.status != "disabled")
        .order_by(Connection.created_at, Connection.id)
    )
    source_stmt = (
        select(Source)
        .where(Source.user_id == user_id, Source.status.not_in(sorted(HIDDEN_V4)))
        .order_by(Source.created_at, Source.id)
    )
    if space_id is not None:
        conn_stmt = conn_stmt.where(Connection.space_id == space_id)
        source_stmt = source_stmt.where(Source.space_id == space_id)
    connections: dict[str, LiveSource] = {}
    for conn, spec in (await session.execute(conn_stmt)).all():
        stale = conn.status in ATTENTION_STATUSES or is_stale(
            now, conn.last_sync_at, freshness_of(spec or {})
        )
        connections[conn.id] = LiveSource(
            id=conn.id,
            kind="connection",
            space_id=conn.space_id,
            status=conn.status,
            stale=stale,
            created_at=conn.created_at,
            title=display_title(conn),
        )
    sources: dict[str, LiveSource] = {}
    for source in await session.scalars(source_stmt):
        sources[source.id] = LiveSource(
            id=source.id,
            kind="source",
            space_id=source.space_id,
            status=source.status,
            stale=source.status in V4_STALE,
            created_at=source.created_at,
            title=source.title,
            priority=source.priority,
        )
    return LiveSources(connections=connections, sources=sources)


# --- order of the live sources per space and data kind ------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleOrder:
    """The schedule sources of one university, the primary first (the v3 `primary.ScheduleSources`
    with both provenance paths); `chosen` — the first one is an explicit setting, not the default."""

    space_id: str
    time_zone: str
    order: tuple[str, ...]
    chosen: bool

    @property
    def primary(self) -> str | None:
        return self.order[0] if self.order else None


@dataclass(frozen=True, slots=True)
class Preferences:
    """The rank of every live source per (space, data kind): 0 decides a conflict."""

    orders: Mapping[tuple[str, str], Mapping[str, int]]

    def rank(self, space_id: str, kind: str, provenance_id: str) -> int:
        order = self.orders.get((space_id, kind))
        return order.get(provenance_id, len(order)) if order is not None else 0

    def better(self, space_id: str, kind: str, first: LiveSource, second: LiveSource) -> bool:
        """Whether `first` decides a conflict over `second` for this kind of data."""
        return self.rank(space_id, kind, first.id) <= self.rank(space_id, kind, second.id)


async def _spaces(
    session: AsyncSession, user_id: str, live: LiveSources
) -> dict[str, tuple[str, str | None]]:
    """space_id -> (time_zone, the student's legacy primary schedule connection)."""
    if live.is_empty:
        return {}
    rows = await session.execute(
        select(Space.id, Space.time_zone, Space.primary_schedule_connection_id).where(
            Space.user_id == user_id, Space.id.in_(list(live.space_ids))
        )
    )
    return {space_id: (zone, chosen) for space_id, zone, chosen in rows.all()}


async def _priorities(
    session: AsyncSession, live: LiveSources, kinds: Iterable[str]
) -> dict[tuple[str, str], list[str]]:
    wanted = list(dict.fromkeys(kinds))
    if not live.sources or not wanted:
        # A v3-only space has no v4 priority setting: the legacy order is the whole answer, so the
        # v3 read keeps its query count exactly as before the v4 overlay.
        return {}
    rows = await session.scalars(
        select(SourcePriority).where(
            SourcePriority.space_id.in_(list(live.space_ids)),
            SourcePriority.kind.in_(wanted),
        )
    )
    return {(row.space_id, row.kind): list(row.source_ids) for row in rows}


def _ordered_for_kind(
    *,
    space_id: str,
    kind: str,
    live: LiveSources,
    default: Sequence[LiveSource],
    priority: Mapping[tuple[str, str], list[str]],
    spaces: Mapping[str, tuple[str, str | None]],
) -> tuple[list[str], bool]:
    """The explicit names first (v4 priority list, then the legacy v3 primary for the schedule),
    then the rest of the default order. Sources of the setting that are not live are skipped."""
    names = [source_id for source_id in priority.get((space_id, kind), []) if live.get(source_id)]
    chosen = bool(names)
    if kind == SCHEDULE_KIND:
        legacy = spaces.get(space_id, ("", None))[1]
        if legacy is not None and live.get(legacy) is not None and legacy not in names:
            names.append(legacy)
            chosen = True
    rest = [source.id for source in default if source.id not in names]
    return names + rest, chosen


async def preferences(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    kinds: Sequence[str],
) -> Preferences:
    """The per-(space, kind) order of live sources; used for the dedupe of non-schedule reads."""
    if not live.sources or not kinds:
        # Without a v4 source there is nothing to deduplicate, and no extra query is made (a v3-only
        # answer stays byte-identical and as cheap as before).
        return Preferences(orders={})
    spaces = await _spaces(session, user_id, live)
    priority = await _priorities(session, live, kinds)
    default = live.default_order()
    orders: dict[tuple[str, str], dict[str, int]] = {}
    for space_id, sources in default.items():
        for kind in kinds:
            names, _ = _ordered_for_kind(
                space_id=space_id,
                kind=kind,
                live=live,
                default=sources,
                priority=priority,
                spaces=spaces,
            )
            orders[(space_id, kind)] = {name: rank for rank, name in enumerate(names)}
    return Preferences(orders=orders)


async def _sources_with_schedule(
    session: AsyncSession, user_id: str, live: LiveSources
) -> set[str]:
    """The provenance ids that actually have schedule rows: one query per provenance path."""
    found: set[str] = set()
    if live.connections:
        found.update(
            value
            for value in await session.scalars(
                select(ScheduleEvent.connection_id)
                .where(
                    ScheduleEvent.user_id == user_id,
                    ScheduleEvent.connection_id.in_(list(live.connections)),
                )
                .distinct()
            )
            if value is not None
        )
    if live.sources:
        found.update(
            value
            for value in await session.scalars(
                select(ScheduleEvent.source_id)
                .where(
                    ScheduleEvent.user_id == user_id,
                    ScheduleEvent.source_id.in_(list(live.sources)),
                )
                .distinct()
            )
            if value is not None
        )
    return found


async def schedule_orders(
    session: AsyncSession, user_id: str, live: LiveSources
) -> dict[str, ScheduleOrder]:
    """Per space: its schedule sources, the primary first — both provenance paths (FR-054, FR-062).

    A space with no source that has classes (and no explicit choice) is absent, exactly as in the
    v3-only read; the v4 priority setting and the legacy v3 choice place the first source, the rest
    follow by health and age.
    """
    if live.is_empty:
        return {}
    spaces = await _spaces(session, user_id, live)
    if not spaces:
        return {}
    priority = await _priorities(session, live, [SCHEDULE_KIND])
    with_schedule = await _sources_with_schedule(session, user_id, live)
    default = live.default_order()
    out: dict[str, ScheduleOrder] = {}
    for space_id, sources in default.items():
        names, chosen = _ordered_for_kind(
            space_id=space_id,
            kind=SCHEDULE_KIND,
            live=live,
            default=sources,
            priority=priority,
            spaces=spaces,
        )
        # Explicit settings first: the v4 priority list then the legacy v3 primary. They are
        # included even before the source has read anything (a chosen primary stays chosen).
        explicit = list(priority.get((space_id, SCHEDULE_KIND), []))
        legacy = spaces.get(space_id, ("Europe/Moscow", None))[1]
        if legacy is not None and legacy not in explicit:
            explicit.append(legacy)
        explicit = [name for name in explicit if live.get(name) is not None]
        # Everything else only when it actually has classes, like the v3-only read did.
        order = explicit + [
            name for name in names if name not in explicit and name in with_schedule
        ]
        if order:
            out[space_id] = ScheduleOrder(
                space_id=space_id,
                time_zone=spaces.get(space_id, ("Europe/Moscow", None))[0],
                order=tuple(dict.fromkeys(order)),
                chosen=chosen,
            )
    return out


async def coverage_of(
    session: AsyncSession,
    user_id: str,
    live: LiveSources,
    provenance_ids: Iterable[str],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, merge.Coverage]:
    """Where each source's regular classes reach around [start, end) — the v3 `primary.coverage_of`
    for both provenance paths. A bounded query over the given sources only."""
    wanted = [source_id for source_id in provenance_ids if live.get(source_id) is not None]
    if not wanted:
        return {}
    clauses: list[ColumnElement[bool]] = []
    connections = [source_id for source_id in wanted if source_id in live.connections]
    sources = [source_id for source_id in wanted if source_id in live.sources]
    if connections:
        clauses.append(ScheduleEvent.connection_id.in_(connections))
    if sources:
        clauses.append(ScheduleEvent.source_id.in_(sources))
    rows = await session.execute(
        select(
            ScheduleEvent.connection_id,
            ScheduleEvent.source_id,
            ScheduleEvent.starts_at,
            ScheduleEvent.ends_at,
            ScheduleEvent.kind,
            ScheduleEvent.title,
        ).where(
            ScheduleEvent.user_id == user_id,
            or_(*clauses),
            ScheduleEvent.starts_at >= start - COVERAGE_MARGIN,
            ScheduleEvent.starts_at < end + COVERAGE_MARGIN,
        )
    )
    regular: dict[str, list[tuple[datetime, datetime]]] = {}
    wanted_set = set(wanted)
    for connection_id, source_id, first, last, kind, title in rows.all():
        key = source_id if source_id in wanted_set else connection_id
        if key is None or key not in wanted_set:
            continue
        if merge.is_regular(kind, title, first, last):
            regular.setdefault(key, []).append((first, last))
    found = {key: merge.Coverage.of_classes(classes) for key, classes in regular.items()}
    return {key: coverage for key, coverage in found.items() if coverage is not None}


# --- dedupe of the non-schedule reads -----------------------------------------------------------


def dedupe_preferring(
    rows: Sequence[Any],
    *,
    live: LiveSources,
    preferences: Preferences,
    kind: str,
    key_of: Callable[[Any], Hashable],
) -> list[Any]:
    """One row per entity across sources, the primary source's row kept.

    Only a group that involves a v4 source is collapsed: a v3-only result stays exactly as it was
    (same rows, same order), so enabling v4 never rewrites the v3 answers. The key is the natural
    key of the kind (title + date, code, name, url); for the same entity from two sources the row
    of the higher-priority source wins, the rest of the order is preserved.
    """
    groups: dict[Hashable, list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(key_of(row), []).append(index)
    if all(len(indices) == 1 for indices in groups.values()):
        return list(rows)
    dropped: set[int] = set()
    for indices in groups.values():
        if len(indices) < 2:
            continue
        sources = [live.resolve(rows[index]) for index in indices]
        if not any(source.kind == "source" for source in sources):
            continue  # a v3-only duplicate: the v3 read showed both rows and still does
        best = min(
            indices,
            key=lambda index: (
                preferences.rank(
                    live.resolve(rows[index]).space_id, kind, live.key_of(rows[index])
                ),
                index,
            ),
        )
        for index in indices:
            if index != best:
                dropped.add(index)
    return [row for index, row in enumerate(rows) if index not in dropped]
