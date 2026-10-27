"""Fixtures for the agent tests. L0/L1 never read the real key: settings ignore every env file."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.api.deps import get_services
from botai.auth.deps import CurrentUser, current_user
from botai.core.ids import new_id
from botai.core.llm import LlmResponse, LlmUsage, ScriptedLlm
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.db.models import User
from botai.db.session import db_session
from botai.main import create_app
from botai.settings import Settings

TEST_KEY = "sk-test-DO-NOT-LEAK-0123456789"

Sm = async_sessionmaker[AsyncSession]


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


@dataclass
class Api:
    app: FastAPI
    sm: Sm
    queue: InMemoryJobQueue
    services: Services
    user_id: str

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://testserver"
        )

    def login_as(self, user_id: str) -> None:
        self.app.dependency_overrides[current_user] = lambda: CurrentUser(
            id=user_id, email="s@example.test", is_owner=False
        )


@pytest.fixture
async def api(sm: Sm, user_id: str) -> AsyncIterator[Api]:
    app = create_app()
    queue = InMemoryJobQueue()
    llm = ScriptedLlm(
        queue=[LlmResponse(content="Ответ.", usage=LlmUsage(input_cache_miss=10, output=2))]
    )
    from tests.agent.support import make_services

    services = make_services(llm, sm=sm, queue=queue)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[db_session] = _db
    app.dependency_overrides[get_services] = lambda: services
    result = Api(app, sm, queue, services, user_id)
    result.login_as(user_id)
    return result
