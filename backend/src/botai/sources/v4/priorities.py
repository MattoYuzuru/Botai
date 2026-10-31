"""Per-kind priority of sources of one university: the first live source wins a conflict.

The order is a setting (03-sources-and-ux §4.1), not a ceiling: any number of sources can be
listed. The service validates that every listed source belongs to the same space and user and
keeps the caller's order; duplicates are dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Source, SourcePriority
from botai.sources.v4 import kinds as kinds_vocab
from botai.sources.v4 import store

LIVE_STATUSES = frozenset({"active", "syncing", "stale"})


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _kind(raw: str) -> str:
    value = raw.strip()
    if value not in kinds_vocab.ALL_KINDS:
        raise _validation(f"Неизвестный вид источника: {value}")
    return value


async def set_priority(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str,
    kind: str,
    source_ids: Sequence[str],
) -> SourcePriority:
    """Store the order of sources for one kind; the first live source becomes the primary."""
    space = await store.owned_space(session, user_id, space_id)
    clean_kind = _kind(kind)
    ordered: list[str] = []
    for source_id in source_ids:
        if source_id in ordered:
            continue
        source = await session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.user_id == user_id,
                Source.space_id == space.id,
            )
        )
        if source is None:
            raise store.not_found("Источник не найден")
        ordered.append(source_id)
    row = await session.get(SourcePriority, (space.id, clean_kind))
    if row is None:
        row = SourcePriority(space_id=space.id, kind=clean_kind, source_ids=ordered)
        session.add(row)
    else:
        row.source_ids = ordered
    await session.flush()
    return row


async def get_priority(
    session: AsyncSession, *, user_id: str, space_id: str, kind: str
) -> list[str]:
    """The stored order for one kind (empty when the student has not chosen one)."""
    space = await store.owned_space(session, user_id, space_id)
    row = await session.get(SourcePriority, (space.id, _kind(kind)))
    return list(row.source_ids) if row is not None else []


async def primary_source(
    session: AsyncSession, *, user_id: str, space_id: str, kind: str
) -> Source | None:
    """The first live (active/syncing/stale) source of the order — the one that wins conflicts."""
    space = await store.owned_space(session, user_id, space_id)
    row = await session.get(SourcePriority, (space.id, _kind(kind)))
    if row is None:
        return None
    for source_id in row.source_ids:
        source = await session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.user_id == user_id,
                Source.space_id == space.id,
            )
        )
        if source is not None and source.status in LIVE_STATUSES:
            return source
    return None
