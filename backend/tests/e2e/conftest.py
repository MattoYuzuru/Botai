"""Fixtures of the full-stack test: the real components of one process against `fakeuni`.

Reused as they are: the job environment of W1 (`env`: committed database, API app, runner), the
HTTPS fixture university with its EgressProxy and the Playwright gateway of W2. `stack` joins
them: the services container gets the real browser and login flow, and everything the test writes
to the tables shared by all students (learned specs, presets) is removed afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import delete, func, select

from botai.browser import egress as egress_module
from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.browser.proxy import EgressProxy
from botai.db import models as m
from tests.auth.support import cheap_hasher  # noqa: F401  (autouse: cheap argon2)
from tests.browser.conftest import (  # noqa: F401  (fixtures)
    egress,
    egress_access,
    fakeuni_port,
    gateway,
    tls,
    trust_fakeuni,
)
from tests.browser.test_login import FAST
from tests.e2e.support import WireTap
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, env  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _fresh_fakeuni(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test meets the university as it starts: nobody logged in, the code is asked."""
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", True)
    yield
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()


@pytest.fixture
def wiretap() -> Iterator[WireTap]:
    """Every log record of the process, formatted the way a log file would hold it."""
    tap = WireTap()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(tap)
    root.setLevel(logging.DEBUG)
    try:
        yield tap
    finally:
        root.removeHandler(tap)
        root.setLevel(previous)


@pytest.fixture
async def stack(
    env: Env,  # noqa: F811
    gateway: PlaywrightGateway,  # noqa: F811
    egress: EgressProxy,  # noqa: F811
    egress_access: ProxyEgress,  # noqa: F811
    trust_fakeuni: object,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Env]:
    """The environment of W1 with the real browser, login flow and EgressProxy plugged in."""
    # Sync clients mint their proxy tokens through the process-wide egress: the fixture proxy.
    monkeypatch.setattr(egress_module, "_cached", egress_access)
    env.settings.egress_proxy_url = egress.url  # "go through the EgressProxy", as in production
    env.services.browser = gateway
    env.services.sessions = LoginFlow(
        gateway=gateway,
        vault=env.services.vault,
        store=PgLoginStore(env.sm),
        egress=egress_access,
        clock=env.clock,
        timing=FAST,
    )

    async with env.sm() as s:
        specs_before = set(await s.scalars(select(m.AdapterSpecRow.id)))
        presets_before = set(await s.scalars(select(m.LoginPresetRow.id)))
        audit_before = (await s.scalar(select(func.coalesce(func.max(m.AuditLog.id), 0)))) or 0
    try:
        yield env
    finally:
        async with env.sm() as s:
            # what the students learned is shared by the pool, not owned by a user: put it back
            await s.execute(delete(m.User).where(m.User.id.in_(env.users)))
            await s.execute(
                delete(m.AdapterSpecRow).where(m.AdapterSpecRow.id.not_in(specs_before))
            )
            await s.execute(
                delete(m.LoginPresetRow).where(m.LoginPresetRow.id.not_in(presets_before))
            )
            await s.execute(delete(m.AuditLog).where(m.AuditLog.id > audit_before))
            await s.commit()
