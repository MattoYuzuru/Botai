from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login


@pytest.fixture
def sm(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Sessions that share the test's rolled-back transaction (handlers `commit` into savepoints)."""
    return async_sessionmaker(
        bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture(autouse=True)
def _reset_fakeuni() -> Iterator[None]:
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
    yield
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
