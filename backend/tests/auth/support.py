"""Shared fixtures and helpers for W1 tests (users, rolled-back transactions).

Fixtures are re-exported by the conftest of each W1 test package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import User


@pytest.fixture
async def tx_sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that share one connection and one outer transaction, rolled back after the test.

    Code under test may open its own sessions and commit: the commit only releases a savepoint.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await trans.rollback()


async def make_user(
    sm: async_sessionmaker[AsyncSession], email: str | None = None, *, is_owner: bool = False
) -> str:
    user_id = new_id()
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=email or f"{user_id.lower()}@example.test",
                display_name="Test",
                password_hash="x",
                is_owner=is_owner,
            )
        )
        await session.commit()
    return user_id
