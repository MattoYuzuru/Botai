"""A personal calendar link typed into the chat (FR-018, Q11, security.md T10): nothing of the
message is kept, the answer is `409 secret_link_calendar`, and the thread asks the student, in
plain words with buttons, whether to connect the calendar. Nothing is created until the student
says yes (L1, real Postgres)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db import models as m
from botai.sources import calendar
from botai.spaces import service as spaces_service
from tests.e2e.support import dump_database
from tests.fakeuni import ical as fakeuni
from tests.jobs.env import Env
from tests.jobs.test_calendar_link import Student, student
from tests.jobs.test_conversations import ctx, dialogue_question, post, start, wait_pending

pytestmark = pytest.mark.db
KEY = "k9Xq2LmZp7Vb4NwR8sTd"
VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "calendar-link-vectors.json").read_text(
        "utf-8"
    )
)
LINKS = [
    f"webcal://calendar.example.org/feed?key={KEY}",
    f"https://calendar.yandex.ru/export/ics.xml?private_token={KEY}&tz_id=Europe/Moscow",
    f"https://calendar.google.com/calendar/ical/student%40example.org/private-{KEY}/basic.ics",
    f"https://outlook.office365.com/owa/calendar/{KEY}@example.org/{KEY}/calendar.html",
    f"https://p52-caldav.icloud.com/published/2/{KEY}",
    f"calendar.yandex.ru/export/ics.xml?private_token={KEY}",
    fakeuni.SERVICE_URL,
    fakeuni.PATH_URL,
]


async def messages(env: Env, thread: str, role: str) -> list[m.ChatMessage]:
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(m.ChatMessage)
                .where(m.ChatMessage.thread_id == thread, m.ChatMessage.role == role)
                .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
            )
        )


async def stored(env: Env, thread: str) -> list[m.ChatMessage]:
    """What the student wrote in the thread."""
    return await messages(env, thread, "user")


async def offers(env: Env, thread: str) -> list[dict[str, Any]]:
    """The questions about a calendar that the thread asked (text blocks of the agent)."""
    return [
        message.blocks[0]
        for message in await messages(env, thread, "agent")
        if str(message.blocks[0].get("markdown", "")).startswith(calendar.OFFER)
    ]


async def made(env: Env, user: str) -> tuple[int, int]:
    """(calendar sources, calendar tasks) of the student."""
    async with env.sm() as session:
        sources = await session.scalar(
            select(func.count())
            .select_from(m.Connection)
            .where(m.Connection.user_id == user, m.Connection.adapter == "ical")
        )
        tasks = await session.scalar(
            select(func.count())
            .select_from(m.Job)
            .where(
                m.Job.user_id == user,
                m.Job.kind == JobKind.CONNECT.value,
                m.Job.payload["calendar"].as_boolean().is_(True),
            )
        )
    return int(sources or 0), int(tasks or 0)


def refusal(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    body = response.json()
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "secret_link_calendar"
    assert body["detail"] == calendar.REFUSAL
    return body


@pytest.mark.parametrize("link", LINKS)
async def test_a_calendar_link_in_the_chat_is_refused_kept_nowhere_and_the_thread_asks(
    env: Env, link: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = await student(env)
    retry_id = new_id()
    traced: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "botai.sources.calendar.diagnostic_event",
        lambda event, **facts: traced.append({"event": event, **facts}),
    )
    sent = await post(
        who.client, who.thread_id, f"Вот моя ссылка: {link} — подключи", clientMessageId=retry_id
    )
    refusal(sent)
    assert link not in sent.text
    assert KEY not in sent.text

    # nothing of the message, no agent turn, no source and no task: only a question with buttons
    assert await stored(env, who.thread_id) == []
    assert await made(env, who.user_id) == (0, 0)
    async with env.sm() as session:
        chats = await session.scalar(
            select(func.count()).select_from(m.Job).where(m.Job.kind == JobKind.CHAT.value)
        )
    assert chats == 0
    (question,) = await offers(env, who.thread_id)
    assert "Подключить календарь вуза «Фейкуни»?" in question["markdown"]
    assert question["quickReplies"] == [calendar.YES, calendar.NO]
    assert link not in json.dumps(question)

    # pasted again, or the same request retried: refused again, and the question is not repeated
    again = await post(who.client, who.thread_id, link, clientMessageId=retry_id)
    refusal(again)
    assert len(await offers(env, who.thread_id)) == 1
    assert await made(env, who.user_id) == (0, 0)
    assert await stored(env, who.thread_id) == []

    # the link is nowhere: not in the database, not in the trace of the request
    for text in (await dump_database(env.sm), json.dumps(traced)):
        assert KEY not in text
        assert "private_token" not in text
        assert fakeuni.PATH_TOKEN not in text
    assert {e["event"] for e in traced} == {"chat.calendar_link"}


@pytest.mark.parametrize("case", VECTORS["secret"], ids=lambda case: case["name"])
async def test_the_server_refuses_every_shared_vector_that_is_a_personal_link(
    env: Env, case: dict[str, str]
) -> None:
    who = await student(env)
    refusal(await post(who.client, who.thread_id, case["text"]))
    assert await stored(env, who.thread_id) == []
    assert await made(env, who.user_id) == (0, 0)


@pytest.mark.parametrize("case", VECTORS["ordinary"], ids=lambda case: case["name"])
async def test_the_server_lets_every_shared_ordinary_message_through(
    env: Env, case: dict[str, str]
) -> None:
    if not case["text"]:
        pytest.skip("an empty message is not a message")
    who = await student(env)
    sent = await post(who.client, who.thread_id, case["text"])
    assert sent.status_code == 202, sent.text
    assert len(await stored(env, who.thread_id)) == 1
    assert await offers(env, who.thread_id) == []


async def test_a_calendar_link_is_refused_before_the_message_is_looked_at_for_anything_else(
    env: Env,
) -> None:
    """A stale or unknown task, a missing file: the link is dropped first, never handed back."""
    who = await student(env)
    sent = await post(who.client, who.thread_id, LINKS[1], targetJobId="0" * 26)
    refusal(sent)
    stale = await post(who.client, who.thread_id, LINKS[1], targetJobId=new_id())
    refusal(stale)
    assert await stored(env, who.thread_id) == []


async def test_the_question_names_the_university_of_the_thread_and_materials_fall_back(
    env: Env,
) -> None:
    who = await student(env)
    other = await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    scoped = (
        await who.client.post("/api/v1/chat/threads", json={"spaceId": other.json()["id"]})
    ).json()["id"]
    refusal(await post(who.client, scoped, LINKS[0]))
    (question,) = await offers(env, scoped)
    assert "«Второй институт»" in question["markdown"]
    assert await made(env, who.user_id) == (0, 0)


async def test_a_thread_of_the_materials_space_falls_back_to_the_only_university(env: Env) -> None:
    who = await student(env)
    async with env.sm() as session:
        materials = await spaces_service.ensure_space(session, who.user_id, None)
        await session.commit()
    thread = (await who.client.post("/api/v1/chat/threads", json={"spaceId": materials.id})).json()
    refusal(await post(who.client, thread["id"], LINKS[0]))
    (question,) = await offers(env, thread["id"])
    assert "«Фейкуни»" in question["markdown"]
    assert question["quickReplies"] == [calendar.YES, calendar.NO]


async def test_with_several_universities_and_no_hint_the_question_is_which_one(env: Env) -> None:
    who = await student(env)
    await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    refusal(await post(who.client, who.thread_id, LINKS[1]))
    (question,) = await offers(env, who.thread_id)
    assert "Для какого вуза подключить календарь?" in question["markdown"]
    assert len(question["quickReplies"]) == 2
    assert all(reply.startswith("Календарь для ") for reply in question["quickReplies"])
    assert all(len(reply) <= 60 for reply in question["quickReplies"])
    assert await made(env, who.user_id) == (0, 0)
    assert KEY not in await dump_database(env.sm)


async def test_with_more_universities_than_buttons_the_student_is_told_to_write_the_name(
    env: Env,
) -> None:
    who = await student(env)
    for n in range(5):
        await who.client.post("/api/v1/spaces", json={"title": f"Институт номер {n}"})
    refusal(await post(who.client, who.thread_id, LINKS[1]))
    (question,) = await offers(env, who.thread_id)
    assert len(question["quickReplies"]) == 4
    assert "напишите его название" in question["markdown"]


async def test_a_student_with_no_university_is_asked_to_name_one(env: Env) -> None:
    client = env.new_client()
    account = await env.account(client)
    thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    refusal(await post(client, thread, LINKS[0]))
    (question,) = await offers(env, thread)
    assert "Напишите название вуза" in question["markdown"]
    assert "quickReplies" not in question
    assert await made(env, account.id) == (0, 0)


async def test_a_calendar_that_works_is_offered_a_new_link_not_a_second_calendar(env: Env) -> None:
    who = await student(env)
    async with env.sm() as session:
        conn = m.Connection(
            id=new_id(),
            user_id=who.user_id,
            space_id=who.space_id,
            origin="https://calendar.fakeuni.test",
            kind="calendar",
            adapter="ical",
            title="Календарь",
            status="active",
            allow_hosts=["calendar.fakeuni.test"],
        )
        spec = m.AdapterSpecRow(
            origin="https://calendar.fakeuni.test",
            adapter="ical",
            revision=1,
            state="active",
            spec={"adapter": "ical", "origin": "https://calendar.fakeuni.test"},
        )
        session.add_all([spec, conn])
        await session.flush()
        conn.spec_id = spec.id
        await session.commit()
        spec_id = spec.id
    try:
        refusal(await post(who.client, who.thread_id, LINKS[1]))
        (question,) = await offers(env, who.thread_id)
        assert "уже подключён" in question["markdown"]
        assert question["quickReplies"] == [calendar.YES_REPLACE, calendar.NO_REPLACE]
        assert await made(env, who.user_id) == (1, 0)
    finally:
        async with env.sm() as session:
            await session.delete(await session.get(m.AdapterSpecRow, spec_id))
            await session.commit()


async def test_a_task_that_waits_for_the_link_here_or_elsewhere_is_pointed_at(env: Env) -> None:
    from botai.agent.env import ChatEnv
    from botai.agent.tools_sources import connect_calendar

    who = await student(env)
    elsewhere = (await who.client.post("/api/v1/chat/threads", json={})).json()["id"]
    await connect_calendar(
        ChatEnv(
            services=env.services,
            user_id=who.user_id,
            thread_id=who.thread_id,
            job_id=None,
            space_id=None,
        ),
        {"university_query": "Фейкуни"},
    )
    here = await post(who.client, who.thread_id, LINKS[1])
    refusal(here)
    (open_here,) = await offers(env, who.thread_id)
    assert "уже открыто выше" in open_here["markdown"]
    assert "quickReplies" not in open_here
    away = await post(who.client, elsewhere, LINKS[1])
    refusal(away)
    (pointer,) = await offers(env, elsewhere)
    assert f"botai://threads/{who.thread_id}" in pointer["markdown"]
    assert "защищённое поле" in pointer["markdown"]
    assert await made(env, who.user_id) == (1, 1)  # the question started nothing


async def test_a_calendar_the_student_switched_off_is_not_reopened_by_a_pasted_link(
    env: Env,
) -> None:
    from botai.agent.env import ChatEnv
    from botai.agent.tools_sources import connect_calendar

    who = await student(env)
    await connect_calendar(
        ChatEnv(
            services=env.services,
            user_id=who.user_id,
            thread_id=who.thread_id,
            job_id=None,
            space_id=None,
        ),
        {"university_query": "Фейкуни"},
    )
    async with env.sm() as session:
        job = await session.scalar(select(m.Job).where(m.Job.user_id == who.user_id))
        conn = await session.scalar(select(m.Connection).where(m.Connection.user_id == who.user_id))
        assert job is not None
        assert conn is not None
        job.state, conn.status = "cancelled", "disabled"
        await session.commit()
    refusal(await post(who.client, who.thread_id, LINKS[1]))
    (question,) = await offers(env, who.thread_id)
    assert "отключён" in question["markdown"]
    assert "quickReplies" not in question
    assert await made(env, who.user_id) == (1, 1)


async def test_a_link_in_the_answer_to_a_question_of_a_task_is_refused_the_same_way(
    env: Env,
) -> None:
    client, user, thread, job_id = await start(env)
    space = await client.post("/api/v1/spaces", json={"title": "Фейкуни"})
    assert space.status_code in (200, 201)
    question = dialogue_question(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    await wait_pending(client, thread)

    refusal(await post(client, thread, LINKS[1], targetJobId=job_id))
    assert await stored(env, thread) == []
    assert len(await offers(env, thread)) == 1
    assert KEY not in await dump_database(env.sm)
    waiting.cancel()


async def test_a_link_in_an_answer_given_through_the_answers_endpoint_gets_the_same_question(
    env: Env,
) -> None:
    client, user, thread, job_id = await start(env)
    await client.post("/api/v1/spaces", json={"title": "Фейкуни"})
    question = dialogue_question(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    await wait_pending(client, thread)

    answered = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": question.id, "values": LINKS[1]}
    )
    refusal(answered)
    assert KEY not in answered.text
    assert await stored(env, thread) == []
    (offer,) = await offers(env, thread)
    assert offer["quickReplies"] == [calendar.YES, calendar.NO]
    async with env.sm() as session:
        answers = await session.scalar(
            select(func.count()).select_from(m.JobAnswer).where(m.JobAnswer.job_id == job_id)
        )
    assert answers == 0  # the question of the task is still open
    assert KEY not in await dump_database(env.sm)
    waiting.cancel()


async def test_the_client_that_kept_a_link_out_of_its_queue_asks_the_thread_to_offer(
    env: Env,
) -> None:
    who = await student(env)
    first = await who.client.post(f"/api/v1/chat/threads/{who.thread_id}/calendar-link")
    assert first.status_code == 204
    again = await who.client.post(f"/api/v1/chat/threads/{who.thread_id}/calendar-link")
    assert again.status_code == 204
    (question,) = await offers(env, who.thread_id)  # once
    assert question["quickReplies"] == [calendar.YES, calendar.NO]
    assert await made(env, who.user_id) == (0, 0)


async def test_nobody_asks_a_question_in_the_thread_of_another_student(env: Env) -> None:
    alice, bob = await student(env), await student(env)
    sent = await post(bob.client, alice.thread_id, LINKS[0])
    assert sent.status_code == 404
    offered = await bob.client.post(f"/api/v1/chat/threads/{alice.thread_id}/calendar-link")
    assert offered.status_code == 404
    assert await offers(env, alice.thread_id) == []
    assert await made(env, alice.user_id) == (0, 0)
    assert await made(env, bob.user_id) == (0, 0)


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
    assert await offers(env, who.thread_id) == []


async def test_a_second_link_while_the_question_is_the_last_word_adds_nothing(env: Env) -> None:
    who: Student = await student(env)
    for link in LINKS[:3]:
        await post(who.client, who.thread_id, link)
    assert len(await offers(env, who.thread_id)) == 1


# --- a page of a calendar service (not a personal link) gets a question of its own -----------------


async def page_offers(env: Env, thread: str) -> list[dict[str, Any]]:
    return [
        message.blocks[0]
        for message in await messages(env, thread, "agent")
        if str(message.blocks[0].get("markdown", "")).startswith(calendar.PAGE_LEAD)
    ]


async def offer_page(env: Env, who: Student) -> None:
    async with env.sm() as session:
        thread = await session.get(m.ChatThread, who.thread_id)
        assert thread is not None
        await calendar.offer_calendar(session, who.user_id, thread, env.clock.now(), page=True)


async def test_a_page_of_a_calendar_service_is_told_apart_from_a_personal_link_and_shows_the_way(
    env: Env,
) -> None:
    who = await student(env)
    await offer_page(env, who)
    (question,) = await page_offers(env, who.thread_id)
    text = question["markdown"]
    assert "а не кабинет вуза" in text
    assert "личная ссылка" not in text  # nothing here is secret: it is a page
    assert "в чат её присылать нельзя" not in text
    assert "Подключить календарь вуза «Фейкуни»?" in text
    assert "«Экспорт»" in text  # the way to the export link, in words
    assert question["quickReplies"] == [calendar.YES, calendar.NO]
    assert await offers(env, who.thread_id) == []  # the question about a personal link is not it

    await offer_page(env, who)  # asked again while it is the last word: nothing is added
    assert len(await page_offers(env, who.thread_id)) == 1
    assert await made(env, who.user_id) == (0, 0)  # and nothing is created before the answer


async def test_with_several_universities_the_question_about_a_page_asks_which_one(env: Env) -> None:
    who = await student(env)
    await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    await offer_page(env, who)
    (question,) = await page_offers(env, who.thread_id)
    assert "Для какого вуза подключить календарь?" in question["markdown"]
    assert all(reply.startswith("Календарь для ") for reply in question["quickReplies"])


@asynccontextmanager
async def working_calendar(env: Env, who: Student) -> AsyncIterator[None]:
    """A calendar of the student that works. Its recipe row is removed whatever happens: the
    table is not cleared with the users, and other suites count its rows."""
    origin = "https://calendar.fakeuni.test"
    try:
        async with env.sm() as session:
            conn = m.Connection(
                id=new_id(),
                user_id=who.user_id,
                space_id=who.space_id,
                origin=origin,
                kind="calendar",
                adapter="ical",
                title="Календарь",
                status="active",
                allow_hosts=["calendar.fakeuni.test"],
            )
            spec = m.AdapterSpecRow(
                origin=origin,
                adapter="ical",
                revision=1,
                state="active",
                spec={"adapter": "ical", "origin": origin},
            )
            session.add_all([spec, conn])
            await session.flush()
            conn.spec_id = spec.id
            await session.commit()
        yield
    finally:
        async with env.sm() as session:
            await session.execute(
                delete(m.AdapterSpecRow).where(
                    m.AdapterSpecRow.origin == origin, m.AdapterSpecRow.adapter == "ical"
                )
            )
            await session.commit()


async def test_a_calendar_that_works_is_offered_a_new_link_when_a_page_is_given(env: Env) -> None:
    who = await student(env)
    async with working_calendar(env, who):
        await offer_page(env, who)
        (question,) = await page_offers(env, who.thread_id)
        assert "уже подключён" in question["markdown"]
        assert question["quickReplies"] == [calendar.YES_REPLACE, calendar.NO_REPLACE]
        assert await made(env, who.user_id) == (1, 0)
