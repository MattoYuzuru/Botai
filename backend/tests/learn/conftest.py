from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.llm import LlmResponse
from tests.learn import support as ls
from tests.learn.env import PRESET, Env
from tests.sources.conftest import _reset_fakeuni, sm  # noqa: F401  (shared fixtures)
from tests.sources.support import FakeSessions, seed_connection

EnvFactory = Callable[..., Awaitable[Env]]


@pytest.fixture
async def make_env(
    db: AsyncSession,
    sm: async_sessionmaker[AsyncSession],  # noqa: F811
    fakeuni_app: object,
) -> EnvFactory:
    async def build(
        responses: list[LlmResponse],
        *,
        with_spec: bool = False,
        home_extra: str = "",
        sessions: FakeSessions | None = None,
        spec: dict[str, Any] | None = None,
        **seed: Any,
    ) -> Env:
        status = seed.pop("status", "active" if with_spec else "draft")
        seeded = await seed_connection(
            db,
            spec=spec,
            with_spec=with_spec,
            status=status,
            preset=PRESET,
            university=True,
            **seed,
        )
        browser = await ls.fake_site(fakeuni_app, home_extra=home_extra)
        env = Env(db, sm, fakeuni_app, seeded, responses, browser, sessions=sessions)
        await env.prepare()
        return env

    return build
