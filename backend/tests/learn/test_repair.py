"""Self-repair end to end: the university changes something, the agent re-learns the spec."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from botai.contracts.agent_ui import ConfirmBlock
from botai.core.jobs import JobKind
from botai.db import models as m
from botai.learn.repair import endpoint_changes
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import data
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.learn.env import Env
from tests.sources import recipes
from tests.sources.helpers import NOW


def moved_timetable() -> dict[str, Any]:
    return recipes.timetable_endpoint(path="/api/v3/schedule")


def news_v2() -> dict[str, Any]:
    return {
        "id": "news",
        "kind": "news",
        "request": {"method": "GET", "path": "/news"},
        "response": {
            "format": "html",
            "itemsSelector": "article.post",
            "fields": {
                "title": {"selector": "h3 a", "required": True},
                "url": {"selector": "h3 a", "attr": "href"},
                "publishedAt": {
                    "selector": ".when",
                    "attr": "data-iso",
                    "transform": "iso",
                    "required": True,
                },
            },
            "identity": ["url"],
        },
    }


async def healthy(
    make_env: EnvFactory, responses: list[Any], spec: dict[str, Any] | None = None
) -> Env:
    """A connection that has synced once, so there is a confirmed snapshot to compare with."""
    env = await make_env(responses, with_spec=True, spec=spec)
    await env.login()
    assert (await env.sync())["status"] == "ok"
    return env


async def break_and_detect(env: Env) -> dict[str, Any]:
    """The sync that notices the change and queues the repair job."""
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    outcome = await env.sync()
    assert outcome["status"] == "repairing"
    (job,) = env.repair_jobs()
    await env.rebrowse()
    return job["payload"]


async def test_a_repair_the_student_stops_leaves_the_connection_stale(
    make_env: EnvFactory,
) -> None:
    """Stopping a job cancels its task (CancelledError, not JobCancelled). The connection used to
    stay `repairing` for good, with no job working on it (found by the full-stack test, W7)."""
    env = await healthy(make_env, [])
    ctx = ls.StoppedByStudent(user_id=env.seeded.user_id, connection_id=env.seeded.connection_id)
    with pytest.raises(asyncio.CancelledError):
        await env.handlers[JobKind.REPAIR](ctx, env.services, {"reason": "drift"})
    conn = await env.conn()
    assert conn.status == "stale"
    assert conn.next_sync_at is not None  # the scheduler will look at it again


# --- the main story -------------------------------------------------------------------------


async def test_a_moved_endpoint_is_found_fixed_and_reported(make_env: EnvFactory) -> None:
    env = await healthy(make_env, [ls.answer([moved_timetable()])])
    events_before = await env.count(m.ScheduleEvent)
    fakeuni_api.STATE.timetable_version = "v3"
    payload = await break_and_detect(env)
    assert payload["reason"] == "drift"
    assert payload["problems"] == [
        {"endpoint": "timetable", "kind": "scheduleEvents", "problem": "not_found"}
    ]
    assert (await env.conn()).status == "repairing"  # the old data stay on screen meanwhile

    result = await env.run(JobKind.REPAIR, payload)

    assert result["status"] == "repaired"
    assert result["fixed"] == ["scheduleEvents"]
    assert await env.specs() == [(1, "deprecated"), (2, "active")]
    new = await env.spec(2)
    paths = {e["kind"]: e["request"]["path"] for e in new["recipe"]["endpoints"]}
    assert paths == {"scheduleEvents": "/api/v3/schedule", "deadlines": "/api/v2/tasks/deadlines"}
    assert new["recipe"]["revision"] == 2
    assert new["recipe"]["learnedByDigest"]
    assert new["recipe"]["endpoints"][0]["id"] == "timetable"  # ids survive a repair

    conn = await env.conn()
    assert (conn.status, conn.spec_revision, conn.error_code) == ("active", 2, None)
    assert await env.count(m.ScheduleEvent) == events_before  # same rows, not duplicates
    revs = await env.rows_revision()
    assert revs == {2}

    # one model call, a repair one, told what used to work and what is wrong
    assert [r.purpose for r in env.llm.requests] == ["repair"]
    prompt = env.llm.requests[0].messages[1].content
    assert '"path": "/api/v2/timetable"' in prompt
    assert "адрес больше не отвечает" in prompt
    assert "<observation" in prompt
    sent = "\n".join(msg.content for r in env.llm.requests for msg in r.messages)
    for secret in (data.PASSWORD, data.USERNAME, "Иванова", "u-1001"):
        assert secret not in sent

    # nothing to confirm: the data are the ones the student already had
    assert env.asked == []
    report = ls.blocks_of(env.ctx, "text")[-1].markdown
    assert "Исправлено" in report
    assert "адрес запроса: `/api/v2/timetable` → `/api/v3/schedule`" in report
    assert "Данные на месте — расписание: 4" in report
    stages = [b.stage for b in ls.blocks_of(env.ctx, "thinking")]
    assert "Чиню: расписание" in stages
    assert (await env.sync())["status"] == "ok"  # the next scheduled sync runs on revision 2


async def test_a_markup_change_is_repaired_with_new_selectors(make_env: EnvFactory) -> None:
    spec = recipes.recipe_spec([recipes.news_endpoint()], require=("news",))
    env = await healthy(make_env, [ls.answer([news_v2()])], spec)
    ids_before = await env.ids(m.NewsItem)
    fakeuni_api.STATE.news_markup = "v2"
    payload = await break_and_detect(env)

    result = await env.run(JobKind.REPAIR, payload)

    assert result["status"] == "repaired"
    new = await env.spec(2)
    assert new["recipe"]["endpoints"][0]["response"]["itemsSelector"] == "article.post"
    assert await env.ids(m.NewsItem) == ids_before  # identity by url: updated in place
    report = ls.blocks_of(env.ctx, "text")[-1].markdown
    assert "где лежит список: `li.news-item` → `article.post`" in report
    assert "поля ответа: «url», «title», «publishedAt»" in report


async def test_a_daily_check_that_finds_a_break_repairs_it(make_env: EnvFactory) -> None:
    env = await healthy(make_env, [ls.answer([moved_timetable()])])
    fakeuni_api.STATE.timetable_version = "v3"  # nobody has synced since: only the check notices
    await env.rebrowse()
    result = await env.run(JobKind.REPAIR, {"mode": "check", "reason": "canary"})
    assert result["status"] == "repaired"
    assert await env.specs() == [(1, "deprecated"), (2, "active")]


async def test_a_healthy_daily_check_is_silent(make_env: EnvFactory) -> None:
    env = await healthy(make_env, [])
    result = await env.run(JobKind.REPAIR, {"mode": "check", "reason": "canary"})
    assert result == {"status": "healthy", "mode": "check"}
    assert env.ctx.blocks == []  # no chatter for a check that found nothing
    assert env.llm.requests == []
    assert (await env.conn()).status == "active"
    checks = await env.audit("canary")
    assert checks[-1].detail["via"] == "check"
    assert checks[-1].detail["ok"] is True


async def test_a_complaint_about_a_healthy_source_gets_an_honest_answer(
    make_env: EnvFactory,
) -> None:
    env = await healthy(make_env, [])
    result = await env.run(JobKind.REPAIR, {})  # as the chat queues it
    assert result["status"] == "healthy"
    assert env.llm.requests == []
    text = ls.blocks_of(env.ctx, "text")[0].markdown
    assert "сайт отвечает так же, как раньше" in text
    assert "расписание: 4" in text


# --- when there is nothing to fix, or no fix -------------------------------------------------


async def test_an_empty_source_is_not_a_break(
    make_env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await healthy(make_env, [ls.nothing()])
    monkeypatch.setattr(data, "deadlines", lambda: [])  # a quiet period: no deadlines at all
    payload = await break_and_detect(env)
    assert payload["problems"][0]["problem"] == "empty"

    result = await env.run(JobKind.REPAIR, payload)

    assert result["status"] == "ok"
    assert result["empty"] == ["deadlines"]
    assert await env.specs() == [(1, "active")]  # the spec was right
    assert "записей сейчас нет: дедлайны" in ls.blocks_of(env.ctx, "text")[-1].markdown
    assert (await env.conn()).status == "active"
    # the empty answer is the truth now: the next sync does not raise the alarm again
    again = await env.sync()
    assert again["status"] == "ok"
    assert len(env.repair_jobs()) == 1


async def test_nothing_better_found_keeps_the_data_and_tries_again_tomorrow(
    make_env: EnvFactory,
) -> None:
    env = await healthy(make_env, [ls.nothing()])
    fakeuni_api.STATE.timetable_version = "v3"
    payload = await break_and_detect(env)
    events = await env.count(m.ScheduleEvent)

    result = await env.run(JobKind.REPAIR, payload)

    assert result["status"] == "unresolved"
    assert result["kinds"] == ["scheduleEvents"]
    assert await env.specs() == [(1, "active")]
    assert await env.count(m.ScheduleEvent) == events  # nothing was lost
    conn = await env.conn()
    assert conn.status == "stale"
    assert conn.error_code == "recipe_drift"
    assert conn.next_sync_at == NOW + timedelta(hours=3, days=1)
    assert (
        "подобрать новое описание не удалось: расписание"
        in ls.blocks_of(env.ctx, "text")[-1].markdown
    )


async def test_a_candidate_reading_other_data_goes_to_the_student(make_env: EnvFactory) -> None:
    wrong = moved_timetable()
    wrong["response"]["fields"]["title"] = {"path": "/place/room", "required": True}
    env = await healthy(make_env, [ls.answer([wrong]), ls.answer([wrong])])
    fakeuni_api.STATE.timetable_version = "v3"
    payload = await break_and_detect(env)

    declined = await env.run(JobKind.REPAIR, payload, confirm=False)

    assert declined["status"] == "unresolved"
    assert any(isinstance(b, ConfirmBlock) for b in env.asked)
    assert ls.blocks_of(env.ctx, "summary")[0].title == "Вот что читается теперь"
    assert await env.specs() == [(1, "active")]

    env.services.clock.advance(timedelta(days=1))  # type: ignore[attr-defined]
    await env.rebrowse()
    accepted = await env.run(JobKind.REPAIR, payload, confirm=True)
    assert accepted["status"] == "repaired"
    assert await env.specs() == [(1, "deprecated"), (2, "active")]


async def test_a_dead_login_during_repair_asks_the_student_to_log_in(
    make_env: EnvFactory,
) -> None:
    env = await healthy(make_env, [])
    fakeuni_api.expire_sessions()
    env.sessions.works = False
    result = await env.run(JobKind.REPAIR, {})
    assert result["status"] == "needs_reauth"
    assert "войти заново" in ls.blocks_of(env.ctx, "text")[0].markdown


async def test_the_repaired_spec_passes_the_same_lint_and_hides_nothing_personal(
    make_env: EnvFactory,
) -> None:
    leaky = moved_timetable()
    leaky["response"]["fields"]["location"] = {"path": "/place/anna@fakeuni.test"}
    env = await healthy(make_env, [ls.answer([leaky]), ls.answer([moved_timetable()])])
    fakeuni_api.STATE.timetable_version = "v3"
    payload = await break_and_detect(env)
    result = await env.run(JobKind.REPAIR, payload)
    assert result["status"] == "repaired"
    assert "personal.email" in env.llm.requests[1].messages[1].content
    assert "anna@" not in json.dumps(await env.spec(2))


async def test_other_students_of_the_university_get_the_repaired_spec(
    make_env: EnvFactory,
) -> None:
    first = await healthy(make_env, [ls.answer([moved_timetable()])])
    fakeuni_api.STATE.timetable_version = "v3"
    payload = await break_and_detect(first)
    await first.run(JobKind.REPAIR, payload)

    # a second student of the university, with no spec of their own yet
    second = await make_env([], adapter="recipe", status="active")
    await second.login()
    result = await second.sync()
    assert result["status"] == "ok"  # revision 2 serves them with no model call
    assert (await second.conn()).spec_revision == 2
    assert second.llm.requests == []


# --- reports --------------------------------------------------------------------------------


def test_endpoint_changes_are_described_without_values() -> None:
    old = recipes.timetable_endpoint()
    new = copy.deepcopy(old)
    new["request"]["path"] = "/api/v3/schedule"
    new["request"]["query"]["from"] = "{minusDays:1}"
    new["response"]["fields"]["title"] = {"path": "/name"}
    new["response"]["fields"]["teacher"] = {"path": "/t"}
    assert endpoint_changes(old, new) == [
        "адрес запроса: `/api/v2/timetable` → `/api/v3/schedule`",
        "параметры запроса",
        "поля ответа: «title»",
        "добавлены поля: «teacher»",
    ]
    html = recipes.news_endpoint()
    assert endpoint_changes(old, html)[:2] == [
        "адрес запроса: `/api/v2/timetable` → `/news`",
        "параметры запроса",
    ]
    assert "формат ответа: JSON → HTML" in endpoint_changes(old, html)
    assert endpoint_changes(old, copy.deepcopy(old)) == []


async def test_free_text_from_the_chat_never_reaches_the_shared_notes(make_env: EnvFactory) -> None:
    env = await healthy(make_env, [ls.answer([moved_timetable()])])
    fakeuni_api.STATE.timetable_version = "v3"
    await env.rebrowse()
    payload = {"reason": "расписание не показывается, мой номер 8 999 123 45 67", "source": "chat"}
    result = await env.run(JobKind.REPAIR, payload)
    assert result["status"] == "repaired"
    notes = (
        await env.db.execute(select(m.AdapterSpecRow.notes).where(m.AdapterSpecRow.revision == 2))
    ).scalar_one()
    assert notes == "repair: chat"
