"""The event card: source wording, address, group, lesson number, links, attributes, open_url."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db import models as m
from botai.domain import repo
from botai.sources import normalize
from botai.sources.adapter import AdapterResult, EndpointOutcome, Record
from tests.sources.helpers import NOW
from tests.sources.support import seed_connection

MSK = ZoneInfo("Europe/Moscow")
BASE: dict[str, Any] = {
    "title": "Теория вероятностей",
    "startsAt": "2026-11-02T09:30:00+03:00",
    "endsAt": "2026-11-02T10:50:00+03:00",
}


def event(fields: dict[str, Any], *, open_url: str | None = None) -> Record:
    return Record(
        "scheduleEvents",
        "ev-1",
        {**BASE, **fields},
        "https://fakeuni.test/api/get-myschedule?begin_date=2026-11-02",
        open_url,
    )


def test_card_fields_are_cleaned_and_bounded() -> None:
    row = normalize.coerce(
        "scheduleEvents",
        event(
            {
                "kindText": " Контрольное мероприятие ",
                "address": "Покровский б-р, д.11",
                "group": "Группа А-21 курс 3",
                "lessonNumber": "3",
                "links": [
                    {"url": "https://meet.fakeuni.test/r/1", "label": "Комната"},
                    {"url": "https://meet.fakeuni.test/r/1", "label": "Дубль"},
                    {"url": "javascript:alert(1)", "label": "Опасная"},
                    {"url": "/lectures/7", "label": ""},
                ],
                "attributes": [
                    {"label": "Корпус", "value": "Покровка"},
                    {"label": "Пусто", "value": "  "},
                    "not a mapping",
                ],
            }
        ),
        MSK,
    )
    assert row is not None
    assert row["kind_text"] == "Контрольное мероприятие"
    assert row["address"] == "Покровский б-р, д.11"
    assert row["group_text"] == "Группа А-21 курс 3"
    assert row["lesson_number"] == 3
    assert row["links"] == [
        {"url": "https://meet.fakeuni.test/r/1", "label": "Комната"},
        {"url": "https://fakeuni.test/lectures/7", "label": ""},
    ]
    assert row["attributes"] == [{"label": "Корпус", "value": "Покровка"}]


def test_a_missing_card_is_an_ordinary_event() -> None:
    row = normalize.coerce("scheduleEvents", event({}), MSK)
    assert row is not None
    assert (row["kind_text"], row["address"], row["group_text"], row["lesson_number"]) == (
        None,
        None,
        None,
        None,
    )
    assert row["links"] == []
    assert row["attributes"] == []


async def test_open_url_is_stored_updated_and_served_next_to_the_api_address(
    db: AsyncSession,
) -> None:
    seeded = await seed_connection(db)
    kwargs: dict[str, Any] = {
        "user_id": seeded.user_id,
        "connection_id": seeded.connection_id,
        "tz": MSK,
    }
    page = "/schedule?begin_date=2026-11-02"
    first = AdapterResult(
        [event({"group": "Группа Б-1"}, open_url="https://fakeuni.test" + page)],
        NOW,
        [EndpointOutcome("s", "scheduleEvents", ok=True)],
    )
    await normalize.apply_result(db, result=first, now=NOW, **kwargs)
    row = (await db.execute(select(m.ScheduleEvent))).scalar_one()
    assert row.open_url == "https://fakeuni.test" + page
    assert row.source_url.startswith("https://fakeuni.test/api/")
    assert row.group_text == "Группа Б-1"

    later = NOW + timedelta(hours=3)
    second = AdapterResult(
        [
            event(
                {"group": "Группа Б-2"},
                open_url="https://fakeuni.test/schedule?begin_date=2026-11-02",
            )
        ],
        later,
        [EndpointOutcome("s", "scheduleEvents", ok=True)],
    )
    stats = await normalize.apply_result(db, result=second, now=later, **kwargs)
    assert stats["scheduleEvents"].updated == 1
    db.expire_all()
    row = (await db.execute(select(m.ScheduleEvent))).scalar_one()
    assert row.group_text == "Группа Б-2"

    served = await repo.list_events(
        db,
        seeded.user_id,
        start=NOW,
        end=NOW + timedelta(days=30),
        now=NOW,
    )
    assert len(served) == 1
    out = served[0]
    assert out.provenance.open_url == "https://fakeuni.test" + page
    assert out.provenance.source_url.startswith("https://fakeuni.test/api/")
    assert out.group == "Группа Б-2"
    assert out.links == []
    assert out.attributes == []


async def test_a_record_without_a_page_has_no_open_url(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    result = AdapterResult([event({})], NOW, [EndpointOutcome("s", "scheduleEvents", ok=True)])
    await normalize.apply_result(
        db,
        user_id=seeded.user_id,
        connection_id=seeded.connection_id,
        result=result,
        now=NOW,
        tz=MSK,
    )
    row = (await db.execute(select(m.ScheduleEvent))).scalar_one()
    assert row.open_url is None


async def test_an_html_page_is_its_own_open_url_and_an_unreadable_parameter_gives_none() -> None:
    from botai.contracts.specs import RecipeSpec
    from botai.sources.adapters.recipe import RecipeAdapter
    from tests.sources import recipes
    from tests.sources.helpers import make_ctx

    html = "<ul><li class='news-item'><a href='/n/1'>Новость</a><time datetime='2026-10-27T10:00:00Z'>x</time></li></ul>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    spec = RecipeSpec.model_validate(
        recipes.recipe_spec([recipes.news_endpoint()], require=("news",))
    )
    result = await RecipeAdapter(spec).fetch(make_ctx(transport=httpx.MockTransport(handler)))
    assert [r.open_url for r in result.records] == ["https://fakeuni.test/news"]

    # a declared page address that needs a value the source did not give is left out
    data = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    data["recipe"]["endpoints"][0]["uiUrl"] = {"path": "/students/{studentId}/schedule"}
    data["recipe"]["bootstrap"] = [
        {"id": "me", "method": "GET", "path": "/api/v2/me", "extract": {"studentId": "/missing"}}
    ]

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/me":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"items": data_timetable()})

    from tests.fakeuni import data as fakeuni_data

    def data_timetable() -> list[dict[str, object]]:
        return fakeuni_data.timetable()

    result = await RecipeAdapter(RecipeSpec.model_validate(data)).fetch(
        make_ctx(transport=httpx.MockTransport(api))
    )
    assert result.records
    assert {r.open_url for r in result.records} == {None}


async def test_an_address_learned_later_is_not_a_change_but_a_different_one_is(
    db: AsyncSession,
) -> None:
    seeded = await seed_connection(db)
    kwargs: dict[str, Any] = {
        "user_id": seeded.user_id,
        "connection_id": seeded.connection_id,
        "tz": MSK,
    }

    async def sync(fields: dict[str, Any], at_hours: int) -> bool:
        result = AdapterResult(
            [event(fields)],
            NOW + timedelta(hours=at_hours),
            [EndpointOutcome("s", "scheduleEvents", ok=True)],
        )
        await normalize.apply_result(
            db, result=result, now=NOW + timedelta(hours=at_hours), **kwargs
        )
        db.expire_all()
        return (await db.execute(select(m.ScheduleEvent))).scalar_one().changed

    assert await sync({}, 0) is False
    assert await sync({"address": "ул. Учебная, д. 5"}, 1) is False  # new field, same class
    assert await sync({"address": "ул. Другая, д. 7"}, 2) is True  # the class moved
