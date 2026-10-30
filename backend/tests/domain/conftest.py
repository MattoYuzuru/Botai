from __future__ import annotations

import httpx
import pytest

from botai.core.clock import FrozenClock
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm, make_connection, make_space, signup
from tests.domain.helpers import Campus, World, add_source


@pytest.fixture
async def world(client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock) -> World:
    acc = await signup(client, tx_sessionmaker, clock)
    sa = await make_space(tx_sessionmaker, acc.id, "Вуз А")
    sb = await make_space(tx_sessionmaker, acc.id, "Вуз Б")
    ca = await make_connection(tx_sessionmaker, acc.id, sa, origin="https://a.example.org")
    cb = await make_connection(tx_sessionmaker, acc.id, sb, origin="https://b.example.org")
    return World(client, acc.id, sa, sb, ca, cb)


@pytest.fixture
async def campus(client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock) -> Campus:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id, "Вуз А")
    other = await make_space(tx_sessionmaker, acc.id, "Вуз Б")
    cabinet = await add_source(tx_sessionmaker, acc.id, space, "Кабинет", age_days=3)
    calendar = await add_source(
        tx_sessionmaker, acc.id, space, "Календарь", age_days=1, kind="calendar"
    )
    elsewhere = await add_source(tx_sessionmaker, acc.id, other, "Расписание Б", age_days=2)
    return Campus(client, acc.id, space, other, cabinet, calendar, elsewhere)
