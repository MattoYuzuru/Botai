"""The chat tools over W1's real domain repository (`botai.domain.repo`), not a fake."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from botai.agent.bench import BENCH_DIR
from botai.agent.bench_data import seed
from botai.agent.reader import RepoReader
from botai.agent.tools import ChatEnv, build_chat_tools
from botai.core.clock import FrozenClock
from botai.core.llm import ScriptedLlm
from tests.agent.conftest import Sm
from tests.agent.support import add_thread, make_services


@pytest.fixture
async def env(sm: Sm) -> ChatEnv:
    data = json.loads((BENCH_DIR / "scenarios" / "b4.json").read_text("utf-8"))
    seeded = await seed(sm, data)
    thread = await add_thread(sm, seeded.user_id, space_id=seeded.space_id)
    services = make_services(ScriptedLlm(), sm=sm)
    services.clock = FrozenClock(datetime.fromisoformat(data["now"]))  # Wednesday 2026-10-28
    return ChatEnv(
        services=services,
        user_id=seeded.user_id,
        thread_id=thread,
        job_id=None,
        space_id=seeded.space_id,
        reader=RepoReader(),
    )


async def call(env: ChatEnv, name: str, **args: object) -> str:
    tools = {t.spec.name: t for t in build_chat_tools(env)}
    return await tools[name].run(dict(args))


async def test_schedule_through_the_repository(env: ChatEnv) -> None:
    text = await call(env, "get_schedule", from_date="2026-10-29", to_date="2026-10-29")
    assert "Чт 29.10, 09:30–10:50 | Математический анализ" in text
    assert "ауд./место: А-101" in text
    assert "Чт 29.10, 15:10–16:30 | Английский язык" in text
    assert "Иванов И.И." in text
    assert "данные устарели" not in text  # synced half an hour before "now"
    assert env.sources.refs["S1"].title == "Расписание ЛК"


async def test_deadlines_hide_submitted_work_and_stale_sources_are_marked(env: ChatEnv) -> None:
    text = await call(env, "get_deadlines")
    assert text.index("ДЗ 3: деревья отрезков") < text.index("Лабораторная 2: индексы")
    assert "ДЗ 2: сортировки" not in text
    assert "Алгоритмы и структуры данных" in text  # the course title is resolved
    news = await call(env, "get_news")
    assert "Перенос занятий 4 ноября" in news
    assert "[S" in news
    refs = {r.title: r for r in env.sources.refs.values()}
    assert refs["Новости факультета"].stale is True  # that connection is `stale`
    assert refs["Курсы и задания"].stale is False


async def test_grades_people_and_courses(env: ChatEnv) -> None:
    grades = await call(env, "get_grades", course="базы")
    assert "Контрольная 1" in grades
    assert "оценка 8 по шкале 10" in grades
    assert "Записей нет" not in await call(env, "get_people", query="иванов")
    assert "записей нет" in await call(env, "get_grades", course="алгоритм")
    assert "Математический анализ" in await call(env, "get_courses")
