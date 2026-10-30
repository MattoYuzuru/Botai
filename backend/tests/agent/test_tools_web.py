"""Live reading: a connected source (GET, the student's session), public pages, documents (L1)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.env import ChatEnv
from botai.agent.tools import TOOL_SPECS, build_chat_tools
from botai.agent.tools_web import MAX_BROWSES, question_urls
from botai.attachments import service as attachments
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.learn.sitemap import save_site_map
from botai.search import SearchService
from botai.search.service import Provider
from botai.sources.session import dump_session
from tests.agent.conftest import add_user, make_settings
from tests.agent.seed import add_connection, add_document, add_space
from tests.agent.support import NOW, add_thread, make_services
from tests.fakeuni.login import SESSIONS
from tests.sources.helpers import session_state

Sm = async_sessionmaker[AsyncSession]
TOKEN = "tok-browse-session-DO-NOT-LEAK"
ENTRY = "https://fakeuni.test/app"
# What the saved site map of the fake university says it has.
ROUTES = ("/api/v2/me", "/api/v2/timetable", "/api/v2/courses", "/news")


class Spy(httpx.AsyncBaseTransport):
    """Records what leaves the tool and answers from a handler or from the fake university."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


class Rig:
    def __init__(self, env: ChatEnv, spy: Spy, conn_id: str) -> None:
        self.env, self.spy, self.conn_id = env, spy, conn_id
        self.tools = {t.spec.name: t for t in build_chat_tools(env)}

    async def call(self, name: str, **args: Any) -> str:
        return await self.tools[name].run(args)


async def source_rig(
    sm: Sm,
    user_id: str,
    app: FastAPI,
    *,
    session: bool = True,
    registered: bool = True,
    hosts: list[str] | None = None,
    routes: tuple[str, ...] = ROUTES,
    inner: httpx.AsyncBaseTransport | None = None,
) -> Rig:
    space = await add_space(sm, user_id, "Вуз А")
    conn = await add_connection(
        sm, user_id, space, "Личный кабинет", allow_hosts=hosts, entry_url=ENTRY
    )
    async with sm() as session:
        await save_site_map(
            session,
            "https://fakeuni.test",
            {"navigation": [], "routes": list(routes), "endpoints": []},
            NOW,
        )
        await session.commit()
    services = make_services(ScriptedLlm(), sm=sm)
    if session:
        state = session_state(TOKEN)
        if registered:
            SESSIONS.add(TOKEN)
        else:
            SESSIONS.discard(TOKEN)  # the university no longer knows this session
        await services.vault.put(user_id, conn, SecretKind.SESSION, dump_session(state))
    spy = Spy(inner or httpx.ASGITransport(app=app))
    env = ChatEnv(
        services=services,
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
        transport=spy,
    )
    return Rig(env, spy, conn)


# --- browse_source ------------------------------------------------------------------------------


async def test_a_page_of_the_source_is_read_with_the_students_session(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    text = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test/app")
    assert "Личный кабинет" in text
    assert "Задания — https://fakeuni.test/portal/deadlines" in text
    assert "получена сейчас сессией студента" in text
    (request,) = r.spy.requests
    assert request.method == "GET"
    assert TOKEN in request.headers["cookie"]  # the session reached the source...
    assert TOKEN not in text  # ...and never the model
    ref = r.env.sources.refs["S1"]
    assert (ref.title, ref.url, ref.kind) == (
        "Личный кабинет: Личный кабинет",
        "https://fakeuni.test/app",
        "page",
    )


async def test_a_json_answer_is_shown_as_a_short_outline(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    text = await r.call(
        "browse_source", connection_id=r.conn_id, url="https://fakeuni.test/api/v2/timetable"
    )
    assert "Математический анализ" in text
    assert '"items":[' in text
    big = await r.call(
        "browse_source", connection_id=r.conn_id, url="https://fakeuni.test/api/v2/courses?size=5"
    )
    assert "Физика" in big


async def test_an_expired_session_is_reported_as_needs_login(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, registered=False)  # the university forgot it
    text = await r.call(
        "browse_source", connection_id=r.conn_id, url="https://fakeuni.test/api/v2/me"
    )
    assert "needs_login" in text
    assert "request_login" in text
    assert "Иванова" not in text
    # a site that answers an expired session with its login page is recognised too
    page = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test/app")
    assert "needs_login" in page
    assert "Пароль" not in page


async def test_a_page_of_another_host_is_not_opened(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    for url in (
        "https://evil.test/collect",
        "https://fakeuni.test.evil.test/app",
        "http://fakeuni.test/app",
        "https://user:pass@fakeuni.test/app",
        "javascript:alert(1)",
    ):
        text = await r.call("browse_source", connection_id=r.conn_id, url=url)
        assert text.startswith("Результат: не выполнено"), url
    assert r.spy.requests == []


async def test_actions_that_look_like_a_logout_or_a_delete_are_not_opened(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    for path in ("/auth/logout", "/account/delete?id=1", "/api/signout", "/x?action=remove"):
        text = await r.call(
            "browse_source", connection_id=r.conn_id, url="https://fakeuni.test" + path
        )
        assert "похож на действие" in text
    assert r.spy.requests == []


async def test_the_tool_can_only_read_it_has_no_method_and_no_body(fakeuni_app: FastAPI) -> None:
    props = TOOL_SPECS["browse_source"].parameters["properties"]
    assert set(props) == {"connection_id", "url"}
    assert TOOL_SPECS["browse_source"].parameters["additionalProperties"] is False


async def test_pages_per_answer_are_capped(sm: Sm, user_id: str, fakeuni_app: FastAPI) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    for i in range(MAX_BROWSES):
        await r.call(
            "browse_source", connection_id=r.conn_id, url=f"https://fakeuni.test/news?page={i}"
        )
    text = await r.call("browse_source", connection_id=r.conn_id, url="https://fakeuni.test/news")
    assert text.startswith("Результат: не выполнено")
    assert len(r.spy.requests) == MAX_BROWSES


async def test_somebody_elses_source_is_not_there(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    other = await add_user(sm, "other")
    space = await add_space(sm, other, "Чужой")
    foreign = await add_connection(sm, other, space, "Чужой кабинет")
    r = await source_rig(sm, user_id, fakeuni_app)
    text = await r.call("browse_source", connection_id=foreign, url="https://fakeuni.test/app")
    assert "Такого подключения" in text
    assert r.spy.requests == []


# --- fetch_public_page --------------------------------------------------------------------------

PAGE = """<!doctype html><title>Положение об учёбе</title><body>
<h1>Положение</h1><p>Пересдача проходит в ноябре.</p>
<a href="/rules/appendix">Приложение</a> <a href="https://elsewhere.example.org/x">Внешняя</a>
<a href="javascript:void(0)">Скрипт</a></body>"""


def public_rig(env_kwargs: dict[str, Any] | None = None) -> tuple[ChatEnv, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    services = make_services(ScriptedLlm())
    env = ChatEnv(
        services=services,
        user_id="01J00000000000000000000USR",
        thread_id="01J00000000000000000000THR",
        job_id=None,
        space_id=None,
        transport=httpx.MockTransport(handler),
        **(env_kwargs or {}),
    )
    return env, seen


async def test_a_link_from_the_question_is_read_without_any_cookie() -> None:
    question = "Что написано тут: https://pages.example.org/rules?"
    env, seen = public_rig({"question_links": question_urls(question)})
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    text = await tools["fetch_public_page"].run({"url": "https://pages.example.org/rules"})
    assert "Недоверенная публичная страница" in text
    assert "Пересдача проходит в ноябре" in text
    assert "Приложение — https://pages.example.org/rules/appendix" in text
    assert "javascript" not in text
    (request,) = seen
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    # a link of an opened page becomes available, a made-up address does not
    again = await tools["fetch_public_page"].run(
        {"url": "https://pages.example.org/rules/appendix"}
    )
    assert "Недоверенная публичная страница" in again
    invented = await tools["fetch_public_page"].run({"url": "https://pages.example.org/?q=secret"})
    assert invented.startswith("Результат: не выполнено")
    assert len(seen) == 2


async def test_unsafe_public_addresses_are_never_requested() -> None:
    urls = [
        "http://pages.example.org/rules",
        "https://127.0.0.1/admin",
        "https://localhost/admin",
        "https://intranet.internal/x",
        "https://user:pw@pages.example.org/",
        "https://pages.example.org:8443/x",
    ]
    env, seen = public_rig({"question_links": set(urls)})
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    for url in urls:
        text = await tools["fetch_public_page"].run({"url": url})
        assert text.startswith("Результат: не выполнено"), url
    assert seen == []


async def test_a_search_result_may_be_opened_and_nothing_else(sm: Sm, user_id: str) -> None:
    async def send(provider: Provider, body: dict[str, Any], remaining: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://pages.example.org/rules",
                        "title": "Положение",
                        "highlights": ["Официальный документ"],
                    }
                ]
            },
        )

    env, seen = public_rig()
    env.services.search = SearchService(make_settings(exa_api_key="fixture"), db=sm, sender=send)
    env.user_id = user_id
    env.public_question = "положение об учёбе"
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    assert (
        await tools["fetch_public_page"].run({"url": "https://pages.example.org/rules"})
    ).startswith("Результат: не выполнено")
    found = await tools["search_public_web"].run({"query": "положение об учёбе"})
    assert "https://pages.example.org/rules" in found
    opened = await tools["fetch_public_page"].run({"url": "https://pages.example.org/rules"})
    assert "Недоверенная публичная страница" in opened
    assert len(seen) == 1


# --- read_document ------------------------------------------------------------------------------


async def test_a_document_is_read_in_pieces_with_the_next_offset(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    doc = await add_document(
        sm, user_id, space, "Положение", ["а" * 700, "б" * 700, "в" * 700], url=None
    )
    services = make_services(ScriptedLlm(), sm=sm)
    env = ChatEnv(
        services=services,
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    first = await tools["read_document"].run({"document_id": doc, "max_chars": 1000})
    assert f"botai://documents/{doc}" in first
    assert "знаков 2100, показано 0–1000" in first
    assert "Дальше: offset=1000" in first
    last = await tools["read_document"].run({"document_id": doc, "offset": 2000})
    assert "Это конец документа" in last
    assert "в" * 100 in last
    assert env.sources.refs["S1"].url == f"botai://documents/{doc}"


async def test_somebody_elses_document_is_not_readable(sm: Sm, user_id: str) -> None:
    other = await add_user(sm, "other")
    theirs = await add_document(sm, other, await add_space(sm, other), "Чужой", ["секрет"])
    env = ChatEnv(
        services=make_services(ScriptedLlm(), sm=sm),
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    text = await tools["read_document"].run({"document_id": theirs})
    assert "Такого документа" in text
    assert "секрет" not in text


async def test_a_file_of_a_chat_message_is_read_like_a_document(sm: Sm, user_id: str) -> None:
    other = await add_user(sm, "other")
    body = ("Правила пересдачи: заявление до пятницы. " * 40).encode()
    async with sm() as session:
        text = await attachments.create(
            session, user_id, filename="Правила.txt", content_type="text/plain", data=body
        )
        picture = await attachments.create(
            session,
            user_id,
            filename="схема.png",
            content_type=None,
            data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 24,
        )
        theirs = await attachments.create(
            session, other, filename="чужое.txt", content_type="text/plain", data="секрет".encode()
        )
        await session.commit()
    env = ChatEnv(
        services=make_services(ScriptedLlm(), sm=sm),
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    first = await tools["read_document"].run({"document_id": text.id, "max_chars": 500})
    assert f"botai://attachments/{text.id}" in first
    assert "Правила пересдачи" in first
    assert "offset=500" in first
    assert env.outside_read  # what a file holds is not the student's own word
    rest = await tools["read_document"].run({"document_id": text.id, "offset": 500})
    assert "Правила пересдачи" in rest
    seen = await tools["read_document"].run({"document_id": picture.id})
    assert "распознавание пока недоступно" in seen
    assert "Такого документа" in await tools["read_document"].run({"document_id": theirs.id})


# --- site_map -----------------------------------------------------------------------------------


async def test_the_site_map_of_a_source_lists_its_sections_and_routes(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app)
    text = await r.call("site_map", connection_id=r.conn_id)
    assert "Карта разделов «Личный кабинет»" in text
    assert "/api/v2/timetable" in text
    assert "Маршруты" in text
    assert f"Входная страница данных: {ENTRY}" in text
    assert r.spy.requests == []  # the map is read from the base, not from the university
    assert r.env.sources.refs  # the map is a source of the answer
    assert "Такого подключения" in await r.call(
        "site_map", connection_id="01J0000000000000000000NOPE"
    )


async def test_a_source_without_a_saved_map_says_so(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id, "Вуз Б")
    conn = await add_connection(
        sm, user_id, space, "Кабинет", entry_url="https://unmapped.test/app"
    )
    env = ChatEnv(
        services=make_services(ScriptedLlm(), sm=sm),
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    text = await tools["site_map"].run({"connection_id": conn})
    assert "Карта пока не собрана" in text


async def test_somebody_elses_site_map_is_not_shown(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    other = await add_user(sm, "other")
    theirs = await source_rig(sm, other, fakeuni_app)
    env = ChatEnv(
        services=make_services(ScriptedLlm(), sm=sm),
        user_id=user_id,
        thread_id=await add_thread(sm, user_id),
        job_id=None,
        space_id=None,
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    text = await tools["site_map"].run({"connection_id": theirs.conn_id})
    assert text.startswith("Такого подключения")
    assert "/api/v2" not in text
