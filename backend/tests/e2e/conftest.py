"""Fixtures of the full-stack test: the real components of one process against `fakeuni`.

Reused as they are: the job environment of W1 (`env`: committed database, API app, runner) and the
HTTPS fixture university of W2. The EgressProxy is run the way compose runs it: its own process
(`python -m botai.browser.proxy`), tokens signed with a key derived from `BOTAI_PROXY_SECRET`, and
the worker side obtains its egress from the production factory `get_egress(settings)`. `stack`
joins them: the services container gets the real browser and login flow, and everything the test
writes to the tables shared by all students (learned specs, presets) is removed afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import delete, func, select

from botai.browser import egress as egress_module
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.db import models as m
from tests.auth.support import cheap_hasher  # noqa: F401  (autouse: cheap argon2)
from tests.browser.conftest import (  # noqa: F401  (fixtures)
    FAKEUNI_HOSTS,
    fakeuni_port,
    tls,
    trust_fakeuni,
)
from tests.browser.test_login import FAST
from tests.e2e.support import PROXY_MASTER, ProxyProcess, Stack, WireTap
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, env  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _fresh_fakeuni(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test meets the university as it starts: nobody logged in, the code is asked."""
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
    fakeuni_login.ATTEMPTS.clear()
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", True)
    yield
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
    fakeuni_login.ATTEMPTS.clear()


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


@pytest.fixture(scope="session")
async def proxy_process(
    fakeuni_port: int,  # noqa: F811
) -> AsyncIterator[ProxyProcess]:
    """The EgressProxy as compose runs it, with `*.fakeuni.test` mapped to the fixture server."""
    args = [sys.executable, "-m", "botai.browser.proxy", "--listen", "127.0.0.1:0"]
    for host in FAKEUNI_HOSTS:
        args += ["--test-host", f"{host}=127.0.0.1:{fakeuni_port}"]
    process = await asyncio.create_subprocess_exec(
        *args,
        env={**os.environ, "BOTAI_PROXY_SECRET": PROXY_MASTER, "BOTAI_ENV": "test"},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    lines: list[str] = []
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def read_log() -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            found = re.search(r"listening on ([\d.]+):(\d+)", line)
            if found and not ready.done():
                ready.set_result(f"http://{found[1]}:{found[2]}")
        if not ready.done():
            ready.set_exception(RuntimeError("the proxy exited:\n" + "\n".join(lines)))

    reader = asyncio.create_task(read_log())
    try:
        url = await asyncio.wait_for(ready, 30)
        yield ProxyProcess(url, lines)
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
        await reader


@pytest.fixture
async def stack(
    env: Env,  # noqa: F811
    proxy_process: ProxyProcess,
    trust_fakeuni: object,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Stack]:
    """The environment of W1 with the real browser, login flow and EgressProxy plugged in."""
    # The worker's side of the proxy, built by the production factory from settings and secret:
    # tokens for a proxy in another process. Sync clients find the same object through
    # `proxy_url_for`, as in the worker.
    monkeypatch.setenv("BOTAI_PROXY_SECRET", PROXY_MASTER)
    monkeypatch.setattr(egress_module, "_cached", None)
    env.settings.egress_proxy_url = proxy_process.url
    egress = egress_module.get_egress(env.settings)
    gateway = PlaywrightGateway(egress, sandbox=False, ignore_https_errors=True)  # fixture TLS only
    env.services.browser = gateway
    env.services.sessions = LoginFlow(
        gateway=gateway,
        vault=env.services.vault,
        store=PgLoginStore(env.sm),
        egress=egress,
        clock=env.clock,
        timing=FAST,
    )

    async with env.sm() as s:
        specs_before = set(await s.scalars(select(m.AdapterSpecRow.id)))
        presets_before = set(await s.scalars(select(m.LoginPresetRow.id)))
        audit_before = (await s.scalar(select(func.coalesce(func.max(m.AuditLog.id), 0)))) or 0
    try:
        yield Stack(env, gateway, proxy_process, len(proxy_process.lines))
    finally:
        await gateway.aclose()
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
