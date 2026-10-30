"""The synthetic SPA cabinet (`tests/fakeuni/lms.py`) as a real browser sees it, and as a scripted one does.

`serving_lms` runs fakeuni over HTTPS on loopback behind an EgressProxy with a real Chromium: the
observation is what the page's own scripts request, with the session cookie. `scripted_lms_site` is
the same cabinet for a `FakeBrowserGateway`: its pages and XHR are read from the app itself, so a
story without Chromium sees the very bodies the stand answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.proxy import EgressProxy
from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from tests.browser.conftest import PROXY_SECRET, QuietServer, Tls
from tests.fakeuni import data, lms
from tests.fakeuni.app import create_fakeuni
from tests.learn import support as ls
from tests.sources.helpers import NOW, new_session_token

ORIGIN = data.ORIGIN
ENTRY = f"{ORIGIN}{lms.TIMETABLE_PAGE}"
TASKS_ENTRY = f"{ORIGIN}{lms.TASKS_PAGE}"
STATE_QUERY = "&".join(f"state={state}" for state in lms.STATES)


@dataclass
class LmsSite:
    app: FastAPI
    gateway: PlaywrightGateway


@asynccontextmanager
async def serving_lms(tls: Tls) -> AsyncGenerator[LmsSite]:
    """fakeuni (with the cabinet) behind the egress proxy and a real background Chromium."""
    app = create_fakeuni()
    server = QuietServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_certfile=str(tls.cert),
            ssl_keyfile=str(tls.key),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    proxy = EgressProxy(PROXY_SECRET, test_hosts={"fakeuni.test": ("127.0.0.1", port)})
    await proxy.start()
    gateway = PlaywrightGateway(
        ProxyEgress(PROXY_SECRET, proxy=proxy), sandbox=False, ignore_https_errors=True
    )
    try:
        yield LmsSite(app, gateway)
    finally:
        await gateway.aclose()
        await proxy.close()
        server.should_exit = True
        await task


def _request(path: str, body: str, page: str, *, resource: str = "fetch") -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=ORIGIN + path,
        status=200,
        resource_type=resource,
        response_content_type="application/json",
        response_sample=body,
        auth_proven=True,
        has_cookie=True,
        initiator_url=page,
    )


async def scripted_lms_site(app: FastAPI) -> FakeBrowserGateway:
    """What a logged-in Chromium sees on the cabinet, built from the stand's own answers."""
    token = new_session_token("tok-observer")
    me = await ls.fetch(app, lms.ME_PATH, token)
    timetables = await ls.fetch(app, lms.TIMETABLES_PATH, token)
    tasks = await ls.fetch(app, f"{lms.TASKS_PATH}?{STATE_QUERY}", token)
    timetable_html = await ls.fetch(app, lms.TIMETABLE_PAGE, token)
    tasks_html = await ls.fetch(app, lms.TASKS_PAGE, token)
    document = ObservedRequest(
        method="GET",
        url=ENTRY,
        status=200,
        resource_type="document",
        response_content_type="text/html",
        auth_proven=True,
        has_cookie=True,
    )
    pages = {
        ENTRY: FakePage(
            timetable_html,
            "Мои пары",
            requests=[
                document,
                _request(lms.ME_PATH, me, ENTRY),
                _request(lms.TIMETABLES_PATH, timetables, ENTRY),
            ],
        ),
        TASKS_ENTRY: FakePage(
            tasks_html,
            "Задания",
            requests=[
                _request(lms.ME_PATH, me, TASKS_ENTRY),
                _request(f"{lms.TASKS_PATH}?{STATE_QUERY}", tasks, TASKS_ENTRY),
            ],
        ),
    }
    pages[f"{ORIGIN}/"] = pages[ENTRY]
    return FakeBrowserGateway(pages, NOW, ORIGIN)
