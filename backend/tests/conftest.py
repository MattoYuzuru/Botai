"""Shared fixtures. Each test session gets its own Postgres database (parallel agents safe)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ADMIN_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://botai:botai@127.0.0.1:55432/botai")
REQUIRE_DB = os.environ.get("BOTAI_REQUIRE_DB") == "1"
BACKEND = Path(__file__).resolve().parents[1]


def _with_db(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture(scope="session")
async def database_url() -> AsyncIterator[str]:
    name = f"botai_test_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OSError as exc:  # connection refused
        await admin.dispose()
        if REQUIRE_DB:
            raise
        pytest.skip(f"Postgres unavailable ({exc}); run `make db`")
    url = _with_db(ADMIN_URL, name)
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    import asyncio

    await asyncio.to_thread(command.upgrade, cfg, "head")
    try:
        yield url
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back after the test."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )()
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture(scope="session")
def fakeuni_app() -> Iterator[object]:
    from tests.fakeuni.app import create_fakeuni

    return create_fakeuni()
