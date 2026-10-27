"""Fixtures for the agent tests. L0/L1 never read the real key: settings ignore every env file."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import User
from botai.settings import Settings

TEST_KEY = "sk-test-DO-NOT-LEAK-0123456789"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "deepseek",
        "deepseek_api_key": TEST_KEY,
        "env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """L2 (`live_llm`) spends real money: explicit opt-in only, otherwise skipped with a reason."""
    if os.environ.get("BOTAI_LIVE_LLM") == "1":
        return
    skip = pytest.mark.skip(reason="L2: set BOTAI_LIVE_LLM=1 (and BOTAI_ENV_FILE) to spend money")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def sm(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that share one connection inside a transaction rolled back after the test.

    Code under test may call `session.commit()`: it only releases a savepoint.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await trans.rollback()


async def add_user(sm: async_sessionmaker[AsyncSession], name: str = "student") -> str:
    user_id = new_id()
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=f"{name}-{user_id.lower()}@example.test",
                display_name=name,
                password_hash="x",
            )
        )
        await session.commit()
    return user_id


@pytest.fixture
async def user_id(sm: async_sessionmaker[AsyncSession]) -> str:
    return await add_user(sm)
