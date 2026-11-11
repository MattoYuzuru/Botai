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
* a complete run does not delete rows it did not see (same policy as the v3 normalizer): their old
  `seen_at` is the honest "last time this source had it";
* a kind without a domain table (`documents`, `messages`) stays in the run report and is counted as
  skipped, not silently dropped;
* after the rows of `grades`, `profile` or `ranking` are written, the system panels of the
  university are ensured (`panels.service.ensure_default_panels`, idempotent, D-039);
* the student's status rules decide the `state` of a deadline whose `state_text` they name, over
  the parser's state (`domain.overrides`, D-045); the student's own mark (`user_state`) is never
  written here;
* `changed_at` of a grade or a deadline (the "Новое" feed, docs/v4/27 §5.5) is set to `now` when a
  row appears after the source's first successful sync — and after the first sync that brought
  this kind at all, so a playbook that learns to read grades does not flood the feed with history —
  and when a grade's value or a deadline's state changes. Nothing else touches it.

Writers of one source are serialized by an advisory transaction lock, so two concurrent syncs of
one source cannot insert the same natural key twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Source
from botai.domain import overrides
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

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
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


async def _has_rows(session: AsyncSession, model: type[Any], source_id: str) -> bool:
    found = await session.scalar(select(model.id).where(model.source_id == source_id).limit(1))
    return found is not None


async def write_records(
    session: AsyncSession,
    *,
    user_id: str,
    source: Source,
    records: Sequence[DomainRecord],
    now: datetime,
    revision: int | None = None,
    synced_before: bool | None = None,
) -> dict[str, dict[str, int]]:
    """Upsert the records of one sync into the domain; returns counts per v4 kind.

    The caller owns the transaction (the runner commits status/state/artifacts together with these
    rows). A record whose kind has no domain table is counted as skipped. `synced_before` says
    whether the source had a successful sync before this one (default: its `last_sync_at`, which
    the runner reads before marking this sync).
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
        # a new row is news only once this source and this kind were read before
        new_is_news = (
            kind_name in _FEED_KINDS
            and synced_before
            and await _has_rows(session, model, source.id)
        )
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
            if _is_news(kind_name, row, fields, changed):
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
    await session.flush()
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
    """The deadlines with the student's status rules applied: a rule beats the parser's state."""
    rules = await overrides.rules_of(session, user_id, source_ids=[source_id])
    if not rules:
        return rows
    out: dict[str, tuple[DomainRecord, Mapping[str, Any]]] = {}
    for external_id, (record, fields) in rows.items():
        state = overrides.rule_state(rules, source_id, fields.get("state_text"))
        out[external_id] = (
            record,
            fields if state is None or state == fields.get("state") else {**fields, "state": state},
        )
    return out
