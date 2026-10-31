"""Fixtures for RAG tests: a disposable user seeded per test and removed afterwards.

RAG ingestion commits its own transactions (the runner calls it without an outer transaction), so
these tests cannot use the rolled-back `db` fixture. The `rag` fixture removes its users at the end;
all RAG rows hang off `users.id` with `ON DELETE CASCADE`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from botai.db.models import User
from botai.search import pgvector
from tests.search.support import RagSeed, SessionFactory


@pytest.fixture(autouse=True)
def reset_pgvector_state() -> Iterator[None]:
    """The detection is cached per process; every test starts from a fresh catalog read."""
    pgvector.reset_state_cache()
    yield
    pgvector.reset_state_cache()


@pytest.fixture
async def rag(engine: AsyncEngine) -> AsyncIterator[RagSeed]:
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    seed = RagSeed(sessions=sessions)
    try:
        yield seed
    finally:
        async with sessions() as session:
            for user_id in seed.users:
                await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


@pytest.fixture
def sessions(rag: RagSeed) -> SessionFactory:
    return rag.sessions
