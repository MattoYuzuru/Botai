"""The agent reads a page that scripts draw: when the browser is used, and what it may and may not do (L1).

A page with next to no text and scripts is opened once more in the read-only background browser with
the student's session. The browser here is the fake gateway; the real Chromium against a single-page
cabinet is `test_tools_web_spa_browser.py`.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import tools_web
from botai.core import diagnostics
from botai.core.browser import (
    FakeBrowserGateway,
    FakeBrowserHandle,
    FakePage,
    ObservedRequest,
    PageSnapshot,
    SessionBearer,
    SessionOriginStorage,
)
from botai.db.models import SourceSitemap
from botai.learn.sitemap import get_site_map
from botai.sources import rendered
from tests.agent.support import NOW
from tests.agent.test_tools_web import TOKEN, Rig, source_rig
from tests.sources.helpers import session_state

Sm = async_sessionmaker[AsyncSession]
ORIGIN = "https://fakeuni.test"
FAQ = f"{ORIGIN}/faq"
API = f"{ORIGIN}/api/faq"
SHELL = (
    "<!doctype html><html><head><title>SmartPoint — Частые вопросы</title></head><body>"
    '<div id="root"></div><script src="/assets/app.js"></script></body></html>'
)
QUESTION = "Как перенести занятие?"
DRAWN = (
    "<!doctype html><html><head><title>SmartPoint — Частые вопросы</title></head><body>"
    f"<h1>Частые вопросы</h1><section><h2>{QUESTION}</h2><p>Через деканат.</p></section>"
    '<nav><a href="/requests">Заявки</a> <a href="https://evil.test/x">Чужая</a> '
    '<a href="/auth/logout">Выйти</a></nav></body></html>'
)
FAQ_JSON = json.dumps({"items": [{"question": QUESTION, "answer": "Через деканат."}]})


def call(url: str = API, **changes: Any) -> ObservedRequest:
    """A request the page made while drawing itself; by default a good GET of JSON."""
    fields: dict[str, Any] = {
        "method": "GET",
        "url": url,
        "status": 200,
        "resource_type": "fetch",
        "response_content_type": "application/json",
        "response_sample": FAQ_JSON,
    }
    return ObservedRequest(**{**fields, **changes})


def faq_page(*requests: ObservedRequest, html: str = DRAWN) -> FakePage:
    return FakePage(html=html, title="SmartPoint — Частые вопросы", requests=list(requests))


def shell_only() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _: httpx.Response(200, content=SHELL, headers={"content-type": "text/html"})
    )


class Recording(FakeBrowserGateway):
    """The fake browser that remembers how it was opened."""

    def __init__(self, pages: dict[str, FakePage]) -> None:
        super().__init__(pages, NOW, ORIGIN)
        self.opened: list[dict[str, Any]] = []

    async def open(self, **kwargs: Any) -> FakeBrowserHandle:
        self.opened.append(kwargs)
        return await super().open(**kwargs)


@pytest.fixture(autouse=True)
def quick_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rendered, "PAGE_QUIET_SECONDS", 0.05)
    monkeypatch.setattr(rendered, "PAGE_IDLE_SECONDS", 0.2)
    monkeypatch.setattr(rendered, "PAGE_WAIT_SECONDS", 0.6)


async def rig(
    sm: Sm,
    user_id: str,
    app: FastAPI,
    pages: dict[str, FakePage],
    **options: Any,
) -> tuple[Rig, Recording]:
    browser = Recording(pages)
    options.setdefault("inner", shell_only())
    options.setdefault("routes", ("/faq", "/requests", "/a", "/b", "/c", "/d"))
    r = await source_rig(sm, user_id, app, browser=browser, **options)
    return r, browser


async def browse(r: Rig, url: str = FAQ) -> str:
    return await r.call("browse_source", connection_id=r.conn_id, url=url)


async def test_a_shell_is_read_in_the_browser_with_the_students_session(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    session = session_state(TOKEN).model_copy(
        update={
            "bearer_scopes": [
                SessionBearer(origin=ORIGIN, token=SecretStr("bearer-DO-NOT-LEAK"), captured_at=NOW)
            ],
            "browser_storage": [
                SessionOriginStorage(
                    origin=ORIGIN, local_storage={"oidc.user": SecretStr("stored-DO-NOT-LEAK")}
                )
            ],
        }
    )
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())}, stored=session)
    text = await browse(r)

    assert QUESTION in text
    assert "Через деканат." in text
    assert "прочитана в браузере" in text
    assert "Данные страницы" in text
    assert f"- {API}:" in text
    assert "Заявки — https://fakeuni.test/requests" in text
    assert "evil.test" not in text  # a link to a foreign host is not shown
    assert "SmartPoint — Частые вопросы" in text
    # one plain GET for the shell, then one read-only background browser with the whole session
    assert [q.method for q in r.spy.requests] == ["GET"]
    (opened,) = browser.opened
    assert opened["read_only"] is True
    assert opened["background"] is True
    assert "fakeuni.test" in opened["allow_hosts"]
    assert opened["session"].cookies[0].value.get_secret_value() == TOKEN
    assert opened["session"].bearer_scopes[0].origin == ORIGIN
    assert opened["session"].browser_storage[0].local_storage
    assert [h.closed for h in browser.handles] == [True]
    assert r.env.counters["render"] == 1
    # a credential never reaches the model, whatever the page does with it
    for secret in (TOKEN, "bearer-DO-NOT-LEAK", "stored-DO-NOT-LEAK"):
        assert secret not in text
    assert r.env.outside_read
    source = r.env.sources.refs["S1"]
    assert (source.url, source.kind) == (FAQ, "page")


async def test_a_page_with_its_own_text_does_not_open_the_browser(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    plain = httpx.MockTransport(
        lambda _: httpx.Response(
            200, content=DRAWN, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    r, browser = await rig(sm, user_id, fakeuni_app, {}, inner=plain)
    text = await browse(r)
    assert QUESTION in text
    assert "прочитана в браузере" not in text
    assert browser.opened == []
    assert "render" not in r.env.counters


async def test_pages_read_in_the_browser_are_capped_per_answer(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    pages = {f"{ORIGIN}/{name}": faq_page(call()) for name in "abcd"}
    r, browser = await rig(sm, user_id, fakeuni_app, pages)
    for name in "abc":
        assert QUESTION in await browse(r, f"{ORIGIN}/{name}")
    limited = await browse(r, f"{ORIGIN}/d")
    assert limited.startswith("Результат: не выполнено")
    assert f"не больше {tools_web.MAX_RENDERS}" in limited
    assert len(browser.opened) == tools_web.MAX_RENDERS
    assert all(h.closed for h in browser.handles)


async def test_a_busy_browser_is_told_honestly_and_not_pushed_out(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    running = await browser.open(user_id=user_id, connection_id=r.conn_id, allow_hosts=["x.test"])
    text = await browse(r)
    assert text.startswith("Результат: не выполнено")
    assert "занят другой задачей" in text
    assert "подключение" in text
    assert not running.closed  # the running login or research keeps its browser
    await running.close()
    assert QUESTION in await browse(r)  # the same page opens once the browser is free


async def test_a_session_that_is_known_to_be_over_never_opens_the_browser(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    gone = session_state(TOKEN, expires_hint=NOW - timedelta(hours=1))
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())}, stored=gone)
    text = await browse(r)
    assert "needs_login" in text
    assert browser.opened == []


@pytest.mark.parametrize(
    "page",
    [
        # the application sent the browser to the login form
        faq_page(html='<form><input type="password" name="p"></form><p>Войти</p>'),
        # its own requests were refused and it drew nothing
        faq_page(call(status=401, response_sample=None, response_content_type=None), html=SHELL),
    ],
    ids=["login-form", "refused-api"],
)
async def test_an_application_that_lost_the_session_is_needs_login(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, page: FakePage
) -> None:
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: page})
    text = await browse(r)
    assert "needs_login" in text
    assert "request_login" in text
    assert [h.closed for h in browser.handles] == [True]


async def test_a_redirect_to_a_foreign_host_is_not_followed_into_the_answer(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    class Elsewhere(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)

            async def goto(url: str) -> PageSnapshot:
                return PageSnapshot(url="https://evil.test/collect", html=DRAWN)

            handle.goto = goto  # type: ignore[method-assign]
            return handle

    browser = Elsewhere({})
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert "needs_login" in text
    assert QUESTION not in text
    assert "evil.test" not in text


async def test_a_page_that_never_loads_ends_as_an_error_and_the_browser_is_closed(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)

            async def goto(url: str) -> PageSnapshot:
                await asyncio.sleep(10)
                return PageSnapshot(url=url)

            handle.goto = goto  # type: ignore[method-assign]
            return handle

    monkeypatch.setattr(tools_web, "RENDER_SECONDS", 1.05)
    browser = Slow({})
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert text.startswith("Результат: ошибка.")
    assert "не успела загрузиться" in text
    assert [h.closed for h in browser.handles] == [True]


async def test_a_post_the_guard_refused_does_not_hold_the_reading(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendered, "PAGE_WAIT_SECONDS", 8.0)
    stuck = call(f"{ORIGIN}/api/track", method="POST", status=None, response_sample=None)
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call(), stuck)})
    started = time.monotonic()
    assert QUESTION in await browse(r)
    assert time.monotonic() - started < 3  # not the 8 seconds a request that never returns costs


async def test_a_short_page_that_scripts_did_not_change_is_just_a_short_page(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    short = (
        "<!doctype html><title>Расписание</title><p>Занятий на этой неделе нет.</p>"
        "<script>window.analytics = 1;</script>"
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=short, headers={"content-type": "text/html"})
    )
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(html=short)}, inner=transport)
    text = await browse(r)
    assert "Занятий на этой неделе нет." in text
    assert "строится скриптами" not in text  # nothing was drawn: this is the whole page
    assert "не делайте вывода" not in text
    assert len(browser.opened) == 1  # the browser had to look to know


async def test_an_application_stuck_on_its_spinner_is_not_a_short_page(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    spinner = SHELL.replace('<div id="root"></div>', '<div id="root">Загрузка…</div>')
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(html=spinner)})
    # the plain GET got text-less markup; after the scripts there is still only the spinner
    text = await browse(r)
    assert "Загрузка…" in text
    assert "не делайте вывода, что данных нет" in text


async def test_an_application_that_draws_nothing_says_so_instead_of_an_empty_source(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(html=SHELL)})
    text = await browse(r)
    assert "ничего не показало" in text
    assert "не делайте вывода, что данных нет" in text
    assert "needs_login" not in text


async def test_only_what_the_page_fetched_with_a_get_from_its_own_site_is_shown(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    mine = f"{ORIGIN}/api/news"
    requests = [
        call(),
        call(mine, response_sample='{"items":[{"title":"Новость недели"}]}'),
        call(f"{ORIGIN}/api/track", method="POST", response_sample='{"leak":"post-body"}'),
        call("https://api.fakeuni.test/v1/other", response_sample='{"leak":"other-origin"}'),
        call(f"{ORIGIN}/api/denied", status=401, response_sample='{"leak":"refused"}'),
        call(f"{ORIGIN}/api/page", response_content_type="text/html", response_sample="<b>x</b>"),
        call(f"{ORIGIN}/api/signout/state", response_sample='{"leak":"action-address"}'),
        call(f"{ORIGIN}/api/me?sesskey=Xy12Zq", response_sample='{"leak":"secret-address"}'),
        call(f"{ORIGIN}/api/big", response_sample='{"items":[{"leak":"cut-off'),
        call(f"{ORIGIN}/api/none", response_sample=None),
    ]
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(*requests)})
    text = await browse(r)
    assert QUESTION in text
    assert "Новость недели" in text
    for leak in ("post-body", "other-origin", "refused", "action-address", "secret-address"):
        assert leak not in text
    assert "cut-off" not in text
    assert "Xy12Zq" not in text
    # the addresses the model was shown may be read next; the ones left out may not
    assert {API, mine} <= set(r.env.seen.source)
    assert f"{ORIGIN}/api/track" not in set(r.env.seen.source)


async def test_credentials_in_what_the_page_fetched_are_hidden(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    sample = json.dumps(
        {
            "items": [{"question": QUESTION}],
            "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "note": f"сессия {TOKEN}",
        }
    )
    echoing = DRAWN.replace("Через деканат.", f"Ваша сессия {TOKEN}")
    r, _ = await rig(
        sm, user_id, fakeuni_app, {FAQ: faq_page(call(response_sample=sample), html=echoing)}
    )
    text = await browse(r)
    assert QUESTION in text
    assert "eyJhbGci" not in text
    assert TOKEN not in text
    assert text.count("[скрыто]") >= 2


async def test_a_page_that_moved_to_an_address_with_a_key_does_not_show_it(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    class Moved(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)
            moved = f"{ORIGIN}/faq/done#access_token=aB3cD4eF5gH6iJ7kL8mN9oP0qR"

            async def goto(url: str) -> PageSnapshot:
                return await handle.snapshot()

            async def snapshot() -> PageSnapshot:
                return PageSnapshot(url=moved, html=DRAWN)

            handle.goto, handle.snapshot = goto, snapshot  # type: ignore[method-assign]
            return handle

    r = await source_rig(
        sm,
        user_id,
        fakeuni_app,
        browser=Moved({}),
        inner=shell_only(),
        routes=("/faq",),
    )
    text = await browse(r)
    assert QUESTION in text
    assert "Открылась другая страница." in text
    assert "aB3cD4eF5g" not in text


async def test_the_page_may_not_lead_to_an_action(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    class Leaving(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)

            async def goto(url: str) -> PageSnapshot:
                return await handle.snapshot()

            async def snapshot() -> PageSnapshot:
                return PageSnapshot(url=f"{ORIGIN}/auth/logout", html=DRAWN)

            handle.goto, handle.snapshot = goto, snapshot  # type: ignore[method-assign]
            return handle

    r = await source_rig(
        sm, user_id, fakeuni_app, browser=Leaving({}), inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert text.startswith("Результат: не выполнено")
    assert "похожий на действие" in text
    assert QUESTION not in text


async def test_a_source_without_a_map_learns_its_sections_from_the_drawn_page(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())}, routes=("/faq",))
    async with sm() as session:  # `source_rig` saved a map that points at /faq; start with none
        await session.execute(delete(SourceSitemap))
        await session.commit()
    r.env.question_links = {FAQ}
    await browse(r)
    async with sm() as session:
        learned = await get_site_map(session, ORIGIN)
    assert learned is not None
    assert {"label": "Заявки", "path": "/requests"} in learned["navigation"]
    assert "/faq" in learned["routes"]
    assert {"kind": "faq", "path": "/api/faq", "queryKeys": []} in learned["endpoints"]
    assert "/auth/logout" not in json.dumps(learned)  # an action is not a section
    assert "evil" not in json.dumps(learned)
    assert QUESTION not in json.dumps(learned)  # sections and routes, not the text of a page
    assert "/api/faq" in await r.call("site_map", connection_id=r.conn_id)


async def test_a_map_that_exists_is_left_as_it_is(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())}, routes=("/faq",))
    async with sm() as session:
        before = await get_site_map(session, ORIGIN)
    await browse(r)
    async with sm() as session:
        assert await get_site_map(session, ORIGIN) == before


async def test_render_events_carry_only_static_facts(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def record(name: str, **facts: object) -> None:
        events.append((name, facts))

    monkeypatch.setattr(tools_web, "diagnostic_event", record)
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    await browse(r)
    running = await browser.open(user_id=user_id, connection_id=r.conn_id, allow_hosts=["x.test"])
    await browse(r)  # the browser is busy: a second kind of event
    await running.close()
    assert [name for name, _ in events] == ["chat.browse_render"] * 2
    assert [(f["outcome"], f.get("reason")) for _, f in events] == [
        ("ok", None),
        ("failed", "unavailable"),
    ]
    for _, facts in events:
        # the closed vocabulary of diagnostics keeps every fact (it drops what it does not know)
        kept = diagnostics._clean(facts)  # pyright: ignore[reportPrivateUsage]
        assert kept == {k: v for k, v in facts.items() if v is not None}
        assert QUESTION not in json.dumps(facts, default=str)
