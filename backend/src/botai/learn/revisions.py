"""Revisions of a spec: one active revision per `(origin, adapter)`, the old ones are kept.

Lifecycle (docs/architecture/contracts.md §2): `draft` -> `canary` -> `active` -> `deprecated` or
`rolled_back`. A revision becomes active only after the canary on the author's session; the
previous active one is deprecated in the same transaction, under an advisory lock, so two jobs can
never leave an origin with two active revisions.

Health of a revision is kept in `audit_log` (no new columns): `spec_drift` when a sync found the
answer no longer matching it, `spec_ok` when one matched again, `canary` for a check on a session
and `canary_scheduled` for a check that was queued. Three drifts in a row roll a revision back to
the previous one (security.md T6).
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.jobs import JobKind
from botai.core.queue import JobQueue
from botai.db import models as m
from botai.sources.scope import covered_kinds

STATES = ("draft", "canary", "active", "deprecated", "rolled_back")
AUDIT_DRIFT = "spec_drift"
AUDIT_OK = "spec_ok"
AUDIT_CANARY = "canary"
AUDIT_SCHEDULED = "canary_scheduled"
AUDIT_ROLLBACK = "spec_rollback"
ROLLBACK_AFTER = 3  # drifts in a row
CANARY_EVERY = timedelta(hours=24)
CANARY_FALLBACK = timedelta(hours=48)  # when the author is not available
SYNC_CANARY_THROTTLE = timedelta(hours=6)


def author_digest(user_id: str) -> str:
    """Pseudonymous id of the spec's author for the canary (does not reveal who it is)."""
    return hashlib.sha256(f"author|{user_id}".encode()).hexdigest()


async def lock_origin(session: AsyncSession, origin: str, adapter: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"spec:{adapter}:{origin}"}
    )


async def active_row(session: AsyncSession, origin: str, adapter: str) -> m.AdapterSpecRow | None:
    return (
        await session.execute(
            select(m.AdapterSpecRow).where(
                m.AdapterSpecRow.origin == origin,
                m.AdapterSpecRow.adapter == adapter,
                m.AdapterSpecRow.state == "active",
            )
        )
    ).scalar_one_or_none()


async def latest_revision(session: AsyncSession, origin: str, adapter: str) -> int:
    value = (
        await session.execute(
            select(func.max(m.AdapterSpecRow.revision)).where(
                m.AdapterSpecRow.origin == origin, m.AdapterSpecRow.adapter == adapter
            )
        )
    ).scalar_one()
    return int(value or 0)


async def add_revision(
    session: AsyncSession,
    *,
    origin: str,
    adapter: str,
    spec: dict[str, Any],
    state: str = "draft",
    digest: str | None = None,
    notes: str | None = None,
) -> m.AdapterSpecRow:
    """Store a spec as the next revision of the origin. The number is assigned under the lock and
    written into the spec too, so the two never disagree."""
    await lock_origin(session, origin, adapter)
    revision = await latest_revision(session, origin, adapter) + 1
    stored = copy.deepcopy(spec)
    if adapter == "recipe":
        stored["recipe"]["revision"] = revision
    row = m.AdapterSpecRow(
        origin=origin,
        adapter=adapter,
        revision=revision,
        state=state,
        spec=stored,
        learned_by_digest=digest,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    return row


async def activate(
    session: AsyncSession, row: m.AdapterSpecRow, *, now: datetime | None = None
) -> m.AdapterSpecRow | None:
    """Make `row` the active revision; returns the one it replaced (now `deprecated`). The canary
    that preceded the activation is recorded, so the new revision starts with a fresh check."""
    await lock_origin(session, row.origin, row.adapter)
    previous = await active_row(session, row.origin, row.adapter)
    if previous is not None and previous.id != row.id:
        previous.state = "deprecated"
    else:
        previous = None
    row.state = "active"
    await session.flush()
    if now is not None:
        await record(session, AUDIT_CANARY, row, now, ok=True, via="activation")
    return previous


async def pin_readers(
    session: AsyncSession,
    previous: m.AdapterSpecRow | None,
    row: m.AdapterSpecRow,
    *,
    keep: str,
) -> int:
    """Pin the connections that read «every kind of the recipe» when `row` adds kinds to it.

    A connection without a durable scope reads whatever the active revision has. A revision that
    one student extends would make everybody else start reading the new kinds unasked, and a
    failure of such a kind would send their sources to repair and, three drifts later, roll the
    revision back for the student who asked. So in the same transaction, under the lock of the
    site, each of them gets the kinds the previous revision had as its explicit scope. `keep` is
    the connection that is being saved. Returns how many connections were pinned.
    """
    if previous is None or row.adapter != "recipe":
        return 0
    before = covered_kinds(previous.spec, None)
    if not set(covered_kinds(row.spec, None)) - set(before):
        return 0
    result = await session.execute(
        update(m.Connection)
        .where(
            m.Connection.origin == row.origin,
            m.Connection.adapter == row.adapter,
            m.Connection.id != keep,
            or_(
                m.Connection.data_scope.is_(None),
                func.jsonb_typeof(m.Connection.data_scope) == "null",
            ),
        )
        .values(data_scope=[str(kind) for kind in before])
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def rollback(
    session: AsyncSession, row: m.AdapterSpecRow, *, now: datetime | None = None
) -> m.AdapterSpecRow | None:
    """Retire an active revision and bring the latest earlier one back. None if there is none to
    return to (the revision then stays: a broken spec is still better than no spec)."""
    await lock_origin(session, row.origin, row.adapter)
    if row.state != "active":
        return None
    previous = (
        await session.execute(
            select(m.AdapterSpecRow)
            .where(
                m.AdapterSpecRow.origin == row.origin,
                m.AdapterSpecRow.adapter == row.adapter,
                m.AdapterSpecRow.revision < row.revision,
                m.AdapterSpecRow.state == "deprecated",
            )
            .order_by(m.AdapterSpecRow.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is None:
        return None
    row.state = "rolled_back"
    previous.state = "active"
    await session.flush()
    if now is not None:
        await record(session, AUDIT_ROLLBACK, row, now, to_revision=previous.revision)
    return previous


# --- health of a revision (audit_log) ---------------------------------------------------------


async def record(
    session: AsyncSession,
    kind: str,
    row: m.AdapterSpecRow,
    now: datetime,
    *,
    user_id: str | None = None,
    **detail: Any,
) -> None:
    session.add(
        m.AuditLog(
            user_id=user_id,
            kind=kind,
            detail={
                "spec_id": row.id,
                "origin": row.origin,
                "revision": row.revision,
                "at": now.isoformat(),
                **detail,
            },
        )
    )
    await session.flush()


async def _entries(
    session: AsyncSession, spec_id: str, kinds: tuple[str, ...], limit: int = 20
) -> list[m.AuditLog]:
    rows = await session.execute(
        select(m.AuditLog)
        .where(m.AuditLog.kind.in_(kinds), m.AuditLog.detail["spec_id"].as_string() == spec_id)
        .order_by(m.AuditLog.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def register_drift(
    session: AsyncSession, row: m.AdapterSpecRow, now: datetime, *, user_id: str
) -> bool:
    """Note that a sync found the answer not matching `row`. Returns True if this was the third
    drift in a row and the revision was rolled back to the previous one."""
    await record(session, AUDIT_DRIFT, row, now, user_id=user_id)
    streak = 0
    for entry in await _entries(session, row.id, (AUDIT_DRIFT, AUDIT_OK)):
        if entry.kind != AUDIT_DRIFT:
            break
        streak += 1
    if streak >= ROLLBACK_AFTER and row.state == "active":
        return await rollback(session, row, now=now) is not None
    return False


async def register_ok(
    session: AsyncSession, row: m.AdapterSpecRow, now: datetime, *, user_id: str
) -> None:
    """A sync matched `row` again: ends a streak of drifts, and counts as a canary (throttled)."""
    entries = await _entries(session, row.id, (AUDIT_DRIFT, AUDIT_OK, AUDIT_CANARY), limit=3)
    if entries and entries[0].kind == AUDIT_DRIFT:
        await record(session, AUDIT_OK, row, now, user_id=user_id)
    last = _latest_at([e for e in entries if e.kind == AUDIT_CANARY])
    if last is None or now - last >= SYNC_CANARY_THROTTLE:
        await record(session, AUDIT_CANARY, row, now, ok=True, via="sync")


def _latest_at(entries: list[m.AuditLog]) -> datetime | None:
    best: datetime | None = None
    for entry in entries:
        raw = entry.detail.get("at")
        if isinstance(raw, str):
            when = datetime.fromisoformat(raw)
            best = when if best is None or when > best else best
    return best


async def last_canary_at(session: AsyncSession, spec_id: str) -> datetime | None:
    return _latest_at(await _entries(session, spec_id, (AUDIT_CANARY, AUDIT_SCHEDULED), limit=5))


async def _plan_canaries(session: AsyncSession, now: datetime, limit: int) -> list[tuple[str, str]]:
    """(user_id, connection_id) of the checks to queue; marks them as scheduled."""
    specs = (
        (
            await session.execute(
                select(m.AdapterSpecRow).where(
                    m.AdapterSpecRow.adapter == "recipe", m.AdapterSpecRow.state == "active"
                )
            )
        )
        .scalars()
        .all()
    )
    plan: list[tuple[str, str]] = []
    for spec in specs:
        if len(plan) >= limit:
            break
        await lock_origin(session, spec.origin, spec.adapter)
        last = await last_canary_at(session, spec.id)
        if last is not None and now - last < CANARY_EVERY:
            continue
        connections = (
            (
                await session.execute(
                    select(m.Connection).where(
                        m.Connection.origin == spec.origin,
                        m.Connection.adapter == "recipe",
                        m.Connection.status.in_(("active", "stale")),
                    )
                )
            )
            .scalars()
            .all()
        )
        author = next(
            (c for c in connections if author_digest(c.user_id) == spec.learned_by_digest), None
        )
        chosen = author or (connections[0] if connections else None)
        if chosen is None or (author is None and last is not None and now - last < CANARY_FALLBACK):
            continue
        await record(session, AUDIT_SCHEDULED, spec, now, connection_id=chosen.id)
        plan.append((chosen.user_id, chosen.id))
    return plan


async def enqueue_due_canaries(
    db: async_sessionmaker[AsyncSession], queue: JobQueue, now: datetime, *, limit: int = 20
) -> int:
    """Queue a `repair` job in check mode for every active recipe spec that has had no canary for
    a day (two days if the author's connection is gone). Safe to call from any tick; the checks
    are marked as scheduled in one transaction and queued after it ends."""
    async with db() as session, session.begin():
        plan = await _plan_canaries(session, now, limit)
    for user_id, connection_id in plan:
        await queue.enqueue(
            JobKind.REPAIR,
            user_id=user_id,
            connection_id=connection_id,
            payload={"mode": "check", "reason": "canary"},
        )
    return len(plan)
