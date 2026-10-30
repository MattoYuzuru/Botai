"""Credentials in what a page, a file or a source's answer holds never reach the model (L1).

By shape (tokens, JWT, cookie lines) and by name: the value after `access_token`, `sesskey`,
`MoodleSession` is hidden however short it is, a whole object under such a name too, and a
`{name, value}` pair whose name is a credential. The same for every way text gets in: a JSON answer
named text/html or text/plain, `<pre>` and `<code>`, a script variable, a public page, a document."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import webtext
from botai.agent.env import ChatEnv
from botai.agent.safety import NOT_SECRET, STRONG, WEAK, key_class, scrub_secrets
from botai.agent.tools import build_chat_tools
from botai.attachments import service as attachments
from botai.core.llm import ScriptedLlm
from tests.agent.seed import add_document, add_space
from tests.agent.support import add_thread, make_services
from tests.agent.test_tools_web import ENTRY, source_rig
from tests.agent.test_tools_web_guards import SECRETS_JSON, answering

Sm = async_sessionmaker[AsyncSession]

SHORT_VALUES = ("aB3cD4eF5g", "Xy12Zq", "abc12", "abc123def", "c-77", "hunter2")


def assert_hidden(text: str, *values: str) -> None:
    for value in values or SHORT_VALUES:
        assert value not in text, value


@pytest.mark.parametrize("kind", ["text/html; charset=utf-8", "text/plain", "application/json"])
async def test_a_json_answer_is_an_outline_whatever_the_server_calls_it(
    sm: Sm, user_id: str, fakeuni_app: FastAPI, kind: str
) -> None:
    r = await source_rig(sm, user_id, fakeuni_app, inner=answering(SECRETS_JSON, kind))
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert_hidden(text, "eyJhbGci", "abc123", "Xy12Zq", "c-77", "hunter2", "Zx9Qw8Er7")
    assert "Иванов" in text  # the shape and the harmless values stay
    assert "А-101" in text
    assert '"items":[' in text  # it is the outline, not the raw body


async def test_secrets_in_pre_code_and_scripts_are_hidden_by_their_names(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    page = (
        "<!doctype html><title>Профиль</title><body><h1>Профиль</h1>"
        "<pre>access_token: aB3cD4eF5g\nuser: ivanov</pre>"
        '<code>{"sesskey":"Xy12Zq","title":"Курс"}</code>'
        "<p>var sesskey = 'abc12'; MoodleSession=abc123def; Группа БПМ-231</p>"
        "<pre>Authorization: Bearer abc.def.ghi</pre></body>"
    )
    r = await source_rig(sm, user_id, fakeuni_app, inner=answering(page, "text/html"))
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert_hidden(text)
    assert "ghi" not in text
    assert "ivanov" in text  # a name that is not a credential is data
    assert "Группа БПМ-231" in text
    assert "[скрыто]" in text


async def test_a_json_inside_a_pre_is_an_outline_with_everything_under_a_secret_key_hidden(
    sm: Sm, user_id: str, fakeuni_app: FastAPI
) -> None:
    body = (
        '{"user":{"name":"Иванов","tokens":{"access":"aB3cD4eF5g","refresh":"Xy12Zq"}},'
        '"cookies":[{"name":"MoodleSession","value":"abc123def"}],'
        '"headers":[{"name":"X-CSRF-Token","value":"c-77"},{"name":"Accept","value":"json"}],'
        '"items":[{"title":"Пара"}]}'
    )
    page = f"<!doctype html><title>x</title><pre>{body}</pre>"
    r = await source_rig(sm, user_id, fakeuni_app, inner=answering(page, "text/html"))
    text = await r.call("browse_source", connection_id=r.conn_id, url=ENTRY)
    assert_hidden(text, "aB3cD4eF5g", "Xy12Zq", "abc123def", "c-77")
    assert "Иванов" in text
    assert "Пара" in text
    assert '"Accept"' in text  # a pair with a harmless name keeps its value
    assert '"json"' in text


async def test_a_public_page_and_a_document_hide_the_same_things() -> None:
    page = (
        "<!doctype html><title>Страница</title><p>token = abc12; sid: Xy12Zq</p>"
        '<pre>{"password":"hunter2","note":"ok"}</pre>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page, headers={"content-type": "text/plain"})

    env = ChatEnv(
        services=make_services(ScriptedLlm()),
        user_id="01J00000000000000000000USR",
        thread_id="01J00000000000000000000THR",
        job_id=None,
        space_id=None,
        transport=httpx.MockTransport(handler),
        question_links={"https://pages.example.org/p"},
    )
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    text = await tools["fetch_public_page"].run({"url": "https://pages.example.org/p"})
    assert_hidden(text, "abc12", "Xy12Zq", "hunter2")


async def test_a_saved_document_and_a_file_of_a_message_hide_credentials_in_every_piece(
    sm: Sm, user_id: str
) -> None:
    space = await add_space(sm, user_id)
    doc = await add_document(
        sm,
        user_id,
        space,
        "Настройки",
        ["access_token: aB3cD4eF5g\nМожно сменить пароль в профиле", '"sesskey": "Xy12Zq"'],
    )
    async with sm() as session:
        att = await attachments.create(
            session,
            user_id,
            filename="note.txt",
            content_type="text/plain",
            data=b"var sesskey = 'abc12';\nMoodleSession=abc123def\n",
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
    saved = await tools["read_document"].run({"document_id": doc})
    sent = await tools["read_document"].run({"document_id": att.id})
    assert_hidden(saved, "aB3cD4eF5g", "Xy12Zq")
    assert_hidden(sent, "abc12", "abc123def")
    assert "Можно сменить пароль в профиле" in saved  # prose about passwords is not a password


# --- the rules themselves ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("access_token", STRONG),
        ("accessToken", STRONG),
        ("sesskey", STRONG),
        ("userSid", STRONG),
        ("PHPSESSID", STRONG),
        ("XSRF-TOKEN", STRONG),
        ("apiKey", STRONG),
        ("cookies", STRONG),
        ("password", STRONG),
        ("MoodleSession", WEAK),
        ("session", WEAK),
        ("sortKey", WEAK),
        ("Tokenization", NOT_SECRET),
        ("authors", NOT_SECRET),
        ("title", NOT_SECRET),
        ("sessions_count", WEAK),
    ],
)
def test_a_name_is_a_credential_by_its_words(name: str, kind: int) -> None:
    assert key_class(name) == kind


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("access_token: aB3cD4eF5g", "aB3cD4eF5g"),
        ('"sesskey":"Xy12Zq"', "Xy12Zq"),
        ("var sesskey = 'abc12';", "abc12"),
        ("MoodleSession=abc123def; theme=dark", "abc123def"),
        ('{"token": {"a": "b1", "c": ["d2"]}, "ok": 1}', "b1"),
        ('name="csrf_token" value="x9y8"', "x9y8"),
        ('{"name":"XSRF-TOKEN","value":"zz1"}', "zz1"),
        ("https://x.test/p?sesskey=ab12&page=2", "ab12"),
        ("access_token: Bearer abc.def and more", "abc.def"),
    ],
)
def test_the_value_after_a_credential_name_is_hidden_however_short(text: str, hidden: str) -> None:
    out = scrub_secrets(text)
    assert hidden not in out
    assert "[скрыто]" in out
    assert scrub_secrets(out) == out  # again changes nothing


@pytest.mark.parametrize(
    "text",
    [
        "Session: Lecture on Monday",
        "Tokenization: intro to NLP",
        "Authors: Ivanov, Petrov",
        "Время 10:30, аудитория 4",
        "Токен можно получить в профиле",
        "Пароль можно сменить в профиле",
    ],
)
def test_prose_and_other_names_are_left_alone(text: str) -> None:
    assert scrub_secrets(text) == text
    assert webtext.mask_text(text) == text


def test_a_json_sent_as_text_gets_an_outline_and_other_text_does_not() -> None:
    assert (
        webtext.data_body('{"a": [1, 2], "token": {"x": "y"}}') == '{"a":[1,2],"token":"[скрыто]"}'
    )
    assert webtext.data_body("Просто текст: token: abc12") == "Просто текст: token: [скрыто]"
    assert webtext.data_body("123") == "123"  # a bare number is no answer of an API
