"""The small mutable state of reading one source: cursor, site map and found endpoints.

A field left out (`None`) is untouched; pass `{}` or `[]` to clear a value to its empty shape. The
state is one row per source, so an upsert is a single row read and update.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import SourceState
from botai.sources.v4 import store


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _object(raw: object | None, what: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _validation(f"{what} должен быть объектом")
    return dict(raw)


def _endpoints(raw: Sequence[object] | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _validation("Эндпоинт должен быть объектом")
        result.append(dict(item))
    return result


async def get_source_state(
    session: AsyncSession, *, user_id: str, source_id: str
) -> SourceState | None:
    """The state row of one source, or None while nothing has been read yet."""
    await store.owned_source(session, user_id, source_id)
    return await session.get(SourceState, source_id)


async def upsert_source_state(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    last_revision: int | None = None,
    cursor: Mapping[str, Any] | None = None,
    site_map: Mapping[str, Any] | None = None,
    endpoints: Sequence[Mapping[str, Any]] | None = None,
) -> SourceState:
    """Create or update the state row; a field left out (`None`) is untouched."""
    await store.owned_source(session, user_id, source_id)
    state = await session.get(SourceState, source_id)
    if state is None:
        state = SourceState(source_id=source_id)
        session.add(state)
    if last_revision is not None:
        if last_revision < 0:
            raise _validation("last_revision не может быть отрицательным")
        state.last_revision = last_revision
    if cursor is not None:
        state.cursor = _object(cursor, "cursor")
    if site_map is not None:
        state.site_map = _object(site_map, "site_map")
    if endpoints is not None:
        state.endpoints = _endpoints(endpoints)
    await session.flush()
    return state
