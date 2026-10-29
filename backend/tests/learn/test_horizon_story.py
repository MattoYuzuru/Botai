"""A source that shows a sliding week: research reads the neighbours, keeps the card, asks when thin.

The synthetic cabinet answers 7 days from `begin_date`. The week of a control work holds one class,
the weeks around it are full. Nothing here reaches a real university or a real model.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from botai.core.jobs import JobKind
from botai.core.llm import LlmResponse
from botai.db import models as m
from botai.learn.sitemap import get_site_map
from tests.fakeuni import data, slide
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.learn.test_probe import plan
from tests.sources.helpers import NOW

ORIGIN = data.ORIGIN
ENTRY = f"{ORIGIN}{slide.PAGE_PATH}?begin_date=2026-10-26"
WEEK_START = date(2026, 10, 26)  # the Monday of NOW (Tuesday 27.10)


def source_browser(window_json: str) -> FakeBrowserGateway:
    """What the student's browser shows on the cabinet: the page, its XHRs, the menu."""

    def xhr(path: str, sample: str, page: str = ENTRY) -> ObservedRequest:
        return ObservedRequest(
            method="GET",
            url=ORIGIN + path,
            status=200,
            resource_type="xhr",
            response_content_type="application/json",
            response_sample=sample,
            auth_proven=True,
            has_cookie=True,
            initiator_url=page,
        )

    pages = {
        ENTRY: FakePage(
            '<nav><a href="/schedule">Расписание</a><a href="/faq">Частые вопросы</a></nav>'
            "<main><h1>Моё расписание</h1></main>",
            "Расписание",
            requests=[
                xhr(f"{slide.API_PATH}?begin_date=2026-10-26", window_json),
                xhr(f"{slide.FAQ_PATH}?q=", "[]"),
            ],
        ),
        f"{ORIGIN}/": FakePage("<main>Главная</main>", "Кабинет"),
    }
    return FakeBrowserGateway(pages, NOW, ORIGIN)


def instant(clock: str) -> dict[str, Any]:
    return {
        "compose": {
            "type": "dateTime",
            "date": {"from": "day", "path": "/date"},
            "time": {"from": "lesson", "path": "/" + clock},
            "timeZone": "Europe/Moscow",
        },
        "required": True,
    }


def schedule_endpoint(*, window: bool) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "id": "schedule",
        "kind": "scheduleEvents",
        "request": {
            "method": "GET",
            "path": slide.API_PATH,
            "query": {"begin_date": "{weekStart}"},
        },
        "response": {
            "format": "json",
            "collections": [{"as": "day", "path": ""}, {"as": "lesson", "path": "/lessons"}],
            "fields": {
                "title": {"from": "lesson", "path": "/name", "required": True},
                "startsAt": instant("begin_time"),
                "endsAt": instant("end_time"),
                "location": {"from": "lesson", "path": "/auditorium"},
                "teacher": {"from": "lesson", "path": "/lecturer"},
                "kindText": {"from": "lesson", "path": "/lesson_type"},
                "address": {"from": "lesson", "path": "/building"},
                "group": {"from": "lesson", "path": "/parent_schedule"},
                "lessonNumber": {"from": "lesson", "path": "/lesson_number", "transform": "number"},
            },
            "links": [
                {
                    "url": {"from": "lesson", "path": "/url1"},
                    "label": {"from": "lesson", "path": "/url1_description"},
                }
            ],
            "attributes": [{"label": "Корпус", "from": "lesson", "path": "/building"}],
            "identity": ["title", "startsAt"],
        },
    }
    if window:
        endpoint["pagination"] = {
            "type": "dateWindow",
            "param": "begin_date",
            "stepDays": 7,
            "windows": 6,
            "startOffset": -1,
        }
    return endpoint


def author(*, window: bool) -> LlmResponse:
    return ls.answer([schedule_endpoint(window=window)])


async def rows(env: Any) -> list[m.ScheduleEvent]:
    env.db.expire_all()
    found = await env.db.scalars(
        select(m.ScheduleEvent)
        .where(m.ScheduleEvent.connection_id == env.seeded.connection_id)
        .order_by(m.ScheduleEvent.starts_at)
    )
    return list(found)


def week_json(begin: date) -> str:
    return json.dumps(slide.window(begin), ensure_ascii=False)


async def test_probe_dates_window_card_and_page_address_from_research_to_sync(
    make_env: EnvFactory,
) -> None:
    env = await make_env([])
    env.services.browser = source_browser(week_json(WEEK_START))
    # The planner looks at the page, repeats the observed read for the next week, then finishes.
    env.services.llm = ls.EvidenceLlm(
        queue=[author(window=True)],
        discovery_queue=[
            plan(1, "probe", "r0", overrides={"begin_date": "{plusDays:7}"}),
            plan(2, "finish", kinds=["scheduleEvents"], evidence=["r0", "r2"]),
        ],
    )
    result = await env.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert result["status"] == "ok"

    # 1. probe: the neighbouring week was really read, with the student's session
    probes = [d for d in slide.STATE.asked if d == "2026-11-03"]
    assert probes, slide.STATE.asked
    planner_view = json.loads(env.services.llm.observation_requests[1].messages[1].content)  # type: ignore[attr-defined]
    probed = planner_view["observation"]["probes"]
    assert [(p["from"], p["overrides"]) for p in probed] == [("r0", {"begin_date": "{plusDays:7}"})]
    assert probed[0]["objectsPerLevel"][-1] > 1  # the next week is full, the control week is not

    # 2. the recipe walks six windows, anchored on the week the page showed
    stored = await env.spec(1)
    endpoint = stored["recipe"]["endpoints"][0]
    assert stored["recipe"]["version"] == 2
    assert endpoint["pagination"] == {
        "type": "dateWindow",
        "param": "begin_date",
        "stepDays": 7,
        "windows": 6,
        "startOffset": -1,
    }
    assert endpoint["uiUrl"] == {"path": "/schedule", "query": {"begin_date": "{weekStart}"}}
    assert "requestEvidenceId" not in json.dumps(stored)

    # 3. sync: several weeks, not one class
    events = await rows(env)
    assert len(events) > 20
    assert {e.starts_at.date().isoformat() for e in events} >= {
        "2026-10-20",
        "2026-10-28",
        "2026-11-04",
    }

    # 4. the card keeps what the source says
    control = next(e for e in events if e.starts_at.date().isoformat() == "2026-10-28")
    assert control.kind_text == "Текущий рубежный контроль"
    assert control.address == slide.ADDRESS
    assert control.group_text == slide.GROUP
    assert control.lesson_number == 2
    assert control.attributes == [{"label": "Корпус", "value": slide.ADDRESS}]
    first = next(e for e in events if e.lesson_number == 1)
    assert first.links == [
        {
            "url": f"https://meet.fakeuni.test/room/{first.starts_at.date().isoformat()}-1",
            "label": "Онлайн-комната",
        }
    ]

    # 5. "open" leads to the page of that week, the API address stays internal provenance
    assert control.open_url == f"{ORIGIN}/schedule?begin_date=2026-10-26"
    assert control.source_url.startswith(f"{ORIGIN}{slide.API_PATH}?")
    later = next(e for e in events if e.starts_at.date().isoformat() == "2026-11-04")
    assert later.open_url == f"{ORIGIN}/schedule?begin_date=2026-11-02"

    # 6. the map of the source is kept for the agent: menu, routes and the FAQ endpoint
    sitemap = await get_site_map(env.db, ORIGIN)
    assert sitemap is not None
    assert {"label": "Частые вопросы", "path": "/faq"} in sitemap["navigation"]
    assert {"kind": "faq", "path": slide.FAQ_PATH, "queryKeys": ["q"]} in sitemap["endpoints"]


async def test_a_single_thin_window_is_widened_without_asking_anyone(
    make_env: EnvFactory,
) -> None:
    env = await make_env([author(window=False)])
    env.services.browser = source_browser(week_json(WEEK_START))
    result = await env.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert result["status"] == "ok"
    stored = await env.spec(1)
    assert stored["recipe"]["endpoints"][0]["pagination"]["type"] == "dateWindow"
    assert len(await rows(env)) > 20
    assert [type(b).__name__ for b in env.asked] == ["ConfirmBlock"]  # nobody was asked about weeks
    stages = [b.stage for b in ls.blocks_of(env.ctx, "thinking")]
    assert "Проверяю соседние недели" in stages
    assert len(env.llm.requests) == 1  # widening is not a model call


async def test_a_thin_schedule_in_every_window_is_asked_in_plain_words(
    make_env: EnvFactory,
) -> None:
    slide.STATE.sparse = True
    classifier = LlmResponse(
        content=json.dumps(
            {
                "linkIndices": [],
                "kinds": [],
                "absentKinds": [],
                "note": "Да, на этой неделе одна пара, это контрольная.",
            },
            ensure_ascii=False,
        )
    )
    env = await make_env([author(window=False), classifier])
    env.services.browser = source_browser(week_json(WEEK_START))
    result = await env.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents"]},
        links=["Да, на этой неделе одна пара, это контрольная."],
    )
    assert result["status"] == "ok"
    (question,) = [b for b in env.asked if b.type == "input"]
    assert question.question.startswith(
        "Вижу 1 занятие за 19.10–29.11. По неделям: 0, 1, 0, 0, 0, 0."
    )
    assert "ссылку на страницу" in question.question
    # the answer went through the ordinary intent classifier and only annotated the task
    assert [r.purpose for r in env.llm.requests] == ["learn", "classify"]
    assert len(await rows(env)) == 1


async def test_a_new_link_in_the_answer_sends_the_search_elsewhere(
    make_env: EnvFactory,
) -> None:
    slide.STATE.sparse = True
    other = f"{ORIGIN}/other-schedule?begin_date=2026-10-26"
    classifier = LlmResponse(
        content=json.dumps(
            {"linkIndices": [0], "kinds": [], "absentKinds": [], "note": "Расписание тут."},
            ensure_ascii=False,
        )
    )
    env = await make_env([author(window=False), classifier, author(window=True)])
    browser = source_browser(week_json(WEEK_START))
    browser._pages[other] = FakePage(  # pyright: ignore[reportPrivateUsage]
        "<main>Другое расписание</main>",
        "Расписание",
        requests=[
            ObservedRequest(
                method="GET",
                url=f"{ORIGIN}{slide.API_PATH}?begin_date=2026-10-26",
                status=200,
                resource_type="xhr",
                response_content_type="application/json",
                response_sample=week_json(WEEK_START),
                auth_proven=True,
                has_cookie=True,
                initiator_url=other,
            )
        ],
    )
    env.services.browser = browser
    result = await env.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents"]},
        links=[f"Расписание вот здесь: {other}"],
    )
    assert result["status"] == "ok"
    questions = [b for b in env.asked if b.type == "input"]
    assert len(questions) == 1  # the student is asked once, not again after the new search
    # author, the classification of the answer, and the author again on the new entry page
    assert [r.purpose for r in env.llm.requests] == ["learn", "classify", "learn"]
    stored = await env.spec(1)
    assert stored["recipe"]["endpoints"][0]["uiUrl"]["path"] == "/other-schedule"


async def test_repair_notices_a_horizon_of_one_window_and_widens_it_without_a_model(
    make_env: EnvFactory,
) -> None:
    from tests.sources import recipes

    single = recipes.recipe_spec([schedule_endpoint(window=False)], require=("scheduleEvents",))
    single["recipe"]["version"] = 2
    env = await make_env([], with_spec=True, spec=single)
    await env.login()
    assert (await env.sync())["status"] == "ok"
    assert len(await rows(env)) == 1  # the control week: one class, and nothing looks broken

    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    await env.rebrowse()
    result = await env.run(JobKind.REPAIR, {"reason": "chat"})

    assert result["status"] == "repaired"
    assert result["fixed"] == ["scheduleEvents"]
    assert env.llm.requests == []
    revised = await env.spec(2)
    assert revised["recipe"]["endpoints"][0]["pagination"]["windows"] == 6
    assert len(await rows(env)) > 20
    report = ls.blocks_of(env.ctx, "text")[-1].markdown
    assert "горизонт чтения: 6 окон по 7 дн." in report
