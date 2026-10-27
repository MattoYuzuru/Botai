"""Canary: run a candidate spec on the author's live session before it is allowed to replace
anything (FR-010), and compare what it reads with the last confirmed snapshot.

A candidate passes when the source answered in the shape the spec describes and every required
kind produced valid records. The comparison is by the key fields of each kind (title and time of a
class, title and due date of a deadline, ...), not by ids, so a repair that changes where ids come
from is still recognised as reading the same data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.specs import Kind
from botai.sources import normalize
from botai.sources.adapter import AdapterResult, Record, SyncContext
from botai.sources.extract import collapse
from botai.sources.factory import AdapterFactory

MATCH_RATIO = 0.6

# Columns of the domain tables that identify a record to a human, per kind.
KEY_COLUMNS: dict[Kind, tuple[str, ...]] = {
    "courses": ("title",),
    "scheduleEvents": ("title", "starts_at"),
    "assignments": ("title",),
    "deadlines": ("title", "due_at"),
    "grades": ("title", "value"),
    "people": ("name",),
    "locations": ("title",),
    "news": ("title",),
}
# Rows that are still ahead: past ones leave the window of a source and prove nothing.
_FUTURE_COLUMN: dict[Kind, str] = {"scheduleEvents": "starts_at", "deadlines": "due_at"}


@dataclass(slots=True)
class CanaryReport:
    ok: bool
    result: AdapterResult | None = None
    valid: dict[Kind, list[Record]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # codes and kinds, never values

    def count(self, kind: Kind) -> int:
        return len(self.valid.get(kind, []))


Verdict = Literal["match", "mismatch", "unverified"]


@dataclass(slots=True)
class Comparison:
    kind: Kind
    baseline: int
    matched: int
    verdict: Verdict

    @property
    def ratio(self) -> float:
        return self.matched / self.baseline if self.baseline else 0.0


def valid_records(result: AdapterResult, tz: ZoneInfo) -> dict[Kind, list[Record]]:
    """Records the Normalizer would accept, per kind."""
    out: dict[Kind, list[Record]] = {}
    for rec in result.records:
        if normalize.coerce(rec.kind, rec, tz) is not None:
            out.setdefault(rec.kind, []).append(rec)
    return out


async def run_canary(
    spec: dict[str, Any], sctx: SyncContext, *, require: Collection[Kind]
) -> CanaryReport:
    """Fetch with the candidate. Session expiry and transport errors propagate to the caller."""
    result = await AdapterFactory().create(spec).fetch(sctx)
    valid = valid_records(result, sctx.tz)
    problems = [f"{o.kind or o.endpoint_id}: {o.problem}" for o in result.structural_problems()]
    for kind in sorted(require):
        if not valid.get(kind):
            problems.append(f"{kind}: no valid records")
    return CanaryReport(ok=not problems, result=result, valid=valid, problems=problems)


# --- comparison with the last confirmed snapshot ----------------------------------------------


def _norm(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    if value is None:
        return ""
    return collapse(str(value)).casefold()


def key_of(kind: Kind, columns: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_norm(columns.get(col)) for col in KEY_COLUMNS[kind])


def keys_of(records: list[Record], kind: Kind, tz: ZoneInfo) -> Counter[tuple[str, ...]]:
    keys: Counter[tuple[str, ...]] = Counter()
    for rec in records:
        row = normalize.coerce(kind, rec, tz)
        if row is not None:
            keys[key_of(kind, row)] += 1
    return keys


async def load_baseline(
    session: AsyncSession, connection_id: str, last_sync_at: datetime | None, now: datetime
) -> dict[Kind, Counter[tuple[str, ...]]]:
    """Key tuples of the rows the last successful sync saw (the confirmed snapshot)."""
    if last_sync_at is None:
        return {}
    out: dict[Kind, Counter[tuple[str, ...]]] = {}
    for kind, model in normalize.MODELS.items():
        stmt = select(model).where(
            model.connection_id == connection_id, model.seen_at >= last_sync_at
        )
        future = _FUTURE_COLUMN.get(kind)
        if future:
            stmt = stmt.where(getattr(model, future) >= now)
        keys: Counter[tuple[str, ...]] = Counter()
        for row in (await session.execute(stmt)).scalars():
            keys[key_of(kind, {c: getattr(row, c) for c in KEY_COLUMNS[kind]})] += 1
        if keys:
            out[kind] = keys
    return out


def compare(
    kind: Kind,
    baseline: Counter[tuple[str, ...]] | None,
    records: list[Record],
    tz: ZoneInfo,
) -> Comparison:
    """How much of the confirmed snapshot the candidate reads again. Without anything comparable
    the candidate is `unverified`: the student, not the machine, has to confirm it."""
    total = sum(baseline.values()) if baseline else 0
    if not baseline or total == 0:
        return Comparison(kind, 0, 0, "unverified")
    matched = sum((baseline & keys_of(records, kind, tz)).values())
    verdict: Verdict = "match" if matched / total >= MATCH_RATIO else "mismatch"
    return Comparison(kind, total, matched, verdict)
