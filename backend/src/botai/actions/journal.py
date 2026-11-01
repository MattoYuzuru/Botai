"""Readable action-journal views: the human title of the source an action belongs to (task-14).

`actions_log` stores `source_id` (and the policy scope), so a journal row alone names a scope kind,
not a source. The owner sees a list of actions in the UI and needs the same human name the source
card shows. This module resolves those names in **two batched queries at most** — one for the v4
`sources`, one for the v3 `connections` — for a whole page of journal rows, and only for the
current user: a source id that belongs to another account (or no longer exists) resolves to nothing.

The journal is historical: a title is shown while the source row exists; a deleted source keeps its
`source_id`/scope as provenance but shows no title instead of inventing one.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.actions.engine import JournalEntry
from botai.db.models import Connection, Source


def related_source_id(entry: JournalEntry) -> str | None:
    """The source a journal row belongs to: explicit `source_id`, else a source-scoped id.

    A row may be tied to a source while the effective policy scope is broader (e.g. the owner has
    only a global policy): the explicit `source_id` still names the source. A source-scoped row
    without `source_id` (older/imported data) falls back to `scope_id`.
    """
    if entry.source_id:
        return entry.source_id
    if entry.scope_kind == "source" and entry.scope_id:
        return entry.scope_id
    return None


def source_title_of(entry: JournalEntry, titles: dict[str, str]) -> str | None:
    """The resolved title for one entry, or `None` when the row has no readable source."""
    source_id = related_source_id(entry)
    return titles.get(source_id) if source_id else None


async def resolve_source_titles(
    session: AsyncSession,
    *,
    user_id: str,
    source_ids: Iterable[str | None],
) -> dict[str, str]:
    """Batch `id -> title` for the user's v4 sources, then the v3 connections for the rest.

    Two queries total for any page size; ids of other users are excluded by `user_id`, so a title
    never leaks a foreign source. Missing ids (deleted source, wrong id) are simply absent.
    """
    ids = sorted({source_id for source_id in source_ids if source_id})
    if not ids:
        return {}
    titles: dict[str, str] = {}
    rows = (
        await session.execute(
            select(Source.id, Source.title).where(Source.id.in_(ids), Source.user_id == user_id)
        )
    ).all()
    for source_id, title in rows:
        titles[str(source_id)] = str(title)
    missing = [source_id for source_id in ids if source_id not in titles]
    if missing:
        legacy = (
            await session.execute(
                select(Connection.id, Connection.title).where(
                    Connection.id.in_(missing), Connection.user_id == user_id
                )
            )
        ).all()
        for connection_id, title in legacy:
            titles[str(connection_id)] = str(title)
    return titles


__all__ = ["related_source_id", "resolve_source_titles", "source_title_of"]
