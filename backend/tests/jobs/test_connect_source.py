"""The agent connects sources in its own thread (FR-035..038): real database, real queue."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

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
from botai.db.models import Connection, Job, Space, University
from tests.jobs.env import Env

pytestmark = pytest.mark.db
LMS = "https://smart.fakeuni.test/courses?tab=current"
PORTAL = "https://lk.fakeuni.test/schedule"
SHEET = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"
HSE = "vysshaya-shkola-ekonomiki"


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
