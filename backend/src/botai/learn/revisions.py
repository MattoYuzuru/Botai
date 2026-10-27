"""Revisions of a spec: one active revision per `(origin, adapter)`, the old ones are kept.

Lifecycle (docs/architecture/contracts.md §2): `draft` -> `canary` -> `active` -> `deprecated` or
`rolled_back`. A revision becomes active only after the canary on the author's session; the
previous active one is deprecated in the same transaction, under an advisory lock, so two jobs can
never leave an origin with two active revisions.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db import models as m

STATES = ("draft", "canary", "active", "deprecated", "rolled_back")


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


async def activate(session: AsyncSession, row: m.AdapterSpecRow) -> m.AdapterSpecRow | None:
    """Make `row` the active revision; returns the one it replaced (now `deprecated`)."""
    await lock_origin(session, row.origin, row.adapter)
    previous = await active_row(session, row.origin, row.adapter)
    if previous is not None and previous.id != row.id:
        previous.state = "deprecated"
    else:
        previous = None
    row.state = "active"
    await session.flush()
    return previous
