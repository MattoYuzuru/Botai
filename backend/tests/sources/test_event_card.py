"""The event card: source wording, address, group, lesson number, links, attributes, open_url."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
                "kindText": " Текущий рубежный контроль ",
                "address": "Покровский б-р, д.11",
                "group": "ДРИП 3 курс 26/27",
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
    assert row["kind_text"] == "Текущий рубежный контроль"
    assert row["address"] == "Покровский б-р, д.11"
    assert row["group_text"] == "ДРИП 3 курс 26/27"
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
        [event({"group": "ДРИП"}, open_url="https://fakeuni.test" + page)],
        NOW,
        [EndpointOutcome("s", "scheduleEvents", ok=True)],
    )
    await normalize.apply_result(db, result=first, now=NOW, **kwargs)
    row = (await db.execute(select(m.ScheduleEvent))).scalar_one()
    assert row.open_url == "https://fakeuni.test" + page
    assert row.source_url.startswith("https://fakeuni.test/api/")
    assert row.group_text == "ДРИП"

    later = NOW + timedelta(hours=3)
    second = AdapterResult(
        [
            event(
                {"group": "ДРИП-2"}, open_url="https://fakeuni.test/schedule?begin_date=2026-11-02"
            )
        ],
        later,
        [EndpointOutcome("s", "scheduleEvents", ok=True)],
    )
    stats = await normalize.apply_result(db, result=second, now=later, **kwargs)
    assert stats["scheduleEvents"].updated == 1
    db.expire_all()
    row = (await db.execute(select(m.ScheduleEvent))).scalar_one()
    assert row.group_text == "ДРИП-2"

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
    assert out.group == "ДРИП-2"
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
