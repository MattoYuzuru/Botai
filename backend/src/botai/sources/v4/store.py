"""Row access for the v4 source service.

Every lookup carries the user predicate: a foreign id is reported as not found, never as
forbidden, so one student cannot learn that another's source exists. Functions here only read and
lock; validation, transactions and default values belong to the service modules.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Playbook, Source, Space


def not_found(what: str) -> BotaiError:
    return BotaiError(ErrorCode.NOT_FOUND, what)


async def owned_space(session: AsyncSession, user_id: str, space_id: str) -> Space:
    """The space of this user; a neighbour's space and a missing one look the same."""
    space = await session.scalar(
        select(Space).where(Space.id == space_id, Space.user_id == user_id)
    )
    if space is None:
        raise not_found("Пространство не найдено")
    return space


async def owned_source(
    session: AsyncSession, user_id: str, source_id: str, *, for_update: bool = False
) -> Source:
    """The source of this user (locked on request); a foreign one looks missing."""
    statement = select(Source).where(Source.id == source_id, Source.user_id == user_id)
    if for_update:
        statement = statement.with_for_update()
    source = await session.scalar(statement)
    if source is None:
        raise not_found("Источник не найден")
    return source


async def visible_playbook(
    session: AsyncSession, user_id: str, playbook_id: str, *, for_update: bool = False
) -> Playbook:
    """A playbook this user may use: their own or a shared one (`owner_id IS NULL`, D-018)."""
    statement = select(Playbook).where(
        Playbook.id == playbook_id,
        (Playbook.owner_id.is_(None)) | (Playbook.owner_id == user_id),
    )
    if for_update:
        statement = statement.with_for_update()
    playbook = await session.scalar(statement)
    if playbook is None:
        raise not_found("Playbook не найден")
    return playbook


async def source_by_identity(
    session: AsyncSession, space_id: str, identity_key: str
) -> Source | None:
    """The source with this identity inside the space, if any (uniqueness is per space)."""
    return await session.scalar(
        select(Source).where(Source.space_id == space_id, Source.identity_key == identity_key)
    )
