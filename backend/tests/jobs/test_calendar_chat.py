"""A personal calendar link typed into the chat (FR-018, Q11, security.md T10): nothing of the
message is kept, the answer is `409 secret_link_calendar`, and the protected field for the link
opens in the same thread (L1, real Postgres)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db import models as m
from tests.e2e.support import dump_database
from tests.fakeuni import ical as fakeuni
from tests.jobs.env import Env
from tests.jobs.test_calendar_link import Student, student
from tests.jobs.test_conversations import ctx, dialogue_question, post, start, wait_pending

pytestmark = pytest.mark.db
KEY = "k9Xq2LmZp7Vb4NwR8sTd"
OPENED = "Это личная ссылка календаря — вставьте её в защищённое поле: я открыл его ниже"
LINKS = [
    f"webcal://calendar.example.org/feed?key={KEY}",
    f"https://calendar.yandex.ru/export/ics.xml?private_token={KEY}&tz_id=Europe/Moscow",
    f"https://calendar.google.com/calendar/ical/student%40example.org/private-{KEY}/basic.ics",
    f"https://outlook.office365.com/owa/calendar/{KEY}@example.org/{KEY}/calendar.ics",
    f"calendar.yandex.ru/export/ics.xml?private_token={KEY}",
    fakeuni.SERVICE_URL,
    fakeuni.PATH_URL,
]


async def stored(env: Env, thread: str) -> list[m.ChatMessage]:
    """What the student wrote in the thread."""
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(m.ChatMessage).where(
                    m.ChatMessage.thread_id == thread, m.ChatMessage.role == "user"
                )
            )
        )


async def calendar_tasks(env: Env, user: str) -> list[m.Job]:
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(m.Job).where(
                    m.Job.user_id == user,
                    m.Job.kind == JobKind.CONNECT.value,
                    m.Job.payload["calendar"].as_boolean().is_(True),
                )
            )
        )


def refusal(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    body = response.json()
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "secret_link_calendar"
    return body


@pytest.mark.parametrize("link", LINKS)
async def test_a_calendar_link_in_the_chat_is_refused_kept_nowhere_and_the_field_opens(
    env: Env, link: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = await student(env)
    retry_id = new_id()
    traced: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "botai.agent.conversations.diagnostic_event",
        lambda event, **facts: traced.append({"event": event, **facts}),
    )
    sent = await post(
        who.client, who.thread_id, f"Вот моя ссылка: {link} — подключи", clientMessageId=retry_id
    )
    body = refusal(sent)
    assert body["detail"] == OPENED
    assert link not in sent.text
    assert KEY not in sent.text

    # no message of the student, no agent turn; the protected field belongs to a task of the thread
    assert await stored(env, who.thread_id) == []
    shown = (await who.client.get(f"/api/v1/chat/threads/{who.thread_id}/messages")).json()
    assert [row for row in shown if row["role"] == "user"] == []
    (task,) = await calendar_tasks(env, who.user_id)
    assert (task.thread_id, task.state) == (who.thread_id, "queued")
    async with env.sm() as session:
        chats = await session.scalar(
            select(func.count()).select_from(m.Job).where(m.Job.kind == JobKind.CHAT.value)
        )
    assert chats == 0
    runtime = (await who.client.get(f"/api/v1/chat/threads/{who.thread_id}/runtime")).json()
    assert [t["id"] for t in runtime["tasks"]] == [task.id]

    # pasted again (or retried by the client): refused again, and still one task
    again = await post(who.client, who.thread_id, link, clientMessageId=retry_id)
    assert refusal(again)["detail"] == OPENED
    assert len(await calendar_tasks(env, who.user_id)) == 1
    assert await stored(env, who.thread_id) == []

    # the link is nowhere: not in the database, not in the trace of the request, not in the task
    dump = await dump_database(env.sm)
    for text in (dump, json.dumps(traced)):
        assert KEY not in text
        assert "private_token" not in text
        assert fakeuni.PATH_TOKEN not in text
    assert {e["event"] for e in traced} == {"chat.calendar_link"}
    assert {e["outcome"] for e in traced} == {"started"}


async def test_the_task_opened_for_a_pasted_link_is_the_calendar_of_the_threads_university(
    env: Env,
) -> None:
    who = await student(env)
    other = await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    scoped = await who.client.post("/api/v1/chat/threads", json={"spaceId": other.json()["id"]})
    sent = await post(who.client, scoped.json()["id"], LINKS[0])
    assert refusal(sent)["detail"] == OPENED
    (task,) = await calendar_tasks(env, who.user_id)
    async with env.sm() as session:
        conn = await session.get(m.Connection, task.connection_id)
    assert conn is not None
    assert (conn.space_id, conn.kind, conn.adapter) == (other.json()["id"], "calendar", "ical")
    assert task.thread_id == scoped.json()["id"]


async def test_with_several_universities_and_no_hint_the_student_is_asked_which(env: Env) -> None:
    who = await student(env)
    await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    sent = await post(who.client, who.thread_id, LINKS[1])
    body = refusal(sent)
    assert body["detail"].startswith("Это личная ссылка календаря, в чат её вставлять нельзя")
    assert "для какого вуза" in body["detail"]
    assert KEY not in sent.text
    assert await stored(env, who.thread_id) == []
    assert await calendar_tasks(env, who.user_id) == []
    async with env.sm() as session:
        sources = await session.scalar(
            select(func.count())
            .select_from(m.Connection)
            .where(m.Connection.user_id == who.user_id)
        )
    assert sources == 0
    assert KEY not in await dump_database(env.sm)


async def test_a_calendar_the_student_switched_off_is_not_reopened_by_a_pasted_link(
    env: Env,
) -> None:
    who = await student(env)
    await post(who.client, who.thread_id, LINKS[0])
    (task,) = await calendar_tasks(env, who.user_id)
    async with env.sm() as session:
        job = await session.get(m.Job, task.id)
        conn = await session.get(m.Connection, task.connection_id)
        assert job is not None
        assert conn is not None
        job.state, conn.status = "cancelled", "disabled"
        await session.commit()
    sent = await post(who.client, who.thread_id, LINKS[1])
    assert "отключили" in refusal(sent)["detail"]
    assert len(await calendar_tasks(env, who.user_id)) == 1
    assert KEY not in sent.text


async def test_a_student_with_no_university_is_asked_for_one_too(env: Env) -> None:
    client = env.new_client()
    account = await env.account(client)
    thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    sent = await post(client, thread, LINKS[0])
    assert "для какого вуза" in refusal(sent)["detail"]
    assert await calendar_tasks(env, account.id) == []


async def test_a_link_in_the_answer_to_a_question_of_a_task_is_refused_the_same_way(
    env: Env,
) -> None:
    client, user, thread, job_id = await start(env)
    space = await client.post("/api/v1/spaces", json={"title": "Фейкуни"})
    assert space.status_code in (200, 201)
    question = dialogue_question(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    await wait_pending(client, thread)

    sent = await post(client, thread, LINKS[1], targetJobId=job_id)
    assert refusal(sent)["detail"] == OPENED
    assert await stored(env, thread) == []  # the question is still open and nothing was written
    assert KEY not in await dump_database(env.sm)
    waiting.cancel()


async def test_other_links_with_a_secret_are_still_refused_with_a_clear_text(env: Env) -> None:
    who = await student(env)
    for link in (
        f"https://lms.example.org/page?access_token={KEY}",
        f"https://student:{KEY}@lms.example.org/page",
    ):
        sent = await post(who.client, who.thread_id, f"смотри {link}")
        assert sent.status_code == 422, sent.text
        assert sent.json()["code"] == "validation"
        assert "секретный ключ" in sent.json()["detail"]
        assert KEY not in sent.text
    assert await stored(env, who.thread_id) == []
    assert await calendar_tasks(env, who.user_id) == []


@pytest.mark.parametrize(
    "text",
    [
        "Моё расписание лежит в Яндекс.Календаре, как его подключить?",
        "Подключи календарь вуза",
        "Посмотри https://lms.example.org/schedule и скажи, что завтра",
        "https://calendar.yandex.ru/week",
    ],
)
async def test_talking_about_calendars_and_ordinary_links_still_go_to_the_agent(
    env: Env, text: str
) -> None:
    who = await student(env)
    sent = await post(who.client, who.thread_id, text)
    assert sent.status_code == 202, sent.text
    assert len(await stored(env, who.thread_id)) == 1
    assert await calendar_tasks(env, who.user_id) == []


async def test_nobody_opens_a_field_in_the_thread_of_another_student(env: Env) -> None:
    alice, bob = await student(env), await student(env)
    sent = await post(bob.client, alice.thread_id, LINKS[0])
    assert sent.status_code == 404
    assert await calendar_tasks(env, alice.user_id) == []
    assert await calendar_tasks(env, bob.user_id) == []


async def test_a_second_calendar_link_while_the_field_is_open_adds_no_task(env: Env) -> None:
    who: Student = await student(env)
    await post(who.client, who.thread_id, LINKS[0])
    await post(who.client, who.thread_id, LINKS[1])
    await post(who.client, who.thread_id, LINKS[2])
    assert len(await calendar_tasks(env, who.user_id)) == 1
