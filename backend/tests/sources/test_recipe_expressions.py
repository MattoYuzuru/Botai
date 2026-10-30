"""Pure data-language checks: bounded traversal, explicit calendars and conservative DST."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from botai.contracts.specs import JsonComposedField, JsonResponse, JsonWeekly, RecipeSpec
from botai.sources.expressions import (
    ExpressionError,
    collect,
    expand_weekly,
    field_value,
)
from tests.sources import recipes

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
TZ = ZoneInfo("Europe/Moscow")


def weekly(**changes: Any) -> JsonWeekly:
    data: dict[str, Any] = {
        "type": "weekly",
        "timeZone": "Europe/Moscow",
        "startDate": {"path": "/from"},
        "untilDate": {"path": "/until"},
        "weekdays": {"path": "/days"},
        "startTime": {"path": "/start"},
        "endTime": {"path": "/end"},
        "horizon": {"from": "{weekStart}", "days": 21},
    }
    data.update(changes)
    return JsonWeekly.model_validate(data)


def series(**changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "from": "2026-10-01",
        "until": "2026-12-31",
        "days": [2],
        "start": "09:30",
        "end": "10:50",
    }
    item.update(changes)
    return {"item": item}


def test_v1_typed_round_trip_does_not_invent_v2_syntax() -> None:
    spec = RecipeSpec.model_validate(recipes.recipe_spec())
    dumped = spec.model_dump(by_alias=True)
    assert "collections" not in dumped["recipe"]["endpoints"][0]["response"]
    assert "from" not in dumped["recipe"]["endpoints"][0]["response"]["fields"]["title"]
    assert RecipeSpec.model_validate(dumped) == spec
    assert spec.recipe.model_dump(include={"endpoints": {"__all__": {"id"}}}) == {
        "endpoints": [{"id": "timetable"}, {"id": "deadlines"}]
    }


@pytest.mark.parametrize("extra", [{"from": "item"}, {"from": "root"}])
def test_v1_explicit_source_alias_is_not_silently_accepted(extra: dict[str, str]) -> None:
    data = recipes.recipe_spec()
    data["recipe"]["endpoints"][0]["response"]["fields"]["title"].update(extra)
    with pytest.raises(ValidationError, match="require recipe v2"):
        RecipeSpec.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"collections": [{"as": "day", "path": "/days"}]},
        {"itemsPath": None},
    ],
)
def test_exactly_one_collection_source(changes: dict[str, Any]) -> None:
    data = {"format": "json", "itemsPath": "/items", "fields": {}}
    data.update(changes)
    with pytest.raises(ValidationError, match="exactly one"):
        JsonResponse.model_validate(data)


@pytest.mark.parametrize("aliases", [["root"], ["item"], ["day", "day"]])
def test_aliases_are_unambiguous(aliases: list[str]) -> None:
    with pytest.raises(ValidationError, match="unique and not reserved"):
        JsonResponse.model_validate(
            {
                "format": "json",
                "collections": [{"as": alias, "path": "/items"} for alias in aliases],
                "fields": {},
            }
        )


def test_unknown_alias_and_recursive_compose_are_rejected() -> None:
    with pytest.raises(ValidationError, match="scalar source"):
        JsonResponse.model_validate(
            {"format": "json", "itemsPath": "", "fields": {"title": {"from": "lost", "path": ""}}}
        )
    with pytest.raises(ValidationError):
        JsonComposedField.model_validate(
            {
                "compose": {
                    "type": "dateTime",
                    "date": {"compose": {"type": "dateTime"}},
                    "time": {"path": "/start"},
                    "timeZone": "Europe/Moscow",
                }
            }
        )


def test_three_levels_keep_parent_bindings_and_paginate_parents() -> None:
    response = JsonResponse.model_validate(
        {
            "format": "json",
            "collections": [
                {"as": "term", "path": "/terms"},
                {"as": "day", "path": "/days"},
                {"as": "lesson", "path": "/lessons"},
            ],
            "fields": {},
        }
    )
    page = collect(
        {"terms": [{"label": "Autumn", "days": [{"date": "2026-10-27", "lessons": [1, 2]}]}]},
        response,
    )
    assert page.root_count == 1
    assert page.nodes_seen == 4
    assert len(page.items) == 2
    assert page.items[1]["term"]["label"] == "Autumn"
    assert page.items[1]["day"]["date"] == "2026-10-27"
    assert page.items[1]["lesson"] == page.items[1]["item"] == 2


@pytest.mark.parametrize("key", ["remaining_nodes", "remaining_leaves"])
def test_collection_overflow_is_an_error_instead_of_a_partial_list(key: str) -> None:
    response = JsonResponse.model_validate({"format": "json", "itemsPath": "", "fields": {}})
    with pytest.raises(ExpressionError, match="limit"):
        collect([1, 2, 3], response, **{key: 2})


def composed(zone: str = "Europe/Moscow", **changes: Any) -> JsonComposedField:
    expression: dict[str, Any] = {
        "type": "dateTime",
        "date": {"from": "day", "path": "/date"},
        "time": {"path": "/start"},
        "timeZone": zone,
    }
    expression.update(changes)
    return JsonComposedField.model_validate({"compose": expression, "required": True})


def test_compose_uses_explicit_parent_date_and_zone() -> None:
    bindings = {"day": {"date": "27.10.2026"}, "item": {"start": "09:30"}}
    assert (
        field_value(composed(), bindings, ZoneInfo("Pacific/Honolulu"))
        == "2026-10-27T06:30:00+00:00"
    )


@pytest.mark.parametrize("bad", ["27.10.26", "27 октября", "2026-10-27T10:00:00", "2026-02-29"])
def test_compose_never_infers_year_or_discards_date_parts(bad: str) -> None:
    with pytest.raises(ExpressionError, match="fields"):
        field_value(composed(), {"day": {"date": bad}, "item": {"start": "09:30"}}, TZ)


@pytest.mark.parametrize(("day", "clock"), [("2026-03-29", "02:30"), ("2026-10-25", "02:30")])
def test_dst_gap_and_unspecified_fold_fail(day: str, clock: str) -> None:
    with pytest.raises(ExpressionError, match="fields"):
        field_value(composed("Europe/Berlin"), {"day": {"date": day}, "item": {"start": clock}}, TZ)


def test_explicit_fold_policy_selects_a_proven_instant() -> None:
    bindings = {"day": {"date": "2026-10-25"}, "item": {"start": "02:30"}}
    assert (
        field_value(composed("Europe/Berlin", fold="earlier"), bindings, TZ)
        == "2026-10-25T00:30:00+00:00"
    )
    assert (
        field_value(composed("Europe/Berlin", fold="later"), bindings, TZ)
        == "2026-10-25T01:30:00+00:00"
    )


def test_optional_compose_does_not_hide_an_ambiguous_source_date() -> None:
    expression = composed("Europe/Berlin")
    expression.required = False
    with pytest.raises(ExpressionError, match="fields"):
        field_value(expression, {"day": {"date": "2026-10-25"}, "item": {"start": "02:30"}}, TZ)


def test_weekly_parity_holidays_and_extra_dates_are_observed() -> None:
    expression = weekly(
        intervalWeeks=2,
        anchorDate={"path": "/anchor"},
        excludeDates={"path": "/cancelled"},
        includeDates={"path": "/extra"},
    )
    result = expand_weekly(
        expression,
        series(anchor="2026-10-26", cancelled=["2026-10-27"], extra=["2026-11-04"]),
        now=NOW,
    )
    assert [occ.local_date.isoformat() for occ in result] == ["2026-11-04", "2026-11-10"]
    assert result[0].starts_at == "2026-11-04T06:30:00+00:00"


def test_overnight_is_explicit_and_duration_is_bounded() -> None:
    with pytest.raises(ExpressionError, match="fields"):
        expand_weekly(weekly(), series(start="23:30", end="00:30"), now=NOW)
    explicit = expand_weekly(weekly(endDayOffset=1), series(start="23:30", end="00:30"), now=NOW)
    assert explicit[0].ends_at == "2026-10-27T21:30:00+00:00"
    duration = weekly(endTime=None, durationMinutes={"path": "/minutes"})
    assert (
        expand_weekly(duration, series(minutes=80), now=NOW)[0].ends_at
        == "2026-10-27T07:50:00+00:00"
    )
    with pytest.raises(ExpressionError, match="fields"):
        expand_weekly(duration, series(minutes=1441), now=NOW)


@pytest.mark.parametrize("changes", [{"maxOccurrences": 1}, {}])
def test_recurrence_overflow_never_returns_a_partial_series(changes: dict[str, Any]) -> None:
    with pytest.raises(ExpressionError, match="limit"):
        expand_weekly(weekly(**changes), series(), now=NOW, remaining_instances=2)


@pytest.mark.parametrize("bad", [[True], [0], {"mon": 1}, [1, 2, 3, 4, 5, 6, 7, 1]])
def test_invalid_weekday_shape_has_a_static_failure(bad: Any) -> None:
    with pytest.raises(ExpressionError, match="fields"):
        expand_weekly(weekly(), series(days=bad), now=NOW)


@pytest.mark.parametrize(
    ("word", "iso_day"),
    [
        ("monday", 1),
        ("Tuesday", 2),
        (" wed ", 3),
        ("TH", 4),
        ("пятница", 5),
        ("Сб", 6),
        ("вс.", 7),
        ("воскресенье", 7),
        ("3", 3),
    ],
)
def test_a_weekday_named_by_the_source_is_an_iso_day(word: str, iso_day: int) -> None:
    # 2026-10-26 is a Monday: the only day of this window with the named ISO day is that day.
    result = expand_weekly(
        weekly(horizon={"from": "{weekStart}", "days": 7}), series(days=[word]), now=NOW
    )
    assert [occ.local_date.isoweekday() for occ in result] == [iso_day]


def test_weekday_names_form_a_list_and_nothing_is_guessed() -> None:
    result = expand_weekly(
        weekly(horizon={"from": "{weekStart}", "days": 7}),
        series(days=["monday", "Ср", 5]),
        now=NOW,
    )
    assert [occ.local_date.isoformat() for occ in result] == [
        "2026-10-26",
        "2026-10-28",
        "2026-10-30",
    ]
    for guess in ("mon,wed", "mondays", "poniedelnik", "понедельникам", "", "0", "8"):
        with pytest.raises(ExpressionError, match="fields"):
            expand_weekly(weekly(), series(days=guess), now=NOW)


def test_the_interval_of_the_rule_is_read_from_the_source() -> None:
    expression = weekly(intervalWeeks={"path": "/interval"})
    every_week = expand_weekly(expression, series(interval=1), now=NOW)
    every_other = expand_weekly(expression, series(interval=2), now=NOW)
    # the count starts at the week of the rule's first date (2026-10-01, a Thursday)
    assert [occ.local_date.isoformat() for occ in every_other] == ["2026-10-27", "2026-11-10"]
    assert len(every_week) == 3
    assert expand_weekly(expression, series(interval="2"), now=NOW) == every_other
    for bad in (0, 5, True, "x", None, 1.5):
        with pytest.raises(ExpressionError, match="fields"):
            expand_weekly(expression, series(interval=bad), now=NOW)


def test_horizon_and_timezone_are_validated_before_requests() -> None:
    with pytest.raises(ValidationError):
        weekly(timeZone="Unknown/Zone")
    with pytest.raises(ValidationError):
        weekly(horizon={"from": "{nowUnix}", "days": 10})
    with pytest.raises(ValidationError):
        weekly(horizon={"from": "{today}", "days": 94})
    with pytest.raises(ValidationError):
        weekly(intervalWeeks=2)
