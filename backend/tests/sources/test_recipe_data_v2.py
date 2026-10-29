"""Synthetic university response shapes, parsed without a model or external requests."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from botai.contracts.schemas import errors
from botai.contracts.specs import RecipeSpec
from botai.sources.adapter import AdapterResult
from botai.sources.adapters.recipe import RecipeAdapter
from tests.sources import recipes
from tests.sources.helpers import make_ctx


def nested_json() -> dict[str, Any]:
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
        "id": "nestedSchedule",
        "kind": "scheduleEvents",
        "request": {"method": "GET", "path": "/schedule"},
        "response": {
            "format": "json",
            "collections": [{"as": "day", "path": "/days"}, {"as": "lesson", "path": "/lessons"}],
            "fields": {
                "externalId": {"path": "/id"},
                "title": {"path": "/title", "required": True},
                "startsAt": instant("begin"),
                "endsAt": instant("end"),
                "location": {"from": "root", "path": "/building"},
            },
            "identity": ["externalId", "startsAt"],
        },
    }


def day(date: str, count: int = 1) -> dict[str, Any]:
    return {
        "date": date,
        "lessons": [
            {"id": f"subject-{i}", "title": f"Курс {i}", "begin": "09:30", "end": "10:50"}
            for i in range(count)
        ],
    }


async def fetch(
    endpoint: dict[str, Any],
    document: Any,
    *,
    html: bool = False,
    shift_days: int = 0,
) -> AdapterResult:
    def handler(request: httpx.Request) -> httpx.Response:
        if html:
            return httpx.Response(200, text=document, headers={"content-type": "text/html"})
        return httpx.Response(200, json=document)

    data = recipes.recipe_spec([endpoint], require=(endpoint["kind"],))
    data["recipe"]["version"] = 2
    adapter = RecipeAdapter(RecipeSpec.model_validate(data))
    ctx = make_ctx(transport=httpx.MockTransport(handler))
    ctx.now += timedelta(days=shift_days)
    return await adapter.fetch(ctx)


async def test_nested_json_parent_dates_compose_distinct_instances_and_provenance() -> None:
    result = await fetch(
        nested_json(), {"building": "А", "days": [day("2026-10-27"), day("2026-10-28")]}
    )
    assert result.outcomes[0].ok
    assert result.outcomes[0].items_seen == 2
    assert len(result.records) == 2
    assert len({r.external_id for r in result.records}) == 2
    assert {r.fields["startsAt"] for r in result.records} == {
        "2026-10-27T06:30:00+00:00",
        "2026-10-28T06:30:00+00:00",
    }
    assert all(r.fields["location"] == "А" for r in result.records)
    assert all(r.source_url == "https://fakeuni.test/schedule" for r in result.records)
    assert result.fetched_at.tzinfo is not None


async def test_repeated_ids_without_declared_parent_identity_fail_honestly() -> None:
    endpoint = nested_json()
    del endpoint["response"]["identity"]
    result = await fetch(endpoint, {"days": [day("2026-10-27"), day("2026-10-28")]})
    assert result.records == []
    assert result.outcomes[0].problem == "fields"
    assert result.complete_kinds() == set()


async def test_nested_empty_child_array_is_a_complete_empty_response() -> None:
    result = await fetch(nested_json(), {"days": [{"date": "2026-10-27", "lessons": []}]})
    assert result.records == []
    assert result.outcomes[0].ok
    assert result.outcomes[0].problem == "empty"
    assert result.complete_kinds() == {"scheduleEvents"}


async def test_nested_html_fields_can_read_the_parent_header() -> None:
    endpoint = {
        "id": "schedulePage",
        "kind": "scheduleEvents",
        "request": {"method": "GET", "path": "/schedule"},
        "response": {
            "format": "html",
            "collections": [
                {"as": "day", "selector": "section.day"},
                {"as": "lesson", "selector": "li"},
            ],
            "fields": {
                "title": {"selector": ".title", "required": True},
                "startsAt": {
                    "compose": {
                        "type": "dateTime",
                        "date": {"from": "day", "selector": "time", "attr": "datetime"},
                        "time": {"selector": ".begin"},
                        "timeZone": "Europe/Moscow",
                    },
                    "required": True,
                },
                "endsAt": {
                    "compose": {
                        "type": "dateTime",
                        "date": {"from": "day", "selector": "time", "attr": "datetime"},
                        "time": {"selector": ".end"},
                        "timeZone": "Europe/Moscow",
                    },
                    "required": True,
                },
            },
            "identity": ["title", "startsAt"],
        },
    }
    document = """<section class="day"><time datetime="2026-10-27"></time><ul>
      <li><b class="title">Алгебра</b><span class="begin">09:30</span><span class="end">10:50</span></li>
      <li><b class="title">Анализ</b><span class="begin">11:10</span><span class="end">12:30</span></li>
    </ul></section>"""
    result = await fetch(endpoint, document, html=True)
    assert result.outcomes[0].ok
    assert result.outcomes[0].items_seen == 1
    assert [r.fields["startsAt"] for r in result.records] == [
        "2026-10-27T06:30:00+00:00",
        "2026-10-27T08:10:00+00:00",
    ]


async def test_nested_pagination_offsets_and_total_count_parents_not_leaves() -> None:
    offsets: list[int] = []
    endpoint = nested_json()
    endpoint["pagination"] = {
        "type": "offset",
        "param": "offset",
        "limitParam": "limit",
        "limit": 2,
        "maxPages": 5,
        "totalPath": "/total",
    }
    documents = [day("2026-10-27", 6), day("2026-10-28", 6), day("2026-10-29", 6)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(200, json={"days": documents[offset : offset + 2], "total": 3})

    data = recipes.recipe_spec([endpoint], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    result = await RecipeAdapter(RecipeSpec.model_validate(data)).fetch(
        make_ctx(transport=httpx.MockTransport(handler))
    )
    assert offsets == [0, 2]
    assert result.outcomes[0].items_seen == 3
    assert result.outcomes[0].items_valid == 18
    assert result.complete_kinds() == {"scheduleEvents"}


def recurring_endpoint() -> dict[str, Any]:
    return {
        "id": "series",
        "kind": "scheduleEvents",
        "request": {"method": "GET", "path": "/series"},
        "response": {
            "format": "json",
            "itemsPath": "/series",
            "fields": {
                "externalId": {"path": "/id"},
                "title": {"path": "/title", "required": True},
            },
            "expand": {
                "type": "weekly",
                "timeZone": "Europe/Moscow",
                "startDate": {"path": "/from"},
                "untilDate": {"path": "/until"},
                "weekdays": {"path": "/days"},
                "startTime": {"path": "/start"},
                "endTime": {"path": "/end"},
                "horizon": {"from": "{weekStart}", "days": 21},
            },
        },
    }


def recurring_data() -> dict[str, Any]:
    return {
        "series": [
            {
                "id": "course-1",
                "title": "Алгебра",
                "from": "2026-09-01",
                "until": "2026-12-31",
                "days": [2],
                "start": "09:30",
                "end": "10:50",
            }
        ]
    }


def test_runtime_fixture_shapes_match_the_authoritative_v2_contract() -> None:
    for endpoint in (nested_json(), recurring_endpoint()):
        data = recipes.recipe_spec([endpoint], require=("scheduleEvents",))
        data["recipe"]["version"] = 2
        assert errors("adapter-spec.schema.json", data) == []


async def test_recurrence_ids_survive_overlapping_horizons_and_duration_edits() -> None:
    endpoint = recurring_endpoint()
    data = recurring_data()
    first = await fetch(endpoint, data)
    second = await fetch(endpoint, data, shift_days=7)
    altered = deepcopy(data)
    altered["series"][0]["end"] = "11:00"
    third = await fetch(endpoint, altered)

    def ids_by_start(result: AdapterResult) -> dict[str, str]:
        return {r.fields["startsAt"]: r.external_id for r in result.records}

    old, new = ids_by_start(first), ids_by_start(second)
    overlap = old.keys() & new.keys()
    assert len(overlap) == 2
    assert all(old[start] == new[start] for start in overlap)
    assert ids_by_start(third) == old
    assert all(
        a.fields["endsAt"] != b.fields["endsAt"]
        for a, b in zip(first.records, third.records, strict=True)
    )
    assert first.outcomes[0].items_seen == 1
    assert first.outcomes[0].items_valid == 3
    assert all(r.source_url == "https://fakeuni.test/series" for r in first.records)


async def test_series_outside_the_requested_horizon_is_valid_empty_data() -> None:
    data = recurring_data()
    data["series"][0]["until"] = "2026-09-30"
    result = await fetch(recurring_endpoint(), data)
    assert result.records == []
    assert result.outcomes[0].ok
    assert result.complete_kinds() == {"scheduleEvents"}


async def test_recurrence_cap_rejects_entire_endpoint_including_previous_series() -> None:
    endpoint = recurring_endpoint()
    endpoint["response"]["expand"]["maxOccurrences"] = 2
    result = await fetch(endpoint, recurring_data())
    assert result.records == []
    assert result.outcomes[0].problem == "limit"
    assert result.complete_kinds() == set()


async def test_leaf_overflow_is_not_silently_truncated_to_a_success() -> None:
    result = await fetch(nested_json(), {"days": [day("2026-10-27", 5001)]})
    assert result.records == []
    assert result.outcomes[0].problem == "limit"
    assert result.complete_kinds() == set()


async def test_later_invalid_child_does_not_commit_an_earlier_partial_schedule() -> None:
    result = await fetch(nested_json(), {"days": [day("2026-10-27"), day("27.10.26")]})
    assert result.records == []
    assert result.outcomes[0].problem == "fields"


async def test_composed_overnight_requires_explicit_day_offset() -> None:
    endpoint = nested_json()
    data = {"days": [day("2026-10-27")]}
    data["days"][0]["lessons"][0].update(begin="23:30", end="00:30")
    denied = await fetch(endpoint, data)
    assert denied.outcomes[0].problem == "fields"
    assert denied.records == []
    endpoint["response"]["fields"]["endsAt"]["compose"]["dayOffset"] = 1
    allowed = await fetch(endpoint, data)
    assert allowed.outcomes[0].ok
    assert allowed.records[0].fields["endsAt"] == "2026-10-27T21:30:00+00:00"


@pytest.mark.parametrize("change", ["version", "kind", "timeField"])
def test_invalid_dsl_combination_is_rejected_before_runtime(change: str) -> None:
    endpoint = recurring_endpoint()
    data = recipes.recipe_spec([endpoint], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    if change == "version":
        data["recipe"]["version"] = 1
    elif change == "kind":
        data["recipe"]["endpoints"][0]["kind"] = "deadlines"
    else:
        data["recipe"]["endpoints"][0]["response"]["fields"]["startsAt"] = {"path": "/start"}
    with pytest.raises(ValidationError):
        RecipeSpec.model_validate(data)


async def test_pagination_cap_and_repeated_pages_are_incomplete_with_usable_records() -> None:
    endpoint = {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": "/courses"},
        "pagination": {"type": "page", "param": "page", "limit": 1, "maxPages": 1},
        "response": {"format": "json", "itemsPath": "", "fields": {"externalId": {"path": "/id"}}},
    }
    capped = await fetch(endpoint, [{"id": "c1"}])
    assert len(capped.records) == 1
    assert capped.outcomes[0].problem == "limit"
    assert capped.complete_kinds() == set()
    endpoint["pagination"]["maxPages"] = 5
    repeated = await fetch(endpoint, [{"id": "c1"}])
    assert len(repeated.records) == 1
    assert repeated.outcomes[0].problem == "no_progress"
    assert repeated.complete_kinds() == set()


async def test_first_five_equal_ids_do_not_hide_new_data_on_a_later_page() -> None:
    endpoint = {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": "/courses"},
        "pagination": {"type": "page", "param": "page", "limit": 6, "maxPages": 3},
        "response": {"format": "json", "itemsPath": "", "fields": {"externalId": {"path": "/id"}}},
    }
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls.append(page)
        rows = [{"id": str(i)} for i in range(5)] + [{"id": str(page + 4)}] if page < 3 else []
        return httpx.Response(200, json=rows)

    data = recipes.recipe_spec([endpoint], require=("courses",))
    result = await RecipeAdapter(RecipeSpec.model_validate(data)).fetch(
        make_ctx(transport=httpx.MockTransport(handler))
    )
    assert calls == [1, 2, 3]
    assert len(result.records) == 7
    assert result.complete_kinds() == {"courses"}
