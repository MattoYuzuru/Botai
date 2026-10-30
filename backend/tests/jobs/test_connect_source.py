"""The agent connects sources in its own thread (FR-035..038): real database, real queue."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from botai.agent.env import ChatEnv
from botai.agent.tools_sources import (
    add_document_url,
    connect_source,
    find_university,
    list_universities,
    sync_source,
    task_status,
)
from botai.contracts.agent_ui import TaskBlock
from botai.core.ids import new_id
from botai.db.models import AdapterSpecRow, Connection, Job, Space, University
from botai.jobs.dialogue import terminal_text
from botai.learn import revisions
from tests.jobs.env import Env
from tests.sources import recipes

pytestmark = pytest.mark.db
LMS = "https://smart.fakeuni.test/courses?tab=current"
PORTAL = "https://lk.fakeuni.test/schedule"
PORTAL_TASKS = "https://lk.fakeuni.test/tasks"
PORTAL_ORIGIN = "https://lk.fakeuni.test"
SHEET = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"
HSE = "vysshaya-shkola-ekonomiki"


@pytest.fixture(autouse=True)
async def no_recipes_left_behind(env: Env) -> AsyncIterator[None]:
    """The sources of these tests share one site; the recipes they are given must not outlive them
    (the table is not cleared with the users, and other suites count its rows)."""
    yield
    async with env.sm() as session:
        await session.execute(delete(AdapterSpecRow).where(AdapterSpecRow.origin == PORTAL_ORIGIN))
        await session.commit()


async def student(env: Env) -> tuple[httpx.AsyncClient, str]:
    client = env.new_client()
    return client, (await env.account(client)).id


async def thread_of(client: httpx.AsyncClient) -> str:
    return (await client.post("/api/v1/chat/threads", json={})).json()["id"]


def tools(env: Env, user: str, thread: str, space_id: str | None = None) -> ChatEnv:
    return ChatEnv(
        services=env.services, user_id=user, thread_id=thread, job_id=None, space_id=space_id
    )


async def jobs_of(env: Env, user: str, kind: str | None = None) -> list[Job]:
    async with env.sm() as session:
        query = select(Job).where(Job.user_id == user).order_by(Job.created_at, Job.id)
        if kind:
            query = query.where(Job.kind == kind)
        return list(await session.scalars(query))


async def count(env: Env, model: Any, *where: Any) -> int:
    async with env.sm() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def no_ulid(text: str) -> None:
    """Ids reach the student only inside botai:// links."""
    stripped = re.sub(r"botai://\w+/[0-9A-Z]{26}", "", text)
    stripped = re.sub(r"(connection_id|university_id|note_id)=\S+", "", stripped)
    assert not re.search(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", stripped), stripped


async def test_a_new_source_gets_the_universitys_space_a_human_title_and_a_task_here(
    env: Env,
) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    chat = tools(env, user, thread)
    result = await connect_source(
        chat,
        {
            "url": LMS,
            "title": "Smart LMS",
            "university_query": "Вышка",
            "kind_hint": "lms",
            "note": "Нужны дедлайны",
        },
    )
    assert result.startswith("Результат: запущено")
    assert "в этом чате" in result
    (job,) = await jobs_of(env, user, "connect")
    no_ulid(result)
    assert job.id not in result
    assert job.thread_id == thread
    assert job.payload["url"] == LMS
    assert job.payload["source"] == "chat"
    intent = job.payload["learning_intent"]
    assert intent["entry_urls"] == [LMS]
    assert intent["user_note"] == "Нужны дедлайны"
    assert intent["discovery_mode"] == "auto"
    async with env.sm() as session:
        space = await session.scalar(select(Space).where(Space.user_id == user))
        conn = await session.get(Connection, job.connection_id)
    assert space is not None
    assert (space.university_id, space.title) == (HSE, "НИУ ВШЭ")
    assert conn is not None
    assert (conn.title, conn.kind, conn.status) == ("Smart LMS", "lms", "draft")
    assert (conn.origin, conn.allow_hosts, conn.space_id) == (
        "https://smart.fakeuni.test",
        ["smart.fakeuni.test"],
        space.id,
    )
    (card,) = chat.blocks
    assert isinstance(card, TaskBlock)
    assert (card.kind, card.thread_id, card.connection_id, card.job_id) == (
        "connect",
        thread,
        conn.id,
        job.id,
    )


async def test_a_title_must_be_a_human_name_not_an_address(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    for title in ("lk.fakeuni.test", "https://lk.fakeuni.test/", "", "   "):
        result = await connect_source(
            chat, {"url": PORTAL, "title": title, "university_query": "Вышка"}
        )
        assert result.startswith("Результат: не запущено")
        assert "человеческое название" in result
    assert await count(env, Connection, Connection.user_id == user) == 0
    assert await count(env, Space, Space.user_id == user) == 0
    assert await jobs_of(env, user) == []


@pytest.mark.parametrize(
    "bad",
    [
        "https://lk.fakeuni.test/?access_token=eyJabcdefghijklmnop.abcdefghijklmnop.abcdefghijklmnop",
        "https://user:secret-word@lk.fakeuni.test/",
        "http://lk.fakeuni.test/",
        "not a link",
    ],
)
async def test_an_unsafe_or_missing_address_starts_nothing(env: Env, bad: str) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await connect_source(
        chat, {"url": bad, "title": "Кабинет", "university_query": "Вышка"}
    )
    assert result.startswith("Результат: не запущено")
    assert "secret-word" not in result
    assert "eyJ" not in result
    assert await count(env, Connection, Connection.user_id == user) == 0
    assert await jobs_of(env, user) == []
    # without an address the source is still made, but not without knowing the university
    unknown = await connect_source(chat, {"title": "Кабинет"})
    assert unknown.startswith("Результат: нужно уточнение")
    assert await count(env, Connection, Connection.user_id == user) == 0


async def test_the_same_site_is_never_connected_twice(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    first = tools(env, user, thread)
    await connect_source(
        first, {"url": LMS, "title": "Smart LMS", "university_query": "Вышка", "kind_hint": "lms"}
    )
    (job,) = await jobs_of(env, user, "connect")
    again = tools(env, user, thread)
    result = await connect_source(
        again,
        {
            "url": "https://smart.fakeuni.test/other/page",
            "title": "Другой",
            "university_query": "ВШЭ",
        },
    )
    assert result.startswith("Результат: уже подключено, ничего не запущено")
    assert "Smart LMS" in result
    assert f"botai://connections/{job.connection_id}" in result
    assert "в другом чате" not in result
    assert len(await jobs_of(env, user, "connect")) == 1
    assert await count(env, Connection, Connection.user_id == user) == 1
    (card,) = again.blocks  # the running task is shown, not started
    assert isinstance(card, TaskBlock)
    assert card.job_id == job.id


async def test_a_task_running_in_another_thread_is_linked_not_restarted(env: Env) -> None:
    client, user = await student(env)
    here, elsewhere = await thread_of(client), await thread_of(client)
    await connect_source(
        tools(env, user, elsewhere),
        {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"},
    )
    (job,) = await jobs_of(env, user, "connect")
    asking = tools(env, user, here)
    result = await connect_source(
        asking, {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"}
    )
    assert f"botai://threads/{elsewhere}" in result
    assert job.id not in result
    assert len(await jobs_of(env, user, "connect")) == 1
    (card,) = asking.blocks
    assert isinstance(card, TaskBlock)
    assert card.thread_id == elsewhere  # the card leads to the thread that owns the task
    status = await task_status(tools(env, user, here), {})
    assert f"botai://threads/{elsewhere}" in status
    assert "Задачи в других чатах" in status


async def test_a_finished_source_is_reported_with_status_and_what_can_be_done(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    await connect_source(
        chat, {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"}
    )
    (job,) = await jobs_of(env, user, "connect")
    async with env.sm() as session:
        row = await session.get(Job, job.id)
        conn = await session.get(Connection, job.connection_id)
        assert row is not None
        assert conn is not None
        row.state = "done"
        conn.status = "needs_reauth"
        await session.commit()
    result = await connect_source(
        tools(env, user, await thread_of(client)),
        {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"},
    )
    assert "нужен повторный вход" in result
    assert "request_login" in result
    assert len(await jobs_of(env, user, "connect")) == 1
    async with env.sm() as session:
        conn = await session.get(Connection, job.connection_id)
        assert conn is not None
        conn.status = "disabled"
        await session.commit()
    disabled = await connect_source(
        tools(env, user, await thread_of(client)),
        {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"},
    )
    assert "отключён студентом" in disabled
    assert "request_login" not in disabled


async def test_sources_of_one_university_share_one_space(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    await connect_source(
        tools(env, user, thread), {"url": PORTAL, "title": "Личный кабинет", "university_id": HSE}
    )
    await connect_source(
        tools(env, user, thread),
        {"url": LMS, "title": "Smart LMS", "university_query": "Высшая школа экономики"},
    )
    async with env.sm() as session:
        spaces = list(await session.scalars(select(Space).where(Space.user_id == user)))
        conns = list(await session.scalars(select(Connection).where(Connection.user_id == user)))
    assert [s.university_id for s in spaces] == [HSE]
    assert {c.space_id for c in conns} == {spaces[0].id}
    assert {c.title for c in conns} == {"Личный кабинет", "Smart LMS"}


async def test_parallel_requests_end_with_one_source_and_one_space(env: Env) -> None:
    client, user = await student(env)
    threads = [await thread_of(client) for _ in range(3)]
    async with asyncio.timeout(10):
        same = await asyncio.gather(
            *(
                connect_source(
                    tools(env, user, thread),
                    {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"},
                )
                for thread in threads
            )
        )
    assert sum(r.startswith("Результат: запущено") for r in same) == 1
    assert sum(r.startswith("Результат: уже подключено") for r in same) == 2
    assert await count(env, Connection, Connection.user_id == user) == 1
    assert len(await jobs_of(env, user, "connect")) == 1
    async with asyncio.timeout(10):
        await asyncio.gather(
            *(
                connect_source(
                    tools(env, user, thread),
                    {
                        "url": f"https://s{i}.fakeuni.test/",
                        "title": f"Источник {i}",
                        "university_id": HSE,
                    },
                )
                for i, thread in enumerate(threads)
            )
        )
    assert await count(env, Space, Space.user_id == user) == 1
    assert await count(env, Connection, Connection.user_id == user) == 4


async def test_an_unsure_university_is_asked_about_before_anything_is_created(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await connect_source(
        chat, {"url": PORTAL, "title": "Кабинет", "university_query": "Политех"}
    )
    assert result.startswith("Результат: нужно уточнение")
    assert "offer_choices" in result
    assert "Санкт-Петербургский политехнический" in result
    assert await count(env, Space, Space.user_id == user) == 0
    assert await count(env, Connection, Connection.user_id == user) == 0
    # find_university shows the same doubt to the model before it even tries
    found = await find_university(chat, {"query": "Политех"})
    assert "по части названия" in found
    assert "подтвердите вуз" in found


async def test_a_university_the_catalog_does_not_know_is_created_from_the_name(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    name = "Институт Фикстур " + new_id()[-6:]
    result = await connect_source(
        chat, {"url": PORTAL, "title": "Кабинет института", "university_query": name}
    )
    assert result.startswith("Результат: запущено")
    async with env.sm() as session:
        space = await session.scalar(select(Space).where(Space.user_id == user))
        assert space is not None
        university = await session.get(University, space.university_id)
    assert space.title == name
    assert university is not None
    assert university.title == name


async def test_without_a_named_university_the_agent_must_ask_unless_one_is_obvious(
    env: Env,
) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    chat = tools(env, user, thread)
    args = {"url": PORTAL, "title": "Кабинет"}
    assert (await connect_source(chat, args)).startswith("Результат: нужно уточнение")
    assert await count(env, Space, Space.user_id == user) == 0
    # with exactly one university the source goes there
    await connect_source(chat, {"url": LMS, "title": "Smart LMS", "university_id": HSE})
    other = await connect_source(chat, args)
    assert other.startswith("Результат: запущено")
    assert await count(env, Space, Space.user_id == user) == 1
    # two universities and nothing said: ask again
    await connect_source(
        chat, {"url": "https://x.fakeuni.test/", "title": "Икс", "university_id": "msu"}
    )
    again = await connect_source(chat, {"url": "https://y.fakeuni.test/", "title": "Игрек"})
    assert again.startswith("Результат: нужно уточнение")


async def test_a_thread_of_a_university_connects_to_that_university(env: Env) -> None:
    client, user = await student(env)
    await connect_source(
        tools(env, user, await thread_of(client)),
        {"url": LMS, "title": "Smart LMS", "university_id": HSE},
    )
    await connect_source(
        tools(env, user, await thread_of(client)),
        {"url": "https://x.fakeuni.test/", "title": "Икс", "university_id": "msu"},
    )
    async with env.sm() as session:
        space = await session.scalar(
            select(Space).where(Space.user_id == user, Space.university_id == "msu")
        )
    assert space is not None
    thread = (await client.post("/api/v1/chat/threads", json={"spaceId": space.id})).json()["id"]
    result = await connect_source(
        tools(env, user, thread, space_id=space.id), {"url": PORTAL, "title": "Кабинет"}
    )
    assert result.startswith("Результат: запущено")
    async with env.sm() as session:
        conn = await session.scalar(
            select(Connection).where(Connection.user_id == user, Connection.origin.like("%lk.%"))
        )
    assert conn is not None
    assert conn.space_id == space.id


async def test_a_university_warning_is_handed_to_the_agent(env: Env) -> None:
    client, user = await student(env)
    uid = "warning-" + new_id().lower()
    async with env.sm() as session:
        session.add(
            University(
                id=uid,
                title="Вуз с предупреждением",
                short_title="ВсП",
                origins=[],
                accent_hue=280,
                third_party_warning="Сайт просит не использовать сторонние приложения.",
            )
        )
        await session.commit()
    result = await connect_source(
        tools(env, user, await thread_of(client)),
        {"url": PORTAL, "title": "Кабинет", "university_id": uid},
    )
    assert "Сайт просит не использовать сторонние приложения." in result
    assert "дословно" in result


async def test_list_universities_shows_sources_statuses_tasks_and_links(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    assert "пока нет вузов" in await list_universities(tools(env, user, thread), {})
    await connect_source(
        tools(env, user, thread),
        {
            "url": PORTAL,
            "title": "Единый личный кабинет",
            "university_query": "Вышка",
            "kind_hint": "portal",
        },
    )
    (job,) = await jobs_of(env, user, "connect")
    listing = await list_universities(tools(env, user, thread), {})
    assert "«НИУ ВШЭ»" in listing
    assert "«Единый личный кабинет»" in listing
    assert f"botai://connections/{job.connection_id}" in listing
    assert "Подключение: Единый личный кабинет — в очереди" in listing
    assert f"botai://threads/{thread}" in listing
    found = await find_university(tools(env, user, thread), {"query": "вшэ"})
    assert "уже есть у студента" in found
    assert f"university_id={HSE}" in found


async def test_a_public_document_becomes_a_task_of_this_thread_once(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    chat = tools(env, user, thread)
    result = await add_document_url(chat, {"url": SHEET, "title": "Список группы"})
    assert result.startswith("Результат: запущено")
    assert "Мои материалы" in result
    (job,) = await jobs_of(env, user, "ingest")
    assert job.thread_id == thread
    assert job.payload["source"]["url"] == SHEET
    async with env.sm() as session:
        space = await session.scalar(select(Space).where(Space.user_id == user))
        conn = await session.get(Connection, job.connection_id)
    assert space is not None
    assert (space.title, space.university_id) == ("Мои материалы", None)
    assert conn is not None
    assert (conn.kind, conn.space_id) == ("sheet", space.id)
    (card,) = chat.blocks
    assert isinstance(card, TaskBlock)
    assert card.kind == "ingest"
    again = await add_document_url(tools(env, user, await thread_of(client)), {"url": SHEET})
    assert again.startswith("Результат: уже читается")
    assert len(await jobs_of(env, user, "ingest")) == 1
    refused = await add_document_url(chat, {"url": "http://example.org/a.pdf"})
    assert refused.startswith("Результат: не запущено")


async def test_a_document_of_a_university_goes_to_its_space(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await add_document_url(
        chat,
        {"url": "https://docs.fakeuni.test/rules.pdf", "title": "Правила", "university_id": HSE},
    )
    assert "«НИУ ВШЭ»" in result
    async with env.sm() as session:
        space = await session.scalar(select(Space).where(Space.user_id == user))
    assert space is not None
    assert space.university_id == HSE


async def test_sync_source_starts_a_sync_in_this_thread_and_refuses_what_is_not_ready(
    env: Env,
) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    _, ready = await env.connection(user, status="active")
    async with env.sm() as session:
        row = await session.get(Connection, ready)
        assert row is not None
        row.adapter = "recipe"
        await session.commit()
    _, draft = await env.connection(user, status="draft")
    other = (await env.account()).id
    _, foreign = await env.connection(other, status="active")
    chat = tools(env, user, thread)
    result = await sync_source(chat, {"connection_id": ready})
    assert "запущено в этом чате" in result
    (sync,) = await jobs_of(env, user, "sync")
    assert (sync.thread_id, sync.connection_id) == (thread, ready)
    (card,) = chat.blocks
    assert isinstance(card, TaskBlock)
    assert card.kind == "sync"
    # a second request meets the running one, in whichever thread it lives
    again = await sync_source(tools(env, user, await thread_of(client)), {"connection_id": ready})
    assert "уже" in again or "запущено" in again
    assert len(await jobs_of(env, user, "sync")) == 1
    assert "не запущено" in await sync_source(chat, {"connection_id": draft})
    assert "Такого подключения" in await sync_source(chat, {"connection_id": foreign})
    assert len(await jobs_of(env, user, "sync")) == 1


async def test_task_status_reports_stage_and_what_the_task_waits_for(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    chat = tools(env, user, thread)
    assert await task_status(chat, {}) == "Активных и недавних задач нет."
    await connect_source(
        chat, {"url": PORTAL, "title": "Единый личный кабинет", "university_query": "Вышка"}
    )
    (job,) = await jobs_of(env, user, "connect")
    async with env.sm() as session:
        row = await session.get(Job, job.id)
        assert row is not None
        row.state = "waiting_user"
        row.stage = "Жду пароль"
        row.payload = {
            **row.payload,
            "dialogue": {"pending": {"channel": "native", "secret": True, "block_id": new_id()}},
        }
        await session.commit()
    text = await task_status(tools(env, user, thread), {})
    assert "Подключение: Единый личный кабинет — ждёт ответа студента" in text
    assert "Жду пароль" in text
    assert "защищённый ввод пароля или кода в приложении" in text
    assert job.id not in text


async def test_a_source_without_an_address_is_made_and_finds_its_entry_itself(env: Env) -> None:
    client, user = await student(env)
    thread = await thread_of(client)
    chat = tools(env, user, thread)
    result = await connect_source(
        chat, {"title": "Smart LMS", "university_query": "Вышка", "kind_hint": "lms"}
    )
    assert result.startswith("Результат: запущено")
    assert "Адреса не было" in result
    (job,) = await jobs_of(env, user, "connect")
    assert "url" not in job.payload
    assert job.payload["learning_intent"]["entry_urls"] == []
    assert job.thread_id == thread
    async with env.sm() as session:
        conn = await session.get(Connection, job.connection_id)
    assert conn is not None
    assert (conn.origin, conn.title, conn.status) == ("", "Smart LMS", "draft")
    again = await connect_source(
        tools(env, user, thread), {"title": "Smart LMS", "university_query": "ВШЭ"}
    )
    assert again.startswith("Результат: уже подключено, ничего не запущено")
    assert len(await jobs_of(env, user, "connect")) == 1


async def test_an_unfinished_source_is_continued_not_offered_as_a_choice(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    ask = {"url": PORTAL, "title": "Расписание", "university_query": "Вышка"}
    await connect_source(tools(env, user, first), ask)
    (job,) = await jobs_of(env, user, "connect")
    async with env.sm() as session:  # the student cancelled the login half-way
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.state = "cancelled"
        await session.commit()
    asking = tools(env, user, again)
    result = await connect_source(asking, ask)
    assert result.startswith("Результат: продолжено")
    jobs = await jobs_of(env, user, "connect")
    assert len(jobs) == 2
    assert {j.thread_id for j in jobs if j.state != "cancelled"} == {again}
    assert await count(env, Connection, Connection.user_id == user) == 1
    (card,) = asking.blocks
    assert isinstance(card, TaskBlock)


# --- an unfinished source asked for again: the current ask is applied (the incident) --------------


async def cancel_first_attempt(env: Env, user: str, **dialogue: Any) -> Job:
    """The only connect task of the user ends as the student left it: cancelled, with what the
    dialogue of that attempt added to its learning intent."""
    (job,) = await jobs_of(env, user, "connect")
    async with env.sm() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.state = "cancelled"
        stored.payload = {
            **stored.payload,
            "learning_intent": {**stored.payload["learning_intent"], **dialogue},
        }
        await session.commit()
    return stored


async def test_asking_again_for_schedule_and_tasks_reads_both_not_the_old_timetable_note(
    env: Env,
) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(
        tools(env, user, first),
        {
            "url": PORTAL,
            "title": "Единый кабинет",
            "university_query": "Вышка",
            "note": "Подключение по ссылке на расписание",
        },
    )
    old = await cancel_first_attempt(env, user)
    assert old.payload["learning_intent"]["requested_kinds"] == ["scheduleEvents"]

    result = await connect_source(
        tools(env, user, again),
        {
            "url": PORTAL,
            "title": "Единый кабинет",
            "university_query": "Вышка",
            "kinds": ["scheduleEvents", "deadlines"],
            "note": "Переподключи: прочитай расписание и задания заново",
        },
    )

    assert result.startswith("Результат: продолжено")
    assert "Читается: расписание, дедлайны" in result  # what the agent may promise, in words
    (_, resumed) = await jobs_of(env, user, "connect")
    assert resumed.thread_id == again
    assert resumed.connection_id == old.connection_id
    assert resumed.payload["learning_intent"] == {
        "entry_urls": [PORTAL],
        "requested_kinds": ["scheduleEvents", "deadlines"],
        "absent_kinds": [],
        "user_note": "Переподключи: прочитай расписание и задания заново",
        "discovery_mode": "requested",
    }
    assert await count(env, Connection, Connection.user_id == user) == 1


async def test_resuming_keeps_the_pages_and_exclusions_of_the_earlier_dialogue(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(
        tools(env, user, first),
        {
            "url": PORTAL,
            "title": "Единый кабинет",
            "university_query": "Вышка",
            "note": "Расписание",
        },
    )
    # in the dialogue the student named deadlines, excluded rooms and gave the page of the tasks
    await cancel_first_attempt(
        env,
        user,
        entry_urls=[PORTAL, PORTAL_TASKS],
        requested_kinds=["deadlines"],
        absent_kinds=["locations"],
        discovery_mode="requested",
    )

    await connect_source(
        tools(env, user, again),
        {
            "url": PORTAL,
            "title": "Единый кабинет",
            "university_query": "Вышка",
            "kinds": ["scheduleEvents"],
            "note": "Ещё раз с начала",
        },
    )

    (_, resumed) = await jobs_of(env, user, "connect")
    intent = resumed.payload["learning_intent"]
    assert intent["requested_kinds"] == ["deadlines", "scheduleEvents"]  # the new ask adds
    assert intent["absent_kinds"] == ["locations"]
    assert intent["entry_urls"] == [PORTAL, PORTAL_TASKS]
    assert intent["user_note"] == "Ещё раз с начала"
    assert intent["discovery_mode"] == "requested"


async def test_resuming_with_only_a_new_note_updates_the_note_and_leaves_discovery_open(
    env: Env,
) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    ask = {"url": PORTAL, "title": "Единый кабинет", "university_query": "Вышка"}
    await connect_source(tools(env, user, first), {**ask, "note": "Подключение по ссылке"})
    await cancel_first_attempt(env, user, requested_kinds=["scheduleEvents", "people", "courses"])

    await connect_source(tools(env, user, again), {**ask, "note": "Посмотри всё, что есть"})

    (_, resumed) = await jobs_of(env, user, "connect")
    intent = resumed.payload["learning_intent"]
    assert intent["user_note"] == "Посмотри всё, что есть"
    assert intent["discovery_mode"] == "auto"  # nothing was named: what discovery saw is no wish
    assert intent["requested_kinds"] == ["scheduleEvents"]


async def test_a_new_source_with_named_kinds_requests_exactly_those(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await connect_source(
        chat,
        {
            "url": LMS,
            "title": "Smart LMS",
            "university_query": "Вышка",
            "kinds": ["deadlines", "deadlines", "courses"],
        },
    )
    assert result.startswith("Результат: запущено")
    (job,) = await jobs_of(env, user, "connect")
    intent = job.payload["learning_intent"]
    assert (intent["requested_kinds"], intent["discovery_mode"]) == (
        ["deadlines", "courses"],
        "requested",
    )


async def test_an_unknown_kind_is_a_message_to_the_model_and_starts_nothing(env: Env) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await connect_source(
        chat,
        {"url": LMS, "title": "Smart LMS", "university_query": "Вышка", "kinds": ["timetable"]},
    )
    assert result.startswith("Результат: не запущено. kinds")
    assert "scheduleEvents" in result
    assert await jobs_of(env, user, "connect") == []


# --- a page of a calendar service is no cabinet --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://calendar.yandex.ru/week?uid=123",
        "https://calendar.google.com/calendar/u/0/r",
        "https://outlook.office365.com/calendar/view/week",
    ],
)
async def test_a_page_of_a_calendar_service_is_never_a_source_the_agent_is_pointed_to_the_calendar(
    env: Env, url: str
) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    result = await connect_source(
        chat, {"url": url, "title": "Календарь", "university_query": "Вышка"}
    )
    assert result.startswith("Результат: не запущено")
    assert "connect_calendar" in result  # the way the agent takes instead
    assert "«Экспорт»" in result  # and what the student is told: where the export link is taken
    assert "Секретный адрес в формате iCal" in result
    assert "в чат" in result  # the link itself is never asked for in the chat
    assert await jobs_of(env, user) == []
    assert await count(env, Connection, Connection.user_id == user) == 0
    assert chat.blocks == []


# --- a source that works is extended with the kinds it lacks (the schedule stays as it is) ---------


async def make_working(
    env: Env, user: str, *, kinds: tuple[str, ...] = ("timetable",), scope: list[str] | None = None
) -> Connection:
    """The only source of the user, as a connection that works: a recipe with the given endpoints,
    status `active`, its first task done."""
    endpoints = {"timetable": recipes.timetable_endpoint, "deadlines": recipes.deadlines_endpoint}
    async with env.sm() as session:
        (job,) = list(await session.scalars(select(Job).where(Job.user_id == user)))
        job.state = "done"
        conn = await session.get(Connection, job.connection_id)
        assert conn is not None
        revision = await revisions.latest_revision(session, conn.origin, "recipe") + 1
        spec = recipes.recipe_spec(
            [endpoints[name]() for name in kinds],
            revision=revision,
            require=tuple("scheduleEvents" if name == "timetable" else name for name in kinds),
        )
        row = AdapterSpecRow(
            origin=conn.origin, adapter="recipe", revision=revision, state="active", spec=spec
        )
        session.add(row)
        await session.flush()
        conn.adapter, conn.spec_id, conn.spec_revision = "recipe", row.id, revision
        conn.status, conn.data_scope = "active", scope
        await session.commit()
        return conn


async def working_schedule(env: Env, **kw: Any) -> tuple[ChatEnv, str, Connection]:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(
        tools(env, user, first),
        {
            "url": PORTAL,
            "title": "Единый кабинет",
            "university_query": "Вышка",
            "kinds": ["scheduleEvents"],
        },
    )
    conn = await make_working(env, user, **kw)
    return tools(env, user, again), user, conn


ASK = {"url": PORTAL, "title": "Единый кабинет", "university_query": "Вышка"}


async def test_adding_tasks_to_a_working_schedule_starts_a_task_that_learns_only_the_tasks(
    env: Env,
) -> None:
    chat, user, conn = await working_schedule(env)

    result = await connect_source(
        chat, {**ASK, "kinds": ["scheduleEvents", "deadlines"], "note": "добавь задания"}
    )

    assert result.startswith("Результат: запущено")
    assert "уже подключён (читается: расписание)" in result
    assert "добавляю то, чего в нём ещё нет: дедлайны" in result
    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.connection_id == conn.id
    assert extension.thread_id == chat.thread_id
    assert extension.payload["extend"] is True
    assert extension.payload["learning_intent"] == {
        "entry_urls": [PORTAL],
        "requested_kinds": ["deadlines"],  # the schedule is read already: not asked again
        "absent_kinds": [],
        "user_note": "добавь задания",
        "discovery_mode": "requested",
    }
    async with env.sm() as session:  # the source is not touched by the ask itself
        stored = await session.get(Connection, conn.id)
        assert stored is not None
        assert (stored.status, stored.spec_revision) == ("active", conn.spec_revision)
    (card,) = chat.blocks
    assert isinstance(card, TaskBlock)


async def test_what_the_source_already_reads_is_not_extended(env: Env) -> None:
    chat, user, _ = await working_schedule(env)
    result = await connect_source(chat, {**ASK, "kinds": ["scheduleEvents"]})
    assert result.startswith("Результат: уже подключено, ничего не запущено")
    assert "добавлять нечего" in result
    assert len(await jobs_of(env, user, "connect")) == 1
    bare = await connect_source(chat, ASK)  # nothing named: nothing to add
    assert bare.startswith("Результат: уже подключено, ничего не запущено")
    assert len(await jobs_of(env, user, "connect")) == 1


async def test_a_kind_the_student_excluded_before_is_not_added_unasked(env: Env) -> None:
    chat, user, conn = await working_schedule(env)
    async with env.sm() as session:  # in the dialogue of the first task: rooms are not needed
        (first,) = list(await session.scalars(select(Job).where(Job.user_id == user)))
        first.payload = {
            **first.payload,
            "learning_intent": {**first.payload["learning_intent"], "absent_kinds": ["locations"]},
        }
        await session.commit()

    await connect_source(chat, {**ASK, "kinds": ["deadlines"], "note": "добавь задания"})

    (_, extension) = await jobs_of(env, user, "connect")
    intent = extension.payload["learning_intent"]
    assert intent["requested_kinds"] == ["deadlines"]
    assert intent["absent_kinds"] == ["locations"]  # the exclusion goes on with the task
    assert extension.connection_id == conn.id


async def test_a_kind_the_recipe_has_but_the_connection_does_not_read_is_added_too(
    env: Env,
) -> None:
    chat, user, _ = await working_schedule(
        env, kinds=("timetable", "deadlines"), scope=["scheduleEvents"]
    )
    result = await connect_source(chat, {**ASK, "kinds": ["deadlines"]})
    assert result.startswith("Результат: запущено")
    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.payload["learning_intent"]["requested_kinds"] == ["deadlines"]


async def test_a_source_that_does_not_work_is_not_extended_but_offered_the_usual_choices(
    env: Env,
) -> None:
    chat, user, conn = await working_schedule(env)
    async with env.sm() as session:
        stored = await session.get(Connection, conn.id)
        assert stored is not None
        stored.status = "needs_reauth"
        await session.commit()
    result = await connect_source(chat, {**ASK, "kinds": ["deadlines"]})
    assert result.startswith("Результат: уже подключено, ничего не запущено")
    assert len(await jobs_of(env, user, "connect")) == 1


# --- the agent names the source it sees (the address is not in its context) ----------------------


async def test_a_source_named_by_its_id_is_extended_without_an_address(env: Env) -> None:
    chat, user, conn = await working_schedule(env)
    result = await connect_source(
        chat,
        {
            "connection_id": conn.id,
            "title": "Единый кабинет",
            "kinds": ["scheduleEvents", "deadlines"],
            "note": "добавь задания",
        },
    )
    assert result.startswith("Результат: запущено")
    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.connection_id == conn.id
    assert extension.payload["extend"] is True
    assert extension.payload["learning_intent"]["requested_kinds"] == ["deadlines"]
    assert extension.payload["learning_intent"]["entry_urls"] == [PORTAL]  # the earlier page stays
    assert await count(env, Connection, Connection.user_id == user) == 1


async def test_an_unfinished_source_named_by_its_id_is_resumed(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(tools(env, user, first), {**ASK, "note": "Расписание"})
    old = await cancel_first_attempt(env, user)
    result = await connect_source(
        tools(env, user, again),
        {
            "connection_id": old.connection_id,
            "title": "Единый кабинет",
            "kinds": ["scheduleEvents", "deadlines"],
        },
    )
    assert result.startswith("Результат: продолжено")
    (_, resumed) = await jobs_of(env, user, "connect")
    assert resumed.connection_id == old.connection_id
    assert resumed.payload["learning_intent"]["requested_kinds"] == ["scheduleEvents", "deadlines"]
    assert "extend" not in resumed.payload
    assert await count(env, Connection, Connection.user_id == user) == 1


async def test_a_source_the_student_does_not_have_is_not_touched_by_an_id(env: Env) -> None:
    chat, user, conn = await working_schedule(env)
    result = await connect_source(
        chat, {"connection_id": new_id(), "title": "Кабинет", "kinds": ["deadlines"]}
    )
    assert result.startswith("Результат: не запущено. Такого подключения у студента нет")
    other = tools(env, (await student(env))[1], chat.thread_id)
    result = await connect_source(
        other, {"connection_id": conn.id, "title": "Кабинет", "kinds": ["deadlines"]}
    )
    assert result.startswith("Результат: не запущено. Такого подключения у студента нет")
    assert len(await jobs_of(env, user, "connect")) == 1


async def test_a_source_asked_for_by_its_title_without_an_address_is_not_made_twice(
    env: Env,
) -> None:
    chat, user, conn = await working_schedule(env)
    result = await connect_source(
        chat,
        {
            "title": "единый кабинет",  # the case of the letters is not a second source
            "university_query": "Вышка",
            "kinds": ["scheduleEvents", "deadlines"],
        },
    )
    assert result.startswith("Результат: запущено")
    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.connection_id == conn.id
    assert await count(env, Connection, Connection.user_id == user) == 1


async def test_a_calendar_is_never_continued_or_extended_through_the_site_tool(env: Env) -> None:
    chat, user, conn = await working_schedule(env)
    async with env.sm() as session:
        stored = await session.get(Connection, conn.id)
        assert stored is not None
        stored.adapter = "ical"
        await session.commit()
    result = await connect_source(
        chat, {"connection_id": conn.id, "title": "Календарь", "kinds": ["deadlines"]}
    )
    assert "connect_calendar" in result
    assert len(await jobs_of(env, user, "connect")) == 1


# --- review round: the ask of now, the title of a source, an address of another site ---------------


async def set_note_of_first_task(env: Env, user: str, note: str) -> None:
    """The dialogue of the first task left a note in its intent, as a student's «только …» does."""
    async with env.sm() as session:
        (first,) = list(await session.scalars(select(Job).where(Job.user_id == user)))
        first.payload = {
            **first.payload,
            "learning_intent": {**first.payload["learning_intent"], "user_note": note},
        }
        await session.commit()


async def test_a_note_of_the_earlier_dialogue_does_not_outlive_an_ask_with_kinds(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(
        tools(env, user, first), {**ASK, "kinds": ["scheduleEvents"], "note": "только расписание"}
    )
    await cancel_first_attempt(env, user)

    await connect_source(
        tools(env, user, again), {**ASK, "kinds": ["scheduleEvents", "deadlines"]}
    )  # no note: the old one said «only the schedule»

    (_, resumed) = await jobs_of(env, user, "connect")
    intent = resumed.payload["learning_intent"]
    assert intent["requested_kinds"] == ["scheduleEvents", "deadlines"]
    assert intent["user_note"] == ""
    assert "note_applied" not in resumed.payload


async def test_a_note_of_the_earlier_dialogue_does_not_outlive_an_extension_either(
    env: Env,
) -> None:
    chat, user, _ = await working_schedule(env)
    await set_note_of_first_task(env, user, "только расписание")

    await connect_source(chat, {**ASK, "kinds": ["scheduleEvents", "deadlines"]})

    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.payload["extend"] is True
    assert extension.payload["learning_intent"]["requested_kinds"] == ["deadlines"]
    assert extension.payload["learning_intent"]["user_note"] == ""


async def test_an_ask_without_kinds_keeps_the_note_but_it_is_not_classified_again(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(tools(env, user, first), {**ASK, "note": "Расписание на этой странице"})
    await cancel_first_attempt(env, user)

    await connect_source(tools(env, user, again), ASK)  # «переподключи», nothing more

    (_, resumed) = await jobs_of(env, user, "connect")
    assert resumed.payload["learning_intent"]["user_note"] == "Расписание на этой странице"
    assert resumed.payload["note_applied"] is True  # it was classified when it was said


async def test_the_student_who_asks_for_only_this_replaces_the_kinds_not_adds(env: Env) -> None:
    client, user = await student(env)
    first, again = await thread_of(client), await thread_of(client)
    await connect_source(tools(env, user, first), {**ASK, "kinds": ["scheduleEvents"]})
    await cancel_first_attempt(env, user)

    await connect_source(
        tools(env, user, again), {**ASK, "kinds": ["deadlines"], "replace_kinds": True}
    )

    (_, resumed) = await jobs_of(env, user, "connect")
    assert resumed.payload["learning_intent"]["requested_kinds"] == ["deadlines"]


async def test_two_sources_with_one_title_are_not_chosen_between_but_listed(env: Env) -> None:
    chat, user, conn = await working_schedule(env)
    async with env.sm() as session:
        twin = Connection(
            user_id=user,
            space_id=conn.space_id,
            origin="https://other.fakeuni.test",
            kind="lms",
            title="Единый кабинет",
            status="draft",
            allow_hosts=["other.fakeuni.test"],
        )
        session.add(twin)
        await session.commit()
        twin_id = twin.id

    result = await connect_source(
        chat,
        {"title": "Единый кабинет", "university_query": "Вышка", "kinds": ["deadlines"]},
    )

    assert result.startswith("Результат: нужно уточнение")
    assert f"connection_id={conn.id}" in result
    assert f"connection_id={twin_id}" in result
    assert "other.fakeuni.test" in result
    assert len(await jobs_of(env, user, "connect")) == 1  # nothing was started


async def test_a_title_that_a_kind_gives_to_any_source_does_not_find_a_source_of_another_site(
    env: Env,
) -> None:
    client, user = await student(env)
    chat = tools(env, user, await thread_of(client))
    await connect_source(chat, {**ASK, "title": "Расписание"})  # a schedule source of one site
    result = await connect_source(
        chat, {"title": "Расписание", "university_query": "Вышка", "kind_hint": "schedule"}
    )  # no address: a new source of whichever site the student will name
    assert result.startswith("Результат: запущено")
    assert await count(env, Connection, Connection.user_id == user) == 2


async def test_an_address_of_another_site_beside_a_connection_id_is_dropped_and_said(
    env: Env,
) -> None:
    chat, user, conn = await working_schedule(env)
    result = await connect_source(
        chat,
        {
            "connection_id": conn.id,
            "url": "https://other.fakeuni.test/week",
            "title": "Единый кабинет",
            "kinds": ["deadlines"],
        },
    )
    assert result.startswith("Результат: запущено")
    assert "указывает на другой сайт" in result
    assert "без connection_id" in result
    (_, extension) = await jobs_of(env, user, "connect")
    assert extension.payload["learning_intent"]["entry_urls"] == [PORTAL]
    assert "url" not in extension.payload


def test_what_an_extension_task_has_told_in_its_own_words_is_not_told_again_by_the_end() -> None:
    def ended(**result: Any) -> str | None:
        job = Job(kind="connect", state="done", payload={"extend": True}, result=result)
        return terminal_text(job)

    assert ended(status="error", detail="not_found", extension=True) is None
    assert ended(status="skipped", detail="active", extension=True) is None
    assert ended(status="ok", kinds=["deadlines"]) is None  # a connection ends with its own line
    # a task that failed without a word of its own still gets the sentence
    failed = Job(kind="connect", state="failed", payload={"extend": True}, error={}, result=None)
    assert "Подключённые данные работают как раньше" in (terminal_text(failed) or "")
    skipped = Job(
        kind="connect",
        state="done",
        payload={},
        result={"status": "skipped", "detail": "calendar"},
    )
    assert terminal_text(skipped) is None  # the thread holds the question about the calendar
