from __future__ import annotations

import httpx
import pytest

from botai.core.clock import FrozenClock
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm, make_connection, make_space, signup
from tests.domain.helpers import World


@pytest.fixture
async def world(client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock) -> World:
    acc = await signup(client, tx_sessionmaker, clock)
    sa = await make_space(tx_sessionmaker, acc.id, "Вуз А")
    sb = await make_space(tx_sessionmaker, acc.id, "Вуз Б")
    ca = await make_connection(tx_sessionmaker, acc.id, sa, origin="https://a.example.org")
    cb = await make_connection(tx_sessionmaker, acc.id, sb, origin="https://b.example.org")
    return World(client, acc.id, sa, sb, ca, cb)
