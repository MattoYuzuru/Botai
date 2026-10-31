"""Fixtures of the action engine tests.

The DB/app rig is the agent tests' one (one connection in a rolled-back transaction, a created
FastAPI app with an in-memory queue); the action engine additionally gets the synthetic
university as an ASGI transport, so an executed HTTP action is a real request to `fakeuni` —
L1, no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from botai.actions.engine import ActionEngine
from tests.actions.support import ORIGIN, RUN_ID, WRITE_URL
from tests.agent.conftest import add_user, api, sm, user_id

__all__ = [
    "ORIGIN",
    "RUN_ID",
    "WRITE_URL",
    "action_engine",
    "add_user",
    "api",
    "deep_writes",
    "sm",
    "user_id",
]


@pytest.fixture(autouse=True)
def _clean_fakeuni() -> Iterator[None]:
    """`fakeuni` counts writes in a module global: reset it around every action test."""
    from tests.fakeuni import deep

    deep.reset()
    yield
    deep.reset()


@pytest.fixture
def transport(fakeuni_app: object) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fakeuni_app)  # type: ignore[arg-type]


@pytest.fixture
def action_engine(api: Any, transport: httpx.ASGITransport) -> ActionEngine:
    return ActionEngine(
        db=api.sm,
        clock=api.services.clock.now,
        http_transport=transport,
    )


def deep_writes() -> int:
    from tests.fakeuni import deep

    return deep.STATE.writes
