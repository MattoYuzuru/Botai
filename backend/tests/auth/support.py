"""Shared fixtures and helpers for W1 tests (users, rolled-back transactions, an API client).

Fixtures are re-exported by the conftest of each W1 test package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.api.deps import get_clock
from botai.auth import security, service
from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.core.vault import generate_kek_b64
from botai.db.models import User
from botai.db.session import db_session
from botai.main import create_app
from botai.settings import Settings, get_settings

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"

Sm = async_sessionmaker[AsyncSession]


@pytest.fixture
async def tx_sessionmaker(engine: AsyncEngine) -> AsyncIterator[Sm]:
    """Sessions that share one connection and one outer transaction, rolled back after the test.

    Code under test may open its own sessions and commit: the commit only releases a savepoint.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await trans.rollback()


@pytest.fixture(autouse=True)
def cheap_hasher(monkeypatch: pytest.MonkeyPatch) -> None:
    """argon2 at full cost makes the suite slow; the algorithm and format stay argon2id."""
    monkeypatch.setattr(
        security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", botai_kek=generate_kek_b64())  # type: ignore[arg-type]


@pytest.fixture
def app(tx_sessionmaker: Sm, settings: Settings, clock: FrozenClock) -> FastAPI:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with tx_sessionmaker() as session:
            yield session

    app.dependency_overrides[db_session] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: clock
    return app


@pytest.fixture
async def new_client(app: FastAPI) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    """Each call returns a client with its own cookie jar, i.e. its own browser."""
    opened: list[httpx.AsyncClient] = []

    def make() -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")
        opened.append(c)
        return c

    yield make
    for c in opened:
        await c.aclose()


@pytest.fixture
def client(new_client: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
    return new_client()


async def make_user(
    sm: Sm,
    email: str | None = None,
    *,
    is_owner: bool = False,
    password: str | None = None,
    display_name: str = "Test",
) -> str:
    user_id = new_id()
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=email or f"{user_id.lower()}@example.com",
                display_name=display_name,
                password_hash=await security.hash_password(password) if password else "x",
                is_owner=is_owner,
            )
        )
        await session.commit()
    return user_id


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    email: str
    password: str


async def issue_invite(sm: Sm, clock: FrozenClock, created_by: str | None = None) -> str:
    async with sm() as session:
        if created_by is None:
            created_by = await make_user(sm, is_owner=True)
        code, _ = await service.create_invite(session, created_by=created_by, now=clock.now())
    return code


async def signup(
    client: httpx.AsyncClient,
    sm: Sm,
    clock: FrozenClock,
    *,
    email: str | None = None,
    password: str = PASSWORD,
    owner_id: str | None = None,
) -> Account:
    """Registers a user through the API by invite; the client keeps the session cookie."""
    code = await issue_invite(sm, clock, owner_id)
    email = email or f"{new_id().lower()}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"inviteCode": code, "email": email, "displayName": "Студент", "password": password},
    )
    assert r.status_code == 201, r.text
    return Account(id=r.json()["user"]["id"], email=email, password=password)
