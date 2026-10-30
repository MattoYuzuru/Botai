"""What the model may be shown from a page, and where a live read may go (L1)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import tools_web
from botai.agent.env import SEEN_PUBLIC, SeenLinks
from botai.agent.tools import build_chat_tools
from tests.agent.test_tools_web import ENTRY, PAGE, public_rig, source_rig

Sm = async_sessionmaker[AsyncSession]


def answering(content: str, kind: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=content, headers={"content-type": kind})
    )


SECRETS_JSON = (
    '{"access_token":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl","sessionKey":"abc123",'
    '"sesskey":"Xy12Zq","csrf":"c-77","password":"hunter2","items":[{"title":"Пара",'
    '"author":"Иванов","note":"ключ Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0","room":"А-101"}]}'
)


async def test_credentials_in_a_json_answer_never_reach_the_model(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(
        sm, user_id, fakeuni_app, inner=answering(SECRETS_JSON, "application/json")
    )
    text = await r.call(
        "browse_source", connection_id=r.conn_id, url="https://fakeuni.test/api/v2/me"
    )
    for secret in ("eyJhbGci", "abc123", "Xy12Zq", "c-77", "hunter2", "Zx9Qw8Er7"):
        assert secret not in text, secret
    # the shape and the harmless values stay: the answer is still useful
    assert "Пара" in text
    assert "Иванов" in text  # "author" is a person, not "auth"
    assert "А-101" in text
    assert "[скрыто]" in text


PAGE_WITH_KEYS = """<!doctype html><title>Раздел</title><body>
<p>Ваш ключ: Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0.</p>
<a href="/news?sesskey=Xy12Zq">С ключом</a> <a href="/news">Новости</a>
<a href="/files/abcdefghij0123456789abcdefghij.pdf">Файл-токен</a></body>"""


async def test_a_page_shows_neither_tokens_nor_links_that_carry_them(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(
        sm, user_id, fakeuni_app, inner=answering(PAGE_WITH_KEYS, "text/html; charset=utf-8")
    )
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert "Zx9Qw8Er7" not in text
    assert "Xy12Zq" not in text
    assert "abcdefghij0123456789" not in text
    assert "Новости — https://fakeuni.test/news" in text


async def test_only_pages_the_source_points_at_are_opened(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, routes=())  # no saved map
    undeclared = "https://fakeuni.test/api/v2/timetable"
    refused = await r.call("browse_source", connection_id=r.conn_id, url=undeclared)
    assert refused.startswith("Результат: не выполнено")
    assert "site_map" in refused
    assert r.spy.requests == []
    # the entry page is the source's own; its links become openable, others do not
    await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    opened = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test/news")
    assert not opened.startswith("Результат: не выполнено")
    # a link as the page showed it, not the same page with other parameters
    other_query = "https://fakeuni.test/news?page=2&sort=old"
    assert (await r.call("browse_source", connection_id=r.conn_id, url=other_query)).startswith(
        "Результат: не выполнено"
    )
    assert (await r.call("browse_source", connection_id=r.conn_id, url=undeclared)).startswith(
        "Результат: не выполнено"
    )
    # what the student wrote in this question may be opened too
    r.env.question_links = {undeclared}
    asked = await r.call("browse_source", connection_id=r.conn_id, url=undeclared)
    assert "Математический анализ" in asked
    # and a link of a page is not an action just because the page lists it
    logout = "https://fakeuni.test/auth/logout"
    assert logout in set(r.env.seen.source)
    assert "похож на действие" in await r.call("browse_source", connection_id=r.conn_id, url=logout)


@pytest.mark.parametrize(
    "path",
    [
        "/log%6Fut",
        "/log%256Fut",
        "/exit",
        "/quit",
        "/subscribe.php?id=3",
        "/enrol/self?id=1",
        "/cancel-registration",
        "/mod/choice/view?sesskey=abc",
        "/x?act=del",
        "/x?do=remove",
        "/x?action=confirm",
        "/account/sign-out",
        # words glued by case or underscore are still words
        "/api/deleteItem",
        "/doLogout",
        "/user/logoutAll",
        "/log_out",
        "/cancelBooking/5",
        "/unsubscribeAll",
        "/api/items/toggle/4",
        "/messages/mark/read",
        "/polls/vote",
        "/register",
        "/signUp",
        "/account/reset",
        "/sessions/terminate",
        "/x?op=removeAll",
        "/LOGOUT",
    ],
)
async def test_hidden_verbs_in_an_address_are_refused_even_when_the_source_lists_it(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, path: str
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, routes=(path.split("?")[0],))
    text = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test" + path)
    assert text.startswith("Результат: не выполнено"), path
    assert r.spy.requests == []


def test_the_links_of_a_thread_are_kept_in_order_and_capped() -> None:
    seen = SeenLinks.load({"public": ["https://a.example.org/1"], "source": 5})
    assert seen.public == ["https://a.example.org/1"]
    assert seen.source == []
    urls = [f"https://a.example.org/{i}" for i in range(SEEN_PUBLIC + 5)]
    seen.remember("public", urls)
    assert len(seen.public) == SEEN_PUBLIC
    assert seen.public[-1] == urls[-1]
    seen.remember("public", [urls[-3]])  # met again: now the newest
    assert seen.public[-1] == urls[-3]
    assert SeenLinks.load(seen.dump()).public == seen.public
    assert seen.changed


async def test_a_www_twin_of_the_site_is_followed_and_nothing_else() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pages.example.org":
            return httpx.Response(301, headers={"location": "https://www.pages.example.org/rules"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    chat, _ = public_rig({"question_links": {"https://pages.example.org/rules"}})
    chat.transport = httpx.MockTransport(handler)
    tools = {t.spec.name: t for t in build_chat_tools(chat)}
    text = await tools["fetch_public_page"].run({"url": "https://pages.example.org/rules"})
    assert "Недоверенная публичная страница" in text

    def elsewhere(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://evil.example.net/x"})

    chat, _ = public_rig({"question_links": {"https://pages.example.org/rules"}})
    chat.transport = httpx.MockTransport(elsewhere)
    tools = {t.spec.name: t for t in build_chat_tools(chat)}
    refused = await tools["fetch_public_page"].run({"url": "https://pages.example.org/rules"})
    assert refused.startswith("Результат: ошибка")


async def test_without_the_egress_proxy_a_private_address_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(host: str) -> list[str]:
        return ["10.0.0.5"] if host.endswith("internal-looking.example.org") else ["93.184.216.34"]

    monkeypatch.setattr(tools_web, "_resolve", resolve)
    chat, seen = public_rig({"question_links": {"https://internal-looking.example.org/x"}})
    chat.transport = None  # a development box: no proxy, no test transport
    tools = {t.spec.name: t for t in build_chat_tools(chat)}
    text = await tools["fetch_public_page"].run({"url": "https://internal-looking.example.org/x"})
    assert "внутренн" in text
    assert seen == []


@pytest.mark.parametrize("path", ["/address-book", "/examinations/schedule", "/news?author=Ivanov"])
async def test_ordinary_words_that_contain_a_verb_are_not_actions(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, path: str
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, routes=(path.split("?")[0],))
    text = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test" + path)
    assert "похож на действие" not in text, path
    assert len(r.spy.requests) == 1  # the address was asked for


async def test_a_redirect_to_an_action_is_not_followed(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    """A page that answers with a redirect to `/logout` must not be allowed to do what the agent
    was forbidden to ask for: each hop is judged before it leaves."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/app":
            return httpx.Response(302, headers={"location": "https://fakeuni.test/auth/logout"})
        return httpx.Response(200, text="вы вышли", headers={"content-type": "text/plain"})

    r = await source_rig(sm, user_id, fakeuni_app, inner=httpx.MockTransport(handler))
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert text.startswith("Результат: ошибка")
    assert "перенаправил на адрес, похожий на действие" in text
    assert requested == ["/app"]  # the second hop never left


async def test_a_redirect_to_an_ordinary_page_is_followed(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(302, headers={"location": "https://fakeuni.test/news"})
        return httpx.Response(200, text="Новости", headers={"content-type": "text/plain"})

    r = await source_rig(sm, user_id, fakeuni_app, inner=httpx.MockTransport(handler))
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert "Новости" in text
