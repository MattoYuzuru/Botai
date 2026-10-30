"""The edges of reading a page that scripts draw: when it is not a lost session, not a short page,
not unreadable, what the browser held, hash routes, other hosts, time (L1, fake browser)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import tools_web, webtext
from botai.core.browser import (
    BrowserBusy,
    FakeBrowserHandle,
    PageSnapshot,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import SecretKind
from botai.sources.session import dump_session
from tests.agent.test_tools_web import TOKEN, source_rig
from tests.agent.test_tools_web_spa import (  # noqa: F401
    API,
    DRAWN,
    FAQ,
    ORIGIN,
    QUESTION,
    SHELL,
    Recording,
    browse,
    call,
    faq_page,
    quick_browser,
    rig,
    shell_only,
)
from tests.sources.helpers import session_state

Sm = async_sessionmaker[AsyncSession]
SHORT = (
    "<!doctype html><title>Расписание</title><p>Занятий на этой неделе нет.</p>"
    "<script>window.analytics = 1;</script>"
)
SPINNER = SHELL.replace('<div id="root"></div>', '<div id="root">Загрузка…</div>')


def serving(markup: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _: httpx.Response(200, content=markup, headers={"content-type": "text/html"})
    )


class Landing(Recording):
    """A browser whose page is at `url` with `html` whatever was asked for (a move by the app)."""

    def __init__(self, url: str, html: str, title: str = "") -> None:
        super().__init__({})
        self.landing = PageSnapshot(url=url, html=html, title=title)

    async def open(self, **kwargs: Any) -> FakeBrowserHandle:
        handle = await super().open(**kwargs)
        landing = self.landing

        async def goto(url: str) -> PageSnapshot:
            return landing

        async def snapshot() -> PageSnapshot:
            return landing

        handle.goto, handle.snapshot = goto, snapshot  # type: ignore[method-assign]
        return handle


# --- 1: a refused side request is not a lost session --------------------------------------------


async def test_a_refused_badge_request_does_not_turn_a_drawn_page_into_needs_login(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    # the main answer left no sample (it had a token key), a side panel was refused with 401
    main = call(response_sample=None)
    badge = call(f"{ORIGIN}/api/notifications", status=401, response_sample=None)
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(main, badge)})
    text = await browse(r)
    assert QUESTION in text
    assert "needs_login" not in text
    assert "не делайте вывода" not in text  # the page did draw: the text grew over the plain GET


# --- 2: a spinner over a failed API is not a short page -----------------------------------------


@pytest.mark.parametrize(
    "requests",
    [
        [call(status=500, response_sample=None)],
        [call(status=None, failed=True, response_sample=None)],
        [call(response_content_type="text/html", response_sample="<b>Ошибка</b>")],
        [],  # asked nothing, but the page says "Загрузка…" and stays so
    ],
    ids=["http-500", "lost", "not-json", "no-request"],
)
async def test_a_spinner_that_stays_is_not_taken_for_a_short_page(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, requests: list[Any]
) -> None:
    r, _ = await rig(
        sm,
        user_id,
        fakeuni_app,
        {FAQ: faq_page(*requests, html=SPINNER)},
        inner=serving(SPINNER),
    )
    text = await browse(r)
    assert "Загрузка…" in text
    assert "не делайте вывода, что данных нет" in text


# --- 3: no browser, no reading of the plain text? --------------------------------------------------


@pytest.mark.parametrize("cause", ["budget", "busy", "error", "no-browser", "no-time"])
async def test_a_short_page_with_a_script_is_still_read_when_the_browser_cannot_be_used(
    sm: Sm,
    user_id: str,
    fakeuni_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    class Broken(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Браузер недоступен")

    pages = {FAQ: faq_page(html=SHORT)}
    browser = Broken(pages) if cause == "error" else Recording(pages)
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=serving(SHORT), routes=("/faq",)
    )
    running = None
    if cause == "budget":
        r.env.counters["render"] = tools_web.MAX_RENDERS
    elif cause == "busy":
        running = await browser.open(
            user_id=user_id, connection_id=r.conn_id, allow_hosts=["x.test"]
        )
    elif cause == "no-browser":
        r.env.services.browser = None  # type: ignore[assignment]
    elif cause == "no-time":
        monkeypatch.setattr(tools_web, "TOOL_BUDGET_S", 1.0)
    text = await browse(r)
    assert "Занятий на этой неделе нет." in text  # what the server gave is the answer
    assert "Скрипты страницы не выполнялись" in text
    assert "приложение могло дорисовать остальное" in text
    if running is not None:
        await running.close()


async def test_an_empty_shell_is_refused_when_the_browser_cannot_be_used(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools_web, "TOOL_BUDGET_S", 1.0)
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    text = await browse(r)
    assert text.startswith("Результат: не выполнено")
    assert "не осталось времени" in text
    assert browser.opened == []


async def test_a_lost_session_is_needs_login_even_when_the_server_gave_some_text(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    login = '<form><input type="password" name="p"></form><p>Войдите</p>'
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(html=login)}, inner=serving(SHORT))
    text = await browse(r)
    assert "needs_login" in text
    assert "Занятий" not in text


async def test_the_render_never_gets_more_time_than_the_answer_has_left(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    original = tools_web.RenderedPageReader

    class Watching(original):  # type: ignore[valid-type,misc]
        def __init__(self, ctx: Any, *, seconds: float | None = None) -> None:
            seen.append(seconds or 0.0)
            super().__init__(ctx, seconds=seconds)

    monkeypatch.setattr(tools_web, "RenderedPageReader", Watching)
    monkeypatch.setattr(tools_web, "TOOL_BUDGET_S", 12.0)
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    assert QUESTION in await browse(r)
    (seconds,) = seen
    assert 5.0 <= seconds <= 12.0  # not RENDER_SECONDS: the plain GET came out of the same time


# --- 4: what the browser held while the page ran --------------------------------------------------


def with_inventory(browser: Recording, *values: str, broken: bool = False) -> Recording:
    original = browser.open

    async def open_(**kwargs: Any) -> FakeBrowserHandle:
        handle = await original(**kwargs)

        async def observation_secrets() -> tuple[SecretStr, ...]:
            if broken:
                raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "incomplete inventory")
            return tuple(SecretStr(v) for v in values)

        handle.observation_secrets = observation_secrets  # type: ignore[attr-defined]
        return handle

    browser.open = open_  # type: ignore[method-assign]
    return browser


async def test_a_cookie_given_to_the_page_while_it_ran_is_hidden_whatever_its_shape(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    printed = DRAWN.replace(
        "Через деканат.",
        "Куки: plain-cookie-without-digits; rn7Kq2Zx; rn7Kq2Zxy; ru; light; настройки",
    )
    pages = {FAQ: faq_page(call(), html=printed)}
    browser = with_inventory(
        Recording(pages), "plain-cookie-without-digits", "rn7Kq2Zx", "ru", "light", "настройки"
    )
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert "plain-cookie-without-digits" not in text  # from 16 characters on: wherever it is
    assert "Куки: [скрыто]; [скрыто]; rn7Kq2Zxy;" in text  # 8..15: a token, a whole word
    assert "ru; light; настройки" in text  # settings stay: they are ordinary words


async def test_without_a_complete_inventory_of_the_browser_nothing_is_shown(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    browser = with_inventory(Recording({FAQ: faq_page(call())}), broken=True)
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert text.startswith("Результат: ошибка.")
    assert QUESTION not in text
    assert [h.closed for h in browser.handles] == [True]


def test_the_hider_leaves_ordinary_words_and_short_settings_alone() -> None:
    hide = tools_web._Hider(  # pyright: ignore[reportPrivateUsage]
        ["ru", "true", "session", "Europe/Moscow", "a1b2c3d4", "x" * 16, "ключ-сессии-8"]
    )
    assert hide("ru true session") == "ru true session"  # under 8 characters
    assert hide("Europe/Moscow") == "Europe/Moscow"  # 8..15 without letters and digits together
    assert hide("id a1b2c3d4, a1b2c3d45") == "id [скрыто], a1b2c3d45"
    assert hide("xxxxxxxxxxxxxxxx") == "[скрыто]"
    assert hide("ключ-сессии-8") == "[скрыто]"


# --- 7, 8: values are cut out before the text is cut, in addresses too -------------------------------


async def test_a_value_of_the_session_is_hidden_before_the_outline_cuts_the_text(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    # `shrink_json` keeps 300 characters of a string: the value starts before that and ends after
    sample = json.dumps({"items": [{"note": "н" * 290 + TOKEN + " конец"}]}, ensure_ascii=False)
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call(response_sample=sample))})
    text = await browse(r)
    assert "tok-browse" not in text
    assert "DO-NOT-LEAK" not in text
    assert "[скрыто]" in text


async def test_an_address_that_holds_a_value_of_the_session_is_not_shown(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    leaking = f"{ORIGIN}/api/me/{TOKEN}"
    html = DRAWN.replace("</nav>", f'<a href="/files/{TOKEN}">Мой файл</a></nav>')
    requests = [call(), call(leaking, response_sample='{"leak":"in-the-address"}')]
    r, _ = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(*requests, html=html)})
    text = await browse(r)
    assert QUESTION in text
    assert "in-the-address" not in text
    assert "/files/" not in text  # the link is not offered (its text is part of the page)
    assert "tok-browse" not in text
    assert leaking not in set(r.env.seen.source)


# --- 6: the route of a hash application --------------------------------------------------------------


async def test_a_hash_route_goes_to_the_browser_and_a_plain_fragment_does_not(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    home = faq_page(html=DRAWN.replace(QUESTION, "Главная страница"))
    pages = {FAQ: home, f"{FAQ}#/schedule": faq_page(call())}
    r, browser = await rig(sm, user_id, fakeuni_app, pages)
    text = await browse(r, f"{FAQ}#/schedule")
    assert QUESTION in text
    assert f"{FAQ}#/schedule [S1]" in text  # the citation leads to the section
    assert len(r.spy.requests) == 1
    assert "#" not in str(r.spy.requests[0].url)  # the server never saw the route
    # a fragment that is not a route (an anchor) is dropped, as before
    other = await browse(r, f"{FAQ}#section")
    assert "Главная страница" in other
    assert f"{FAQ}#section" not in other
    assert browser.opened  # both were read in the browser


async def test_a_route_that_names_an_action_or_holds_a_key_is_refused(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    for route in ("/logout", "/orders/cancel", "/faq?token=aB3cD4eF5gH6iJ7kL8mN9oP0qR"):
        text = await browse(r, f"{FAQ}#{route}")
        assert text.startswith("Результат: не выполнено"), route
    assert browser.opened == []
    assert r.spy.requests == []


def test_the_links_of_a_page_keep_the_routes_of_a_hash_application() -> None:
    markup = (
        '<a href="#/faq">Вопросы</a> <a href="#/requests">Заявки</a> <a href="#top">Наверх</a>'
        '<a href="/page#/faq">Ещё</a>'
    ).encode()
    links = webtext.page_links(markup, "https://fakeuni.test/app")
    assert ("Вопросы", "https://fakeuni.test/app#/faq") in links
    assert ("Заявки", "https://fakeuni.test/app#/requests") in links
    assert ("Наверх", "https://fakeuni.test/app") in links  # an anchor is not a page
    assert ("Ещё", "https://fakeuni.test/page#/faq") in links


# --- 10: other hosts and the form that changes a password -----------------------------------------------


async def test_a_move_to_another_host_of_the_source_is_a_login_only_if_it_looks_like_one(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    api = "https://api.fakeuni.test/v1/faq"
    hosts = ["api.fakeuni.test", "id.fakeuni.test"]
    quiet = Landing(api, DRAWN)
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=quiet, inner=shell_only(), routes=("/faq",), hosts=hosts
    )
    text = await browse(r)
    assert "needs_login" not in text
    assert "другой адрес" in text
    assert QUESTION not in text  # the page of the other host is not read
    id_login = '<form><input type="text" name="u"><input type="password" name="p"></form>'
    for landing in (
        Landing("https://id.fakeuni.test/realms/x/auth", id_login),
        Landing("https://id.fakeuni.test/login", "<p>Продолжить</p>"),
    ):
        r = await source_rig(
            sm,
            user_id,
            fakeuni_app,
            browser=landing,
            inner=shell_only(),
            routes=("/faq",),
            hosts=hosts,
        )
        assert "needs_login" in await browse(r)


async def test_the_form_that_changes_a_password_is_a_page_and_a_login_form_is_not(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    change = (
        "<!doctype html><title>Смена пароля</title><h1>Смена пароля</h1><form>"
        '<input type="password" name="old"><input type="password" name="new" autocomplete="new-password">'
        '<input type="password" name="again"></form>'
    )
    url = f"{ORIGIN}/profile/password"
    r = await source_rig(
        sm,
        user_id,
        fakeuni_app,
        browser=Landing(url, change, "Смена пароля"),
        inner=shell_only(),
        routes=("/profile/password",),
    )
    text = await browse(r, url)
    assert "needs_login" not in text
    assert "Смена пароля" in text
    one = '<form><input type="password" name="p"></form>'
    r = await source_rig(
        sm,
        user_id,
        fakeuni_app,
        browser=Landing(f"{ORIGIN}/login", one, "Вход"),
        inner=shell_only(),
        routes=("/login",),
    )
    assert "needs_login" in await browse(r, f"{ORIGIN}/login")


# --- 5: the agent's read gives way ---------------------------------------------------------------------


async def test_the_agents_read_is_opened_as_one_that_gives_way(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r, browser = await rig(sm, user_id, fakeuni_app, {FAQ: faq_page(call())})
    await browse(r)
    (opened,) = browser.opened
    assert (opened["read_only"], opened["background"], opened["agent_read"]) == (True, True, True)


async def test_a_read_that_another_action_pushed_out_says_so(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    class Pushed(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)

            async def goto(url: str) -> PageSnapshot:
                # the research of the student starts: it closes this reading, the page fails
                await super(Pushed, self).open(
                    user_id=user_id, connection_id="c", allow_hosts=["x.test"], background=True
                )
                assert handle.closed
                raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Сайт не отвечает")

            handle.goto = goto  # type: ignore[method-assign]
            return handle

    browser = Pushed({FAQ: faq_page(call())})
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=browser, inner=shell_only(), routes=("/faq",)
    )
    text = await browse(r)
    assert "занят другой задачей" in text
    assert "не отвечает" not in text


async def test_the_fake_gateway_lets_the_agents_read_give_way_and_nothing_else() -> None:
    gateway = Recording({})
    kwargs: dict[str, Any] = {
        "user_id": "u",
        "connection_id": "c",
        "allow_hosts": ["x.test"],
        "read_only": True,
        "background": True,
    }
    agent = await gateway.open(**kwargs, agent_read=True)
    other = await gateway.open(**kwargs)
    assert agent.closed
    assert not other.closed
    with pytest.raises(BrowserBusy):  # not the other way round: the agent never pushes out
        await gateway.open(**kwargs, agent_read=True)
    assert not other.closed


async def test_session_values_of_the_vault_are_still_hidden_without_an_inventory(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    """The fake handle has no inventory of its own: the saved session is the whole list."""
    session = session_state(TOKEN)
    r, _ = await rig(
        sm,
        user_id,
        fakeuni_app,
        {FAQ: faq_page(call(), html=DRAWN.replace("Через деканат.", f"Сессия {TOKEN}"))},
        stored=session,
    )
    await r.env.services.vault.put(
        r.env.user_id, r.conn_id, SecretKind.SESSION, dump_session(session)
    )
    text = await browse(r)
    assert TOKEN not in text
    assert "Сессия [скрыто]" in text


async def test_a_slow_reading_does_not_outlive_the_time_it_was_given(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow(Recording):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            await asyncio.sleep(30)  # waiting for a slot of the gateway is inside the time too
            return await super().open(**kwargs)

    monkeypatch.setattr(tools_web, "TOOL_BUDGET_S", 3.5)
    monkeypatch.setattr(tools_web, "MIN_RENDER_SECONDS", 1.0)
    r = await source_rig(
        sm, user_id, fakeuni_app, browser=Slow({}), inner=shell_only(), routes=("/faq",)
    )
    started = asyncio.get_running_loop().time()
    text = await browse(r)
    assert asyncio.get_running_loop().time() - started < 4.5
    assert text.startswith("Результат: ошибка.")
