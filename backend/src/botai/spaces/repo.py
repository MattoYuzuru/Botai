"""Tenant-scoped reads and deletes of spaces and connections (NFR-001).

`user_id` is always the first argument and always part of the predicate. A row that belongs to
someone else is reported exactly like a row that does not exist: 404, never 403.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Connection, Job, SecretRow, Space


async def get_space(session: AsyncSession, user_id: str, space_id: str) -> Space:
    space = await session.scalar(
        select(Space).where(Space.id == space_id, Space.user_id == user_id)
    )
    if space is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")
    return space


async def list_spaces(session: AsyncSession, user_id: str) -> list[Space]:
    rows = await session.scalars(
        select(Space).where(Space.user_id == user_id).order_by(Space.created_at, Space.id)
    )
    return list(rows)


async def get_connection(session: AsyncSession, user_id: str, connection_id: str) -> Connection:
    conn = await session.scalar(
        select(Connection).where(Connection.id == connection_id, Connection.user_id == user_id)
    )
    if conn is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
    return conn


async def list_connections(session: AsyncSession, user_id: str, space_id: str) -> list[Connection]:
    rows = await session.scalars(
        select(Connection)
        .where(Connection.user_id == user_id, Connection.space_id == space_id)
        .order_by(Connection.created_at, Connection.id)
    )
    return list(rows)


async def purge_connection_secrets(
    session: AsyncSession, user_id: str, connection_ids: Sequence[str]
) -> None:
    """`secrets.connection_id` has no foreign key (it also holds job ids), so deleting a
    connection must remove its secrets, and the transient answers of its jobs, by hand."""
    if not connection_ids:
        return
    job_ids = select(Job.id).where(Job.user_id == user_id, Job.connection_id.in_(connection_ids))
    await session.execute(
        delete(SecretRow).where(
            SecretRow.user_id == user_id,
            SecretRow.connection_id.in_(connection_ids) | SecretRow.connection_id.in_(job_ids),
        )
    )
