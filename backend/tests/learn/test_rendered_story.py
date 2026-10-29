"""A DOM-only synthetic SPA: real Chromium canary, selected week and later model-free sync."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.proxy import EgressProxy
from botai.core.errors import BotaiError, ErrorCode
from botai.db import models as m
from botai.learn.canary import run_canary
from botai.learn.handlers import make_handlers
from botai.sources import rendered
from tests.browser.conftest import PROXY_SECRET, QuietServer, Tls, tls  # noqa: F401
from tests.fakeuni import data
from tests.fakeuni.app import create_fakeuni
from tests.fakeuni.login import COOKIE, SESSIONS
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import FAST, NOW, make_ctx, session_state

pytestmark = pytest.mark.browser
ENTRY = f"{data.ORIGIN}/rendered-schedule?begin_date=2026-10-26"


@dataclass
class DomSite:
    gateway: PlaywrightGateway
    weeks: list[str]
    writes: list[str]
    app: FastAPI
    broken: bool = False


@pytest.fixture
async def dom_site(tls: Tls) -> AsyncIterator[DomSite]:  # noqa: F811
    app = create_fakeuni()
    site: DomSite

    @app.get("/rendered-schedule", response_model=None)
    async def schedule(request: Request) -> HTMLResponse | RedirectResponse:
        if request.cookies.get(COOKIE) not in SESSIONS:
            return RedirectResponse("/auth/login", status_code=302)
        week = request.query_params.get("begin_date", "")
        date.fromisoformat(week)
        site.weeks.append(week)
        title_class = "moved-title" if site.broken else "event-title"
        event = (
            f'<article class="event"><h4 class="{title_class}">Курс о графах</h4>'
            f'<time class="begin" datetime="{week}T09:00:00+03:00">09:00</time>'
            f'<time class="end" datetime="{week}T10:20:00+03:00">10:20</time></article>'
        )
        # The HTTP shell has no article. Only JavaScript renders the deeply wrapped record.
        body = (
            "<html><body><nav>"
            + "".join(f'<a class="menu-{i}" href="/section/{i}">Раздел {i}</a>' for i in range(100))
            + "</nav><main id='schedule'></main><script>"
            + "setTimeout(() => { document.querySelector('#schedule').innerHTML = "
            + json.dumps("<div>" * 20 + event + "</div>" * 20)
            + "; }, 100);"
            + "fetch('/rendered-write', {method:'POST'}).catch(() => {});"
            + "</script></body></html>"
        )
        return HTMLResponse(body)

    @app.post("/rendered-write")
    async def write() -> dict[str, bool]:
        site.writes.append("unexpected")
        return {"ok": True}

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        ssl_certfile=str(tls.cert),
        ssl_keyfile=str(tls.key),
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = QuietServer(config)
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
    site = DomSite(gateway, [], [], app)
    try:
        yield site
    finally:
        await gateway.aclose()
        await proxy.close()
        server.should_exit = True
        await task


def endpoint() -> dict[str, Any]:
    return {
        "id": "schedule",
        "kind": "scheduleEvents",
        "request": {
            "method": "GET",
            "path": "/rendered-schedule",
            "query": {"begin_date": "{weekStart}"},
            "render": True,
        },
        "response": {
            "format": "html",
            "itemsSelector": "article.event",
            "identity": ["title", "startsAt"],
            "fields": {
                "title": {"selector": ".event-title", "required": True},
                "startsAt": {
                    "selector": "time.begin",
                    "attr": "datetime",
                    "transform": "iso",
                    "required": True,
                },
                "endsAt": {
                    "selector": "time.end",
                    "attr": "datetime",
                    "transform": "iso",
                    "required": True,
                },
            },
        },
    }


async def test_dom_only_spa_canary_and_sync_follow_local_week_without_model(
    make_env: EnvFactory, dom_site: DomSite
) -> None:
    spec = recipes.recipe_spec([endpoint()], require=("scheduleEvents",))
    ctx = make_ctx(dom_site.app, browser=dom_site.gateway)
    ctx.now = NOW + timedelta(days=1)
    http_endpoint = endpoint()
    http_endpoint["request"]["render"] = False
    http_spec = recipes.recipe_spec([http_endpoint], require=("scheduleEvents",))
    assert not (await run_canary(http_spec, ctx, require={"scheduleEvents"})).ok
    rendered_canary = await run_canary(spec, ctx, require={"scheduleEvents"})
    assert rendered_canary.ok
    assert rendered_canary.valid["scheduleEvents"][0].fields["startsAt"].startswith("2026-10-26")
    assert dom_site.writes == []

    env = await make_env([ls.answer([endpoint()])])
    await env.login()
    env.services.browser = dom_site.gateway
    env.services.clock.advance(timedelta(days=1))  # type: ignore[attr-defined]
    result = await env.run(payload={"url": ENTRY})
    assert result["status"] == "ok"
    assert await env.count(m.ScheduleEvent) == 1
    assert len(env.llm.requests) == 1
    prompt = env.llm.requests[0].messages[1].content
    assert "[page] /rendered-schedule?begin_date=2026-10-26" in prompt
    assert "Курс о графах" in prompt  # retained despite depth >19 and navigation noise
    stored = (await env.db.scalars(select(m.AdapterSpecRow.spec))).one()
    assert stored["recipe"]["endpoints"][0]["request"]["query"] == {"begin_date": "{weekStart}"}
    assert "2026-10-26" not in json.dumps(stored)
    env.services.clock.advance(timedelta(days=7))  # type: ignore[attr-defined]
    assert (await env.sync())["status"] == "ok"
    assert dom_site.weeks[-1] == "2026-11-02"
    assert len(env.llm.requests) == 1
    events = (await env.db.scalars(select(m.ScheduleEvent))).all()
    assert {event.starts_at.date().isoformat() for event in events} == {"2026-10-26", "2026-11-02"}
    assert dom_site.writes == []
    # Disabled sources must not acquire a background browser or load the page again.
    conn = await env.conn()
    conn.status = "disabled"
    await env.db.commit()
    visits = len(dom_site.weeks)
    assert (await env.sync())["status"] == "skipped"
    assert len(dom_site.weeks) == visits


async def test_refused_http_candidate_does_not_hide_a_working_rendered_candidate(
    make_env: EnvFactory, dom_site: DomSite
) -> None:
    refused = recipes.timetable_endpoint(path="/api/browser-only-timetable")
    env = await make_env([ls.answer([refused, endpoint()])])
    await env.login()
    env.services.browser = dom_site.gateway
    http_calls: list[str] = []

    def denied(request: httpx.Request) -> httpx.Response:
        http_calls.append(request.url.path)
        return httpx.Response(401)

    env.transport = httpx.MockTransport(denied)
    env.handlers = make_handlers(transport=env.transport, retry=FAST)
    result = await env.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})

    assert result["status"] == "ok"
    assert http_calls == ["/api/browser-only-timetable"]
    assert await env.count(m.ScheduleEvent) == 1
    stored = await env.spec(1)
    assert stored["recipe"]["endpoints"][0]["request"]["render"] is True
    assert len(env.llm.requests) == 1
    assert env.sessions.calls == 1
    assert dom_site.writes == []


async def test_rendered_drift_preserves_the_last_snapshot(
    make_env: EnvFactory, dom_site: DomSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make_env([ls.answer([endpoint()])])
    await env.login()
    env.services.browser = dom_site.gateway
    assert (await env.run(payload={"url": ENTRY}))["status"] == "ok"
    dom_site.broken = True
    monkeypatch.setattr(rendered, "SELECTOR_WAIT_SECONDS", 0.01)
    assert (await env.sync())["status"] == "repairing"
    assert await env.count(m.ScheduleEvent) == 1
    assert len(env.llm.requests) == 1


async def test_rendered_timeout_preserves_the_last_snapshot(
    make_env: EnvFactory, dom_site: DomSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make_env([ls.answer([endpoint()])])
    await env.login()
    env.services.browser = dom_site.gateway
    assert (await env.run(payload={"url": ENTRY}))["status"] == "ok"
    # A too-small total deadline fails before any snapshot replacement.
    monkeypatch.setattr(rendered, "MAX_RENDERED_SECONDS", 0.01)
    with pytest.raises(BotaiError, match="source_unavailable") as error:
        await env.sync()
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert await env.count(m.ScheduleEvent) == 1
    assert len(env.llm.requests) == 1


async def test_rendered_auth_redirect_needs_login_and_never_asks_model(
    dom_site: DomSite,
) -> None:
    ctx = make_ctx(dom_site.app, browser=dom_site.gateway, session=session_state())
    SESSIONS.clear()
    from botai.sources.adapter import SessionExpired

    with pytest.raises(SessionExpired, match=r"login|session"):
        await run_canary(
            recipes.recipe_spec([endpoint()], require=("scheduleEvents",)),
            ctx,
            require={"scheduleEvents"},
        )
