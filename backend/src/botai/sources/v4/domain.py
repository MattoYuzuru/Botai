"""v4 → domain: a sync's normalized records become rows of the product domain.

The v4 runner reads a playbook and coerces every record to the same column dicts as v3
(`sources.normalize.coerce`); this module writes those dicts into the shared domain tables with
`source_id` provenance instead of a `connection_id`. Rules (docs/v4/03-sources-and-ux §4.2, §6.1):

* identity of a v4 row is `(source_id, external_id)` — running the same sync twice changes nothing
  but `fetched_at`/`seen_at`, so a re-sync never duplicates and never re-inserts an updated row;
* every row records where and when it came from (`source_url`, `open_url`, `fetched_at`,
  `spec_revision`) and `seen_at`, the last sync that saw it;
* rows of the v3 path (`connection_id` set, `source_id` NULL) are never selected, updated or
  deleted: this writer only touches rows whose `source_id` is its source;
* a run does not delete rows of its own parser version that it did not see (reads are windowed:
  an unseen row is not evidence of deletion; their old `seen_at` is the honest "last time this
  source had it"). The exception is a parser change (`sweep`, a successful sync only): rows of a
  kind this run wrote, left by ANOTHER playbook version (or by none, `spec_revision` NULL) and not
  seen now, are leftovers of an old external-id scheme and are deleted — for a time-bound kind
  (classes, tasks, news) only inside the span of dates this run read. A deleted task hands the
  student's mark to its surviving copy (same dedupe key, `domain.keys`);
* a kind without a domain table (`documents`, `messages`) stays in the run report and is counted as
  skipped, not silently dropped;
* after the rows of `grades`, `profile` or `ranking` are written, the system panels of the
  university are ensured (`panels.service.ensure_default_panels`, idempotent, D-039);
* a deadline keeps the parser's state as `source_state`; the student's status rules decide its
  `state` when they name its `state_text` (`domain.overrides`, D-045); the student's own mark
  (`user_state`) is never written here;
* `changed_at` of a grade or a deadline (the "Новое" feed, docs/v4/27 §5.5) means "the source
  changed it": set to `now` when a row appears after the source's first successful sync — and
  after the first sync that brought this kind at all, so a playbook that learns to read grades does
  not flood the feed with history — and when a grade's value or a deadline's stored state (the
  source's, through the rules) changes. A rule's recompute or a mark never sets it, and the sync
  after a recompute finds the same stored state, so it is no news either. The first sync of a new
  parser version (no stored row of the kind has its version) sets it for nothing of that kind: a
  parser change is not news, and the rows it re-reads under new ids replace the swept ones.

Writers of one source are serialized by an advisory transaction lock, so two concurrent syncs of
one source cannot insert the same natural key twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Deadline, Source
from botai.domain import keys, overrides
from botai.panels import service as panels_service
from botai.sources.normalize import ALL_MODELS, lock_connection
from botai.sources.v4 import kinds as kinds_vocab

_CHUNK = 500
# The kinds whose arrival gives the university its default panels (grades, profile, ranking).
_PANEL_KINDS = frozenset({"grades", "profile", "ranking"})
# Columns whose change means "the student should notice" for a class (the v3 normalizer's set).
_EVENT_MATERIAL = (
    "title",
    "kind",
    "starts_at",
    "ends_at",
    "location_text",
    "teacher_text",
    "address",
)


# The kinds of the "Новое" feed (docs/v4/27 §5.5): their rows carry `changed_at`.
_FEED_KINDS = frozenset({"grades", "deadlines"})


def _is_news(kind: str, row: Any, fields: Mapping[str, Any], changed: Sequence[str]) -> bool:
    """Whether an update of a stored row is a change the student should notice.

    A grade whose value changed, or whose number changed from another number; a task whose state
    changed. Reading the same data better is not news: a number found for the first time (a
    playbook learned `max`) or a state read from words kept for the first time (`state_text`
    appeared) leave `changed_at` alone, so a repaired playbook does not flood the feed."""
    if kind == "grades":
        return "value" in changed or ("value_num" in changed and row.value_num is not None)
    if kind == "deadlines":
        first_words = row.state_text is None and fields.get("state_text") is not None
        return "state" in changed and not first_words
    return False


class DomainRecord(Protocol):
    """What the writer needs from one record of a run (a `runner.NormalizedRecord`)."""

    @property
    def kind(self) -> str: ...

    @property
    def external_id(self) -> str: ...

    @property
    def fields(self) -> Mapping[str, Any]: ...

    @property
    def source_url(self) -> str | None: ...

    @property
    def open_url(self) -> str | None: ...


@dataclass(slots=True)
class DomainStats:
    """What one kind of one run wrote: the count of rows by outcome."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deleted: int = 0  # leftovers of another parser version (`sweep`)

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }


async def _lock(session: AsyncSession, source_id: str) -> None:
    """Serialize the writers of one source for the rest of the transaction."""
    await lock_connection(session, source_id)


async def _existing(
    session: AsyncSession, model: type[Any], source_id: str, external_ids: list[str]
) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for start in range(0, len(external_ids), _CHUNK):
        part = external_ids[start : start + _CHUNK]
        rows = await session.execute(
            select(model).where(model.source_id == source_id, model.external_id.in_(part))
        )
        for row in rows.scalars():
            found[row.external_id] = row
    return found


def _provenance_urls(source: Source, record: DomainRecord) -> tuple[str, str]:
    """`source_url` (never empty) and `open_url` of one record, secret-free by construction."""
    source_url = record.source_url or source.entry_url or source.identity_key
    return source_url, record.open_url or source_url


def _has_material_change(existing: Any, row: Mapping[str, Any]) -> bool:
    changed = [column for column, value in row.items() if getattr(existing, column) != value]
    return any(
        column in _EVENT_MATERIAL
        and not (column == "address" and getattr(existing, column) is None)
        for column in changed
    )


async def _has_rows(
    session: AsyncSession, model: type[Any], source_id: str, *, revision: int | None = None
) -> bool:
    stmt = select(model.id).where(model.source_id == source_id)
    if revision is not None:
        stmt = stmt.where(model.spec_revision == revision)
    return await session.scalar(stmt.limit(1)) is not None


# The date a time-bound kind is read by: a sweep only looks inside the span this run read.
_WINDOW_COLUMNS: dict[str, str] = {
    "schedule": "starts_at",
    "deadlines": "due_at",
    "news": "published_at",
}


async def write_records(
    session: AsyncSession,
    *,
    user_id: str,
    source: Source,
    records: Sequence[DomainRecord],
    now: datetime,
    revision: int | None = None,
    synced_before: bool | None = None,
    sweep: bool = False,
) -> dict[str, dict[str, int]]:
    """Upsert the records of one sync into the domain; returns counts per v4 kind.

    The caller owns the transaction (the runner commits status/state/artifacts together with these
    rows). A record whose kind has no domain table is counted as skipped. `synced_before` says
    whether the source had a successful sync before this one (default: its `last_sync_at`, which
    the runner reads before marking this sync). `sweep` (a successful sync of playbook version
    `revision`) deletes the rows another parser version left (see the module docstring).
    """
    if source.user_id != user_id:
        # The runner already resolved the source through the owner-scoped service; a mismatch here
        # would mean a foreign source object reached a write path: refuse instead of writing it.
        raise BotaiError(ErrorCode.NOT_FOUND, "Источник не найден")
    if not records:
        return {}
    if synced_before is None:
        synced_before = source.last_sync_at is not None
    await _lock(session, source.id)
    by_kind: dict[str, list[DomainRecord]] = {}
    stats: dict[str, DomainStats] = {}
    for record in records:
        by_kind.setdefault(record.kind, []).append(record)
    for kind_name in by_kind:
        stats[kind_name] = DomainStats()
    written: dict[str, tuple[type[Any], dict[str, tuple[DomainRecord, Mapping[str, Any]]]]] = {}
    for kind_name, kind_records in by_kind.items():
        st = stats[kind_name]
        reading = kinds_vocab.reading_kind(kind_name)
        if reading is None:
            st.skipped += len(kind_records)
            continue
        model = ALL_MODELS[reading]
        rows: dict[str, tuple[DomainRecord, Mapping[str, Any]]] = {}
        for record in kind_records:
            if not record.external_id or not record.fields:
                st.skipped += 1
                continue
            rows[record.external_id] = (record, record.fields)
        if kind_name == "deadlines" and rows:
            rows = await _apply_status_rules(session, user_id, source.id, rows)
        feed = kind_name in _FEED_KINDS
        had_rows = feed and await _has_rows(session, model, source.id)
        # the first sync of a new parser version re-reads old data, often under new ids: no news
        parser_changed = (
            had_rows
            and revision is not None
            and not await _has_rows(session, model, source.id, revision=revision)
        )
        # a new row is news only once this source and this kind were read before
        new_is_news = feed and synced_before and had_rows and not parser_changed
        existing = await _existing(session, model, source.id, list(rows))
        for external_id, (record, fields) in rows.items():
            source_url, open_url = _provenance_urls(source, record)
            row = existing.get(external_id)
            if row is None:
                session.add(
                    model(
                        user_id=user_id,
                        connection_id=None,
                        source_id=source.id,
                        external_id=external_id,
                        source_url=source_url,
                        open_url=open_url,
                        fetched_at=now,
                        spec_revision=revision,
                        seen_at=now,
                        **dict(fields),
                        **({"changed_at": now} if new_is_news else {}),
                    )
                )
                st.inserted += 1
                continue
            material = kind_name == "schedule" and _has_material_change(row, fields)
            changed = [column for column, value in fields.items() if getattr(row, column) != value]
            if not parser_changed and _is_news(kind_name, row, fields, changed):
                row.changed_at = now
            for column in changed:
                setattr(row, column, fields[column])
            row.source_url, row.open_url = source_url, open_url
            row.fetched_at, row.seen_at, row.spec_revision = now, now, revision
            if kind_name == "schedule" and material:
                row.changed = True
            if changed:
                st.updated += 1
            else:
                st.unchanged += 1
        if sweep and revision is not None and rows:
            written[kind_name] = (model, rows)
    await session.flush()
    for kind_name, (model, rows) in written.items():
        stats[kind_name].deleted = await _sweep(
            session,
            user_id=user_id,
            source=source,
            kind=kind_name,
            model=model,
            rows=rows,
            revision=cast("int", revision),  # `written` is filled only when it is set
        )
    if _PANEL_KINDS & set(by_kind):
        await panels_service.ensure_default_panels(
            session, user_id=user_id, space_id=source.space_id
        )
    return {kind_name: st.as_dict() for kind_name, st in stats.items()}


async def _apply_status_rules(
    session: AsyncSession,
    user_id: str,
    source_id: str,
    rows: dict[str, tuple[DomainRecord, Mapping[str, Any]]],
) -> dict[str, tuple[DomainRecord, Mapping[str, Any]]]:
    """The deadlines with the student's status rules applied: the parser's state is kept as
    `source_state`, a rule (when one names the status text) decides `state` over it."""
    rules = await overrides.rules_of(session, user_id, source_ids=[source_id])
    out: dict[str, tuple[DomainRecord, Mapping[str, Any]]] = {}
    for external_id, (record, fields) in rows.items():
        parsed = fields.get("state")
        ruled = overrides.rule_state(rules, source_id, fields.get("state_text")) if rules else None
        out[external_id] = (record, {**fields, "source_state": parsed, "state": ruled or parsed})
    return out


async def _sweep(
    session: AsyncSession,
    *,
    user_id: str,
    source: Source,
    kind: str,
    model: type[Any],
    rows: Mapping[str, tuple[DomainRecord, Mapping[str, Any]]],
    revision: int,
) -> int:
    """Delete the rows of one kind of this source that another parser version left and this run
    did not see; returns how many. Only `source_id` rows are ever selected (v3 rows never)."""
    conditions = [
        model.user_id == user_id,
        model.source_id == source.id,
        or_(model.spec_revision.is_(None), model.spec_revision != revision),
    ]
    column = _WINDOW_COLUMNS.get(kind)
    if column is not None:
        dates = [fields[column] for _, fields in rows.values() if fields.get(column) is not None]
        if not dates:
            return 0
        attr = getattr(model, column)
        conditions.append(and_(attr >= min(dates), attr <= max(dates)))
    seen = set(rows)
    stale = [row for row in await session.scalars(select(model).where(*conditions))]
    stale = [row for row in stale if row.external_id not in seen]
    if not stale:
        return 0
    if kind == "deadlines":
        await _carry_marks(session, user_id=user_id, source=source, stale=stale)
    for row in stale:
        await session.delete(row)
    await session.flush()
    return len(stale)


async def _carry_marks(
    session: AsyncSession, *, user_id: str, source: Source, stale: Sequence[Any]
) -> None:
    """Hand the student's mark of a task about to be swept to its surviving copy in the same
    university (same dedupe key) when that copy has no mark of its own."""
    marks = {
        keys.deadline_key(row, source.space_id): row.user_state for row in stale if row.user_state
    }
    if not marks:
        return
    gone = {row.id for row in stale}
    survivors = await session.scalars(
        select(Deadline)
        .join(Source, Source.id == Deadline.source_id)
        .where(
            Deadline.user_id == user_id,
            Source.user_id == user_id,
            Source.space_id == source.space_id,
            Deadline.user_state.is_(None),
        )
    )
    for row in survivors:
        mark = marks.get(keys.deadline_key(row, source.space_id))
        if mark is not None and row.id not in gone:
            row.user_state = mark
