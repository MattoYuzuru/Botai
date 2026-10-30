"""Chat tools: formats, sources, honest empties, FTS, repair, tenant isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.env import ChatEnv
from botai.agent.harness import HarnessTool
from botai.agent.tools import TOOL_SPECS, build_chat_tools
from botai.core.jobs import JobKind
from botai.core.llm import ScriptedLlm
from botai.core.queue import InMemoryJobQueue
from botai.db.models import Connection, Job
from tests.agent.conftest import add_user
from tests.agent.seed import (
    FakeReader,
    add_connection,
    add_document,
    add_fact,
    add_space,
    deadline,
    event,
    provenance,
)
from tests.agent.support import NOW, add_thread, make_services

Sm = async_sessionmaker[AsyncSession]
MSK = 3  # hours ahead of UTC


class Rig:
    def __init__(
        self, env: ChatEnv, tools: dict[str, HarnessTool], queue: InMemoryJobQueue
    ) -> None:
        self.env, self.tools, self.queue = env, tools, queue

    async def call(self, name: str, **args: Any) -> str:
        return await self.tools[name].run(args)


async def rig(
    sm: Sm, user_id: str, *, space_id: str | None = None, reader: FakeReader | None = None
) -> Rig:
    queue = InMemoryJobQueue()
    services = make_services(ScriptedLlm(), sm=sm, queue=queue)
    thread = await add_thread(sm, user_id, space_id=space_id)
    env = ChatEnv(
        services=services,
        user_id=user_id,
        thread_id=thread,
        job_id=None,
        space_id=space_id,
        reader=reader or FakeReader(),
    )
    return Rig(env, {t.spec.name: t for t in build_chat_tools(env)}, queue)


def test_the_agent_has_exactly_these_tools() -> None:
    assert set(TOOL_SPECS) == {
        "get_schedule",
        "get_deadlines",
        "get_courses",
        "get_grades",
        "get_people",
        "get_news",
        "search_documents",
        "search_facts",
        "search_public_web",
        "request_repair",
        "request_login",
        "list_universities",
        "find_university",
        "connect_source",
        "add_document_url",
        "task_status",
        "sync_source",
        "read_document",
        "browse_source",
        "site_map",
        "fetch_public_page",
        "remember",
        "forget",
        "update_plan",
        "offer_choices",
        "rename_thread",
    }
    for spec in TOOL_SPECS.values():
        assert spec.parameters["additionalProperties"] is False
        # T5: a credential, cookie or code is never a parameter of any tool.
        assert not set(spec.parameters["properties"]) & {"password", "code", "cookie", "token"}
    # Only these four name an address; the others take ids of the student's own things.
    assert {n for n, s in TOOL_SPECS.items() if "url" in s.parameters["properties"]} == {
        "connect_source",
        "add_document_url",
        "browse_source",
        "fetch_public_page",
    }
    assert set(TOOL_SPECS["request_login"].parameters["properties"]) == {
        "connection_id",
        "on_screen",
    }


async def test_schedule_is_shown_in_the_space_time_zone_with_sources(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id, "Вуз А")
    conn = await add_connection(sm, user_id, space, "Расписание ЛК")
    prov = provenance(conn, space)
    reader = FakeReader(
        events=[
            event(prov, "Математический анализ", datetime(2026, 10, 29, 6, 30, tzinfo=UTC)),
            event(prov, "Базы данных", datetime(2026, 10, 29, 10, 0, tzinfo=UTC), changed=True),
            event(prov, "Не в диапазоне", datetime(2026, 11, 5, 6, 30, tzinfo=UTC)),
        ]
    )
    r = await rig(sm, user_id, reader=reader)
    text = await r.call("get_schedule", from_date="2026-10-29", to_date="2026-10-29")
    assert "Чт 29.10, 09:30–10:50 | Математический анализ" in text  # 06:30 UTC = 09:30 MSK
    assert "Чт 29.10, 13:00–14:20 | Базы данных" in text
    assert "ИЗМЕНЕНО" in text
    assert "Не в диапазоне" not in text
    assert text.count("[S1]") == 2
    ref = r.env.sources.refs["S1"]
    assert (ref.title, ref.url, ref.kind) == (
        "Расписание ЛК",
        "https://fakeuni.test/timetable?week=2026-10-26",  # the page, never the API (FR-042)
        "data",
    )
    assert ref.fetched_at == NOW


async def test_schedule_defaults_to_today_and_validates_dates(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    conn = await add_connection(sm, user_id, space)
    prov = provenance(conn, space)
    reader = FakeReader(events=[event(prov, "Сегодня", datetime(2026, 10, 28, 6, 0, tzinfo=UTC))])
    r = await rig(sm, user_id, reader=reader)
    assert "Сегодня" in await r.call("get_schedule")  # NOW is Wed 2026-10-28
    assert "YYYY-MM-DD" in await r.call("get_schedule", from_date="завтра")


async def test_empty_schedule_says_what_was_checked_and_cites_the_connection(
    sm: Sm, user_id: str
) -> None:
    space = await add_space(sm, user_id, "Вуз А")
    await add_connection(sm, user_id, space, "Расписание ЛК", last_sync_at=NOW - timedelta(hours=2))
    r = await rig(sm, user_id)
    text = await r.call("get_schedule", from_date="2026-10-29")
    assert "записей нет" in text
    assert "«Расписание ЛК»" in text
    assert "обновлено 2026-10-28 09:00" in text  # 06:00 UTC = 09:00 MSK
    assert r.env.sources.refs["S1"].kind == "connection"


async def test_no_connections_at_all_is_stated(sm: Sm, user_id: str) -> None:
    r = await rig(sm, user_id)
    assert "Подключённых источников нет" in await r.call("get_deadlines")


async def test_deadlines_are_sorted_and_overdue_is_flagged(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    conn = await add_connection(sm, user_id, space)
    prov = provenance(conn, space)
    reader = FakeReader(
        deadlines=[
            deadline(prov, "Поздняя", NOW + timedelta(days=5)),
            deadline(prov, "Просроченная", NOW - timedelta(days=1)),
            deadline(prov, "Ближайшая", NOW + timedelta(hours=30)),
            deadline(prov, "Сданная", NOW + timedelta(days=1), state="submitted"),
            deadline(prov, "Без срока", None),
        ]
    )
    r = await rig(sm, user_id, reader=reader)
    text = await r.call("get_deadlines")
    order = [text.index(t) for t in ("Просроченная", "Ближайшая", "Поздняя", "Без срока")]
    assert order == sorted(order)
    assert "ПРОСРОЧЕНО" in text
    assert "Сданная" not in text
    assert "Сданная" in await r.call("get_deadlines", include_done=True)


async def test_universities_list_has_ids_statuses_and_links(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id, "Вуз А")
    conn = await add_connection(
        sm, user_id, space, "Расписание ЛК", status="stale", error_code="recipe_drift"
    )
    r = await rig(sm, user_id)
    text = await r.call("list_universities")
    assert "«Вуз А»" in text
    assert f"connection_id={conn}" in text
    assert f"botai://connections/{conn}" in text
    assert "статус: данные устарели" in text
    assert "код ошибки: recipe_drift" in text


async def test_documents_are_found_by_russian_word_forms(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    await add_document(
        sm,
        user_id,
        space,
        "Положение об учёбе",
        [
            "Общие положения о порядке учебного процесса.",
            "Справка об обучении выдаётся в учебном офисе в течение трёх рабочих дней.",
        ],
    )
    r = await rig(sm, user_id)
    text = await r.call("search_documents", query="как получить справку об обучении")
    assert "учебном офисе" in text
    assert "[S1] «Положение об учёбе»" in text
    ref = r.env.sources.refs["S1"]
    assert (ref.kind, ref.url) == ("page", "https://fakeuni.test/docs/rules")


async def test_wordy_question_falls_back_to_any_word(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    await add_document(sm, user_id, space, "Правила", ["Пересдача экзамена проходит в сентябре."])
    r = await rig(sm, user_id)
    text = await r.call("search_documents", query="когда пересдача экзамена по матанализу")
    assert "в сентябре" in text  # "матанализу" is not in the text, the other words are


async def test_nothing_found_is_said_plainly(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    await add_document(sm, user_id, space, "Правила", ["Пересдача экзамена проходит в сентябре."])
    r = await rig(sm, user_id)
    assert "ничего не найдено" in await r.call("search_documents", query="общежитие заселение")
    assert "нужен непустой" in await r.call("search_documents", query="  ")


async def test_document_without_url_gets_an_internal_reference(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    doc = await add_document(
        sm, user_id, space, "Файл", ["Стипендия выплачивается раз в месяц."], url=None, kind="file"
    )
    r = await rig(sm, user_id)
    await r.call("search_documents", query="стипендия")
    ref = r.env.sources.refs["S1"]
    assert (ref.url, ref.kind) == (f"botai://documents/{doc}", "document")


async def test_search_is_tenant_and_space_scoped(sm: Sm, user_id: str) -> None:
    other = await add_user(sm, "other")
    mine = await add_space(sm, user_id, "Мой вуз")
    mine2 = await add_space(sm, user_id, "Второй вуз")
    theirs = await add_space(sm, other, "Чужой")
    await add_document(sm, other, theirs, "Чужой документ", ["Секретная стипендия соседа."])
    await add_document(sm, user_id, mine, "Мой", ["Моя стипендия."])
    await add_document(sm, user_id, mine2, "Второй", ["Стипендия во втором вузе."])
    await add_fact(sm, other, "стипендия?", "чужой факт про стипендию")

    everywhere = await rig(sm, user_id)
    text = await everywhere.call("search_documents", query="стипендия")
    assert "Моя стипендия" in text
    assert "втором вузе" in text
    assert "соседа" not in text
    assert "чужой факт" not in await everywhere.call("search_facts", query="стипендия")

    scoped = await rig(sm, user_id, space_id=mine)
    scoped_text = await scoped.call("search_documents", query="стипендия")
    assert "Моя стипендия" in scoped_text
    assert "втором вузе" not in scoped_text


async def test_facts_are_returned_with_freshness(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    await add_fact(
        sm,
        user_id,
        "Как получить справку?",
        "В учебном офисе за три дня.",
        space_id=space,
        confirmed_at=NOW - timedelta(days=5),
    )
    await add_fact(
        sm,
        user_id,
        "Куда сдавать заявление на пересдачу?",
        "В деканат до пятницы.",
        space_id=space,
        confirmed_at=NOW - timedelta(days=60),
    )
    r = await rig(sm, user_id)
    fresh = await r.call("search_facts", query="справку")
    assert "три дня" in fresh
    assert "актуален" in fresh
    old = await r.call("search_facts", query="заявление пересдачу")
    assert "УСТАРЕЛ" in old
    assert r.env.sources.refs["S2"].stale
    assert "Сохранённых подтверждённых ответов" in await r.call("search_facts", query="общежитие")


async def test_repair_enqueues_once_and_refuses_what_a_human_must_do(sm: Sm, user_id: str) -> None:
    other = await add_user(sm, "other")
    space = await add_space(sm, user_id)
    foreign_space = await add_space(sm, other)
    broken = await add_connection(
        sm, user_id, space, "Расписание", status="error", error_code="recipe_drift"
    )
    async with sm() as session:
        connection = await session.get(Connection, broken)
        assert connection is not None
        connection.adapter = "recipe"  # This case repairs an existing recipe, not unfinished setup.
        await session.commit()
    reauth = await add_connection(sm, user_id, space, "ЛК", status="needs_reauth")
    foreign = await add_connection(sm, other, foreign_space, "Чужое", status="error")
    r = await rig(sm, user_id)

    done = await r.call("request_repair", connection_id=broken, reason="расписание пустое")
    assert "поставлена в очередь" in done
    (job,) = r.queue.jobs
    assert job["kind"] == JobKind.REPAIR
    assert (job["user_id"], job["connection_id"], job["thread_id"]) == (
        user_id,
        broken,
        r.env.thread_id,
    )
    assert job["payload"] == {"reason": "расписание пустое", "source": "chat"}
    assert r.env.repairs == ["Расписание"]

    assert "нужен повторный вход" in await r.call(
        "request_repair", connection_id=reauth, reason="x"
    )
    assert "Такого подключения" in await r.call("request_repair", connection_id=foreign, reason="x")
    assert "Такого подключения" in await r.call("request_repair", connection_id="nope", reason="x")
    assert len(r.queue.jobs) == 1


async def test_actions_reach_the_sources_of_every_university_from_any_thread(
    sm: Sm, user_id: str
) -> None:
    """A thread is not a fence: the student in a chat about one university can ask to update or
    repair a source of another, and the task starts in the chat they are in."""
    here = await add_space(sm, user_id, "Первый вуз")
    there = await add_space(sm, user_id, "Второй вуз")
    far = await add_connection(sm, user_id, there, "Расписание", status="error")
    stale = await add_connection(sm, user_id, there, "Сайт кафедры", status="stale")
    async with sm() as session:
        for conn_id in (far, stale):
            row = await session.get(Connection, conn_id)
            assert row is not None
            row.adapter = "recipe"
        await session.commit()
    r = await rig(sm, user_id, space_id=here)
    assert "запущено" in await r.call("sync_source", connection_id=stale)
    assert "поставлена в очередь" in await r.call(
        "request_repair", connection_id=far, reason="пусто"
    )
    assert [(j["kind"], j["thread_id"]) for j in r.queue.jobs] == [
        (JobKind.SYNC, r.env.thread_id),
        (JobKind.REPAIR, r.env.thread_id),
    ]
    listing = await r.call("list_universities")
    assert "Первый вуз" in listing
    assert "Второй вуз" in listing
    assert f"connection_id={far}" in listing


@pytest.mark.parametrize(
    "reading",
    [
        ("get_news", {}),
        ("get_schedule", {}),
        ("get_deadlines", {}),
        ("get_courses", {}),
        ("search_documents", {"query": "пересдача"}),
        ("search_facts", {"query": "общежитие"}),
        ("search_public_web", {"query": "пересдача"}),
    ],
    ids=lambda case: case[0],
)
async def test_after_any_text_from_outside_only_the_students_words_are_remembered(
    sm: Sm, user_id: str, reading: tuple[str, dict[str, Any]]
) -> None:
    """A teacher writes the news and the timetable, a document or a hit says what it likes: what
    these tools returned is not a fact about the student, whatever the model makes of it."""
    r = await rig(sm, user_id)
    assert (
        await r.call("remember", note="Староста группы Иванов") == "Запомнено."
    )  # nothing read yet
    name, args = reading
    await r.call(name, **args)
    refused = await r.call("remember", note="Староста потока Петров")
    assert refused.startswith("Не запомнено")
    assert r.env.outside_read


async def test_repair_does_not_start_a_second_job_while_one_runs(sm: Sm, user_id: str) -> None:
    space = await add_space(sm, user_id)
    conn = await add_connection(sm, user_id, space, "Расписание", status="stale")
    async with sm() as session:
        session.add(
            Job(
                user_id=user_id,
                kind="repair",
                state="running",
                connection_id=conn,
                run_after=NOW,
            )
        )
        await session.commit()
    r = await rig(sm, user_id)
    text = await r.call("request_repair", connection_id=conn, reason="не обновляется")
    assert "уже идёт" in text
    assert r.queue.jobs == []
    async with sm() as session:
        assert len((await session.execute(select(Job))).scalars().all()) == 1


async def test_bad_arguments_become_text_not_exceptions(sm: Sm, user_id: str) -> None:
    r = await rig(sm, user_id)
    assert "Ошибка аргументов" in await r.call("get_news", limit="много")


async def test_schedule_gives_the_whole_card_and_cites_the_page_not_the_api(
    sm: Sm, user_id: str
) -> None:
    space = await add_space(sm, user_id)
    conn = await add_connection(sm, user_id, space)
    prov = provenance(conn, space).model_copy(update={"open_url": None})
    lesson = event(prov, "Программная инфраструктура", datetime(2026, 10, 29, 13, 20, tzinfo=UTC))
    lesson = lesson.model_copy(
        update={
            "kind_text": "Лекция",
            "address": "Синтетический пр., 1",
            "group": "Поток А-21",
            "lesson_number": 3,
        }
    )
    r = await rig(sm, user_id, reader=FakeReader(events=[lesson]))
    text = await r.call("get_schedule", from_date="2026-10-29", to_date="2026-10-29")
    for piece in ("пара 3", "Лекция", "адрес: Синтетический пр., 1", "группа/поток: Поток А-21"):
        assert piece in text
    assert r.env.sources.refs["S1"].url == f"botai://connections/{conn}"
