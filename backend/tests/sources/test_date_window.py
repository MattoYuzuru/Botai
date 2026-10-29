"""A source that answers a sliding window of days from a date is read window by window."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from botai.contracts.schemas import errors
from botai.contracts.specs import Recipe, RecipeSpec
from botai.sources import templates
from botai.sources.adapters.recipe import RecipeAdapter
from tests.sources import recipes
from tests.sources.helpers import NOW, make_ctx

PER_WEEK = {date(2026, 10, 26): 1}  # the week of a control work: one class; later weeks are full
WINDOW_DAYS = 7


def lesson(index: int) -> dict[str, Any]:
    return {
        "auditorium": f"А-{100 + index}",
        "building": "Корпус 1",
        "name": f"Предмет {index}",
        "lesson_type": "Семинар",
        "begin_time": "09:30",
        "end_time": "10:50",
        "lecturer": "Преподаватель",
        "lesson_number": index + 1,
        "url1": "https://meet.fakeuni.test/room",
        "url1_description": "Онлайн-комната",
    }


def week(begin: date) -> list[dict[str, Any]]:
    """The source: 7 days from `begin`, one class on each Monday-to-Friday of busy weeks."""
    count = PER_WEEK.get(begin, 5)
    days: list[dict[str, Any]] = []
    for offset in range(WINDOW_DAYS):
        day = begin + timedelta(days=offset)
        lessons = [lesson(offset)] if offset < count and day.weekday() < 5 else []
        days.append({"date": day.isoformat(), "lessons": lessons})
    return days


def endpoint(**pagination: Any) -> dict[str, Any]:
    def instant(clock: str) -> dict[str, Any]:
        return {
            "compose": {
                "type": "dateTime",
                "date": {"from": "day", "path": "/date"},
                "time": {"path": "/" + clock},
                "timeZone": "Europe/Moscow",
            },
            "required": True,
        }

    return {
        "id": "schedule",
        "kind": "scheduleEvents",
        "request": {
            "method": "GET",
            "path": "/api/get-myschedule",
            "query": {"begin_date": "{weekStart}"},
        },
        "pagination": {
            "type": "dateWindow",
            "param": "begin_date",
            "stepDays": 7,
            "windows": 6,
            "startOffset": -1,
            **pagination,
        },
        "uiUrl": {"path": "/schedule", "query": {"begin_date": "{weekStart}"}},
        "response": {
            "format": "json",
            "collections": [{"as": "day", "path": ""}, {"as": "lesson", "path": "/lessons"}],
            "fields": {
                "title": {"from": "lesson", "path": "/name", "required": True},
                "startsAt": instant("begin_time"),
                "endsAt": instant("end_time"),
                "location": {"from": "lesson", "path": "/auditorium"},
                "kindText": {"from": "lesson", "path": "/lesson_type"},
                "address": {"from": "lesson", "path": "/building"},
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


def spec(endpoint_: dict[str, Any] | None = None) -> RecipeSpec:
    data = recipes.recipe_spec([endpoint_ or endpoint()], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    return RecipeSpec.model_validate(data)


def server(requests: list[str], *, ignore_date: bool = False, empty: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        begin = date.fromisoformat(request.url.params["begin_date"])
        requests.append(begin.isoformat())
        if ignore_date:
            begin = date(2026, 11, 2)
        return httpx.Response(200, json=[] if empty else week(begin))

    return httpx.MockTransport(handler)


async def read(adapter_spec: RecipeSpec, transport: httpx.AsyncBaseTransport, now: datetime = NOW):
    ctx = make_ctx(transport=transport)
    ctx.now = now
    return await RecipeAdapter(adapter_spec).fetch(ctx)


async def test_each_window_renders_the_week_start_of_its_own_day_and_the_walk_is_complete() -> None:
    requests: list[str] = []
    result = await read(spec(), server(requests))

    # Tuesday 27.10: the previous Monday, the current one, and four more.
    assert requests == [
        "2026-10-19",
        "2026-10-26",
        "2026-11-02",
        "2026-11-09",
        "2026-11-16",
        "2026-11-23",
    ]
    (outcome,) = result.outcomes
    assert outcome.ok
    assert outcome.problem is None
    assert outcome.windows == 6
    # 5 + 1 + 5 + 5 + 5 + 5 classes: the thin control-work week does not end the walk.
    assert len(result.records) == 26


async def test_records_carry_the_page_of_their_own_window_and_the_card_fields() -> None:
    result = await read(spec(), server([]))
    control = [r for r in result.records if r.open_url and "begin_date=2026-10-26" in r.open_url]
    assert len(control) == 1
    assert control[0].open_url == "https://fakeuni.test/schedule?begin_date=2026-10-26"
    assert control[0].source_url.startswith("https://fakeuni.test/api/get-myschedule?")
    later = next(r for r in result.records if "begin_date=2026-11-02" in (r.open_url or ""))
    assert later.fields["kindText"] == "Семинар"
    assert later.fields["address"] == "Корпус 1"
    assert later.fields["lessonNumber"] == 1
    assert later.fields["links"] == [
        {"url": "https://meet.fakeuni.test/room", "label": "Онлайн-комната"}
    ]
    assert later.fields["attributes"] == [{"label": "Корпус", "value": "Корпус 1"}]


async def test_an_empty_window_is_not_the_end_but_all_empty_windows_are_empty() -> None:
    requests: list[str] = []
    result = await read(spec(), server(requests, empty=True))
    (outcome,) = result.outcomes
    assert len(requests) == 6  # every window was asked
    assert outcome.problem == "empty"
    assert outcome.ok
    assert not result.records


async def test_a_source_that_ignores_the_date_is_no_progress_not_a_complete_snapshot() -> None:
    requests: list[str] = []
    result = await read(spec(), server(requests, ignore_date=True))
    (outcome,) = result.outcomes
    assert not outcome.ok
    assert outcome.problem == "no_progress"
    assert len(requests) == 2  # the second window repeated the first answer
    assert result.records  # what was read stays usable
    assert result.complete_kinds() == set()


async def test_step_days_follow_the_declared_step_across_a_clock_change() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    # Saturday evening before the clocks go back: +7 days is still Saturday 23:30 local.
    now = datetime(2026, 10, 24, 23, 30, tzinfo=berlin)
    moved = templates.shifted_now(now, berlin, 7)
    assert (moved.date(), moved.hour) == (date(2026, 10, 31), 23)
    assert templates.render("{weekStart}", now=moved, tz=berlin, params={}) == "2026-10-26"
    assert templates.render("{plusDays:1}", now=moved, tz=berlin, params={}) == "2026-11-01"


async def test_an_unreadable_window_fails_the_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["begin_date"] == "2026-11-09":
            return httpx.Response(400)
        return httpx.Response(200, json=week(date.fromisoformat(request.url.params["begin_date"])))

    result = await read(spec(), httpx.MockTransport(handler))
    (outcome,) = result.outcomes
    assert not outcome.ok
    assert outcome.problem == "status"
    assert result.complete_kinds() == set()


# --- the contract --------------------------------------------------------------------------


def document(**pagination: Any) -> dict[str, Any]:
    data = recipes.recipe_spec([endpoint(**pagination)], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    return data


def test_schema_accepts_the_window_and_rejects_page_options_beside_it() -> None:
    assert errors("adapter-spec.schema.json", document()) == []
    assert errors("adapter-spec.schema.json", document(maxPages=3))
    assert errors("adapter-spec.schema.json", document(limit=10))
    assert errors("adapter-spec.schema.json", document(windows=13))
    assert errors("adapter-spec.schema.json", document(stepDays=32))
    assert errors("adapter-spec.schema.json", document(startOffset=1))
    assert errors("adapter-spec.schema.json", document(startOffset=-5))


def test_version_one_keeps_its_strict_shape() -> None:
    data = document()
    data["recipe"]["version"] = 1
    assert errors("adapter-spec.schema.json", data)
    with pytest.raises(ValidationError):
        RecipeSpec.model_validate(data)


def test_window_options_do_not_belong_to_other_pagination() -> None:
    data = document()
    data["recipe"]["endpoints"][0]["pagination"] = {"type": "page", "windows": 3}
    assert errors("adapter-spec.schema.json", data)
    with pytest.raises(ValidationError):
        RecipeSpec.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e["pagination"].update(param="to"),  # not a query key of the request
        lambda e: e["request"]["query"].update(begin_date="2026-10-26"),  # a literal date
        lambda e: e["request"].update(render=True),
        lambda e: e["pagination"].update(startOffset=-4, windows=3),  # misses the current window
    ],
)
def test_the_window_must_move_a_date_template_and_include_the_observed_window(
    mutation: Any,
) -> None:
    data = document()
    mutation(data["recipe"]["endpoints"][0])
    with pytest.raises(ValidationError):
        Recipe.model_validate(data["recipe"])


def test_typed_round_trip_of_a_version_one_recipe_has_no_window_syntax() -> None:
    plain = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
    dumped = RecipeSpec.model_validate(copy.deepcopy(plain)).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert dumped["recipe"]["endpoints"][0].get("uiUrl") is None
    assert "stepDays" not in str(dumped)


def test_utc_now_is_irrelevant_for_the_default_clock() -> None:
    assert NOW.tzinfo is UTC
