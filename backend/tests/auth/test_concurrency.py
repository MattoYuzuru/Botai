"""Real commits on separate connections: the invite must hold under a race."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.auth import service
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError
from botai.db.models import User
from tests.auth.support import NOW, PASSWORD, make_user

pytestmark = pytest.mark.db


@pytest.fixture
async def committed(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real transactions; the users the test creates are removed afterwards (FKs cascade)."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        before = set((await s.execute(select(User.id))).scalars())
    yield sm
    async with sm() as s:
        await s.execute(delete(User).where(User.id.not_in(before)))
        await s.commit()


async def test_two_simultaneous_registrations_cannot_share_one_invite(
    committed: async_sessionmaker[AsyncSession],
) -> None:
    owner = await make_user(committed, is_owner=True)
    async with committed() as s:
        code, _ = await service.create_invite(s, created_by=owner, now=NOW)

    async def attempt(n: int) -> str:
        async with committed() as s:
            try:
                await service.register(
                    s,
                    invite_code=code,
                    email=f"racer{n}@example.com",
                    display_name="R",
                    password=PASSWORD,
                    now=NOW,
                )
            except BotaiError as exc:
                return exc.code.value
            return "ok"

    results = await asyncio.gather(*(attempt(n) for n in range(6)))
    assert results.count("ok") == 1, results
    assert set(results) <= {"ok", "forbidden"}
    async with committed() as s:
        racers = (await s.execute(select(User.email).where(User.email.like("racer%")))).all()
    assert len(racers) == 1  # losers left no half-created accounts behind


async def test_sessions_resolve_across_connections(
    committed: async_sessionmaker[AsyncSession],
) -> None:
    from botai.auth.deps import resolve_session

    uid = await make_user(committed, "durable@example.com")
    clock = FrozenClock(NOW)
    async with committed() as s:
        token = await service.open_session(s, user_id=uid, now=clock.now(), ttl_days=30)
    async with committed() as other:
        user = await resolve_session(other, token, clock.now())
    assert user is not None
    assert user.id == uid
