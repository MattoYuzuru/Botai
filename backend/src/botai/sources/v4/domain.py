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
  skipped, not silently dropped.

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
from botai.sources.normalize import MODELS, lock_connection
from botai.sources.v4 import kinds as kinds_vocab

_CHUNK = 500
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


async def write_records(
    session: AsyncSession,
    *,
    user_id: str,
    source: Source,
    records: Sequence[DomainRecord],
    now: datetime,
    revision: int | None = None,
) -> dict[str, dict[str, int]]:
    """Upsert the records of one sync into the domain; returns counts per v4 kind.

    The caller owns the transaction (the runner commits status/state/artifacts together with these
    rows). A record whose kind has no domain table is counted as skipped.
    """
    if source.user_id != user_id:
        # The runner already resolved the source through the owner-scoped service; a mismatch here
        # would mean a foreign source object reached a write path: refuse instead of writing it.
        raise BotaiError(ErrorCode.NOT_FOUND, "Источник не найден")
    if not records:
        return {}
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
        model = MODELS[reading]
        rows: dict[str, tuple[DomainRecord, Mapping[str, Any]]] = {}
        for record in kind_records:
            if not record.external_id or not record.fields:
                st.skipped += 1
                continue
            rows[record.external_id] = (record, record.fields)
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
                    )
                )
                st.inserted += 1
                continue
            material = kind_name == "schedule" and _has_material_change(row, fields)
            changed = [column for column, value in fields.items() if getattr(row, column) != value]
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
    return {kind_name: st.as_dict() for kind_name, st in stats.items()}
