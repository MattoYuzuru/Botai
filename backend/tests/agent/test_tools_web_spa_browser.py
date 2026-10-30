"""The agent reads a single-page cabinet: the shell has no text, a real read-only Chromium has (L1).

fakeuni serves `/spa-cabinet/faq` as an empty shell; its script draws the FAQ from a Bearer API with
the token the session left in localStorage. Everything the page does besides reading (a POST, a
foreign host, a WebSocket) must stay unsent, and a session the university forgot ends as needs_login.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.browser.gateway import PlaywrightGateway
from botai.core.browser import SessionBearer, SessionOriginStorage, SessionState
from botai.db.models import SourceSitemap
from botai.learn.sitemap import get_site_map
from botai.sources import rendered
from tests.agent.support import NOW
from tests.agent.test_tools_web import TOKEN, source_rig
from tests.browser.conftest import egress, egress_access, fakeuni_port, gateway, tls  # noqa: F401
from tests.fakeuni import spa
from tests.sources.helpers import session_state

pytestmark = pytest.mark.browser
Sm = async_sessionmaker[AsyncSession]
ORIGIN = "https://fakeuni.test"
FAQ = f"{ORIGIN}/spa-cabinet/faq"
QUIET = f"{ORIGIN}/spa-cabinet/quiet"
ROUTES = ("/spa-cabinet/faq", "/spa-cabinet/quiet")


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    spa.reset()


@pytest.fixture
def browser(gateway: PlaywrightGateway) -> PlaywrightGateway:  # noqa: F811
    return gateway


def stored() -> SessionState:
    """Cookie, the bearer of the origin and the localStorage the application reads it from."""
    return session_state(TOKEN).model_copy(
        update={
            "bearer_scopes": [
                SessionBearer(origin=ORIGIN, token=SecretStr(spa.FAQ_TOKEN), captured_at=NOW)
            ],
            "browser_storage": [
                SessionOriginStorage(
                    origin=ORIGIN, local_storage={spa.STORAGE_KEY: SecretStr(spa.FAQ_TOKEN)}
                )
            ],
        }
    )


async def test_the_shell_has_no_text_and_the_browser_reads_the_faq(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, browser: PlaywrightGateway
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fakeuni_app), base_url=ORIGIN
    ) as plain:
        shell = await plain.get("/spa-cabinet/faq")
    assert "Как перенести занятие?" not in shell.text  # what a plain GET gets: no questions
    spa.reset()

    r = await source_rig(sm, user_id, fakeuni_app, stored=stored(), routes=ROUTES)
    r.env.services.browser = browser
    text = await r.call("browse_source", connection_id=r.conn_id, url=FAQ)

    assert "прочитана в браузере" in text
    assert "Частые вопросы" in text
    assert "Как перенести занятие?" in text
    assert "Подайте заявление в деканат за три дня." in text
    assert "Данные страницы" in text
    assert "/spa-cabinet/api/faq" in text  # the JSON the page fetched for itself
    assert r.env.counters["render"] == 1
    # the page read its API with the cookie, the bearer and the restored localStorage...
    assert spa.STATE.faq_reads == 1
    assert spa.STATE.echoed == [TOKEN]
    # ...and none of those values reached the model, though the page itself printed the cookie
    assert "Заметка: сессия [скрыто]" in text
    assert TOKEN not in text
    assert spa.FAQ_TOKEN not in text
    # the read-only browser sent nothing: no POST, no WebSocket, nothing to the foreign host
    assert spa.STATE.writes == 0
    assert spa.STATE.sockets == 0
    assert "[foreign blocked]" in text
    assert "[foreign sent]" not in text
    assert r.spy.requests[0].method == "GET"
    assert [q.method for q in r.spy.requests] == ["GET"]  # the shell, once, over HTTP
    assert r.env.sources.refs["S1"].url == FAQ
    # the browser is free again for the next job of the student
    assert browser.active_for(user_id) is None


async def test_the_map_of_a_source_without_one_learns_the_faq_from_the_render(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, browser: PlaywrightGateway
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, stored=stored(), routes=ROUTES)
    # an empty map, as before the first observation of the source
    async with sm() as session:
        await session.execute(delete(SourceSitemap))
        await session.commit()
    r.env.services.browser = browser
    r.env.question_links = {FAQ}  # the student wrote this address: no map yet
    await r.call("browse_source", connection_id=r.conn_id, url=FAQ)
    async with sm() as session:
        learned = await get_site_map(session, ORIGIN)
    assert learned is not None
    assert any(
        e["kind"] == "faq" and e["path"] == "/spa-cabinet/api/faq" for e in learned["endpoints"]
    )
    assert "/spa-cabinet/faq" in learned["routes"]
    assert "Частые вопросы" not in str(learned)  # routes and endpoints, not page text
    shown = await r.call("site_map", connection_id=r.conn_id)
    assert "/spa-cabinet/api/faq" in shown


async def test_a_session_the_university_forgot_is_needs_login(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, browser: PlaywrightGateway
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, stored=stored(), registered=False, routes=ROUTES)
    r.env.services.browser = browser
    text = await r.call("browse_source", connection_id=r.conn_id, url=FAQ)
    assert "needs_login" in text
    assert "request_login" in text
    assert "Как перенести занятие?" not in text
    assert spa.STATE.faq_reads == 0
    assert spa.STATE.refused >= 1  # the application asked and was refused: that is the sign
    assert browser.active_for(user_id) is None


async def test_a_busy_browser_is_an_honest_answer_and_is_not_pushed_out(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, browser: PlaywrightGateway
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, stored=stored(), routes=ROUTES)
    r.env.services.browser = browser
    # the student's browser is taken by a login or a research of a source
    running = await browser.open(
        user_id=user_id, connection_id=r.conn_id, allow_hosts=("fakeuni.test",)
    )
    try:
        text = await r.call("browse_source", connection_id=r.conn_id, url=FAQ)
        assert text.startswith("Результат: не выполнено")
        assert "занят другой задачей" in text
        assert not running.closed  # the running action was not replaced
        assert spa.STATE.faq_reads == 0
    finally:
        await running.close()
    again = await r.call("browse_source", connection_id=r.conn_id, url=FAQ)
    assert "Как перенести занятие?" in again


async def test_an_application_that_draws_nothing_is_not_taken_for_an_empty_source(
    sm: Sm,
    user_id: str,
    fakeuni_app: FastAPI,
    browser: PlaywrightGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rendered, "PAGE_WAIT_SECONDS", 1.0)
    r = await source_rig(sm, user_id, fakeuni_app, stored=stored(), routes=ROUTES)
    r.env.services.browser = browser
    text = await r.call("browse_source", connection_id=r.conn_id, url=QUIET)
    assert "ничего не показало" in text
    assert "не делайте вывода, что данных нет" in text
    assert browser.active_for(user_id) is None


async def test_the_entry_page_of_a_plain_source_is_still_one_get(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, browser: PlaywrightGateway
) -> None:
    """A page with its own text is not opened in the browser at all."""
    r = await source_rig(sm, user_id, fakeuni_app, routes=("/news",))
    r.env.services.browser = browser
    text = await r.call("browse_source", connection_id=r.conn_id, url=f"{ORIGIN}/news")
    assert "Перенос занятий 4 ноября" in text
    assert "прочитана в браузере" not in text
    assert "render" not in r.env.counters
