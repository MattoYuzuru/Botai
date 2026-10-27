"""Canary: run a candidate spec on the author's live session before it is allowed to replace
anything (FR-010). A candidate passes when the source answered in the shape the spec describes and
every required kind produced valid records. Comparison with the last confirmed snapshot lives in
`learn/revisions.py`."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from botai.contracts.specs import Kind
from botai.sources import normalize
from botai.sources.adapter import AdapterResult, Record, SyncContext
from botai.sources.factory import AdapterFactory


@dataclass(slots=True)
class CanaryReport:
    ok: bool
    result: AdapterResult | None = None
    valid: dict[Kind, list[Record]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # codes and kinds, never values

    def count(self, kind: Kind) -> int:
        return len(self.valid.get(kind, []))


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
