"""Recipe-v2 expressions keep the shared-spec safety boundary before any source read."""

from __future__ import annotations

from typing import Any

import pytest

from botai.learn.lint import LintContext, lint_spec
from tests.sources import recipes

CTX = LintContext(allowed_hosts=["fakeuni.test"], expected_origin="https://fakeuni.test")
MARKER = "PrivateObservedName"


def nested(format: str = "json", *, weekly: bool = False) -> dict[str, Any]:
    def scalar(name: str, **extra: Any) -> dict[str, Any]:
        key, value = ("path", "/" + name) if format == "json" else ("selector", "." + name)
        return {key: value, **extra}

    response: dict[str, Any] = {
        "format": format,
        "collections": [
            {"as": "day", **scalar("days")},
            {"as": "lesson", **scalar("lessons")},
        ],
        "fields": {
            "title": scalar("title", required=True),
            "externalId": scalar("id"),
        },
    }
    if weekly:
        response["expand"] = {
            "type": "weekly",
            "timeZone": "Europe/Moscow",
            "startDate": scalar("from", **{"from": "day"}),
            "untilDate": scalar("until", **{"from": "day"}),
            "weekdays": scalar("weekdays"),
            "startTime": scalar("start"),
            "durationMinutes": 90,
            "horizon": {"from": "{weekStart}", "days": 56},
        }
    else:
        response["fields"].update(
            {
                "startsAt": {
                    "compose": {
                        "type": "dateTime",
                        "date": scalar("date", **{"from": "day"}),
                        "time": scalar("start"),
                        "timeZone": "Europe/Moscow",
                    },
                    "required": True,
                },
                "durationMinutes": scalar("duration", transform="number"),
            }
        )
    endpoint = {
        "id": "schedule",
        "kind": "scheduleEvents",
        "request": {"method": "GET", "path": "/schedule"},
        "response": response,
    }
    spec = recipes.recipe_spec([endpoint], require=("scheduleEvents",))
    spec["recipe"]["version"] = 2
    return spec


def response(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["recipe"]["endpoints"][0]["response"]


@pytest.mark.parametrize("format", ["json", "html"])
@pytest.mark.parametrize("weekly", [False, True])
def test_nested_sources_and_weekly_generated_required_fields_pass(
    format: str, weekly: bool
) -> None:
    result = lint_spec(nested(format, weekly=weekly), CTX)
    assert result.ok, result.reasons()


@pytest.mark.parametrize("change", ["alias", "source", "zone", "weekly_source", "weekly_zone"])
def test_typed_cross_field_errors_are_static_and_never_include_input(change: str) -> None:
    spec = nested(weekly=change.startswith("weekly"))
    data = response(spec)
    if change == "alias":
        data["collections"][1]["as"] = "day"
    elif change == "source":
        data["fields"]["startsAt"]["compose"]["date"]["from"] = MARKER
    elif change == "zone":
        data["fields"]["startsAt"]["compose"]["timeZone"] = "Missing/" + MARKER
    elif change == "weekly_source":
        data["expand"]["startDate"]["from"] = MARKER
    else:
        data["expand"]["timeZone"] = "Missing/" + MARKER
    result = lint_spec(spec, CTX)
    assert "data.invalid" in result.codes()
    assert MARKER not in str(result.reasons())


@pytest.mark.parametrize(
    "location",
    [
        "collection",
        "field",
        "fallback",
        "compose_date",
        "compose_fallback",
        "weekly",
        "weekly_duration",
        "pagination",
    ],
)
def test_secret_pointers_are_checked_in_every_data_operand(location: str) -> None:
    spec = nested(weekly=location.startswith("weekly"))
    data = response(spec)
    if location == "collection":
        data["collections"][0]["path"] = "/access_token"
    elif location == "field":
        data["fields"]["title"]["path"] = "/password"
    elif location == "fallback":
        data["fields"]["title"]["fallbackPath"] = "/apiKey"
    elif location == "compose_date":
        data["fields"]["startsAt"]["compose"]["date"]["path"] = "/password"
    elif location == "compose_fallback":
        data["fields"]["startsAt"]["compose"]["time"]["fallbackPath"] = "/jwt"
    elif location == "weekly":
        data["expand"]["excludeDates"] = {"path": "/session_token"}
    elif location == "weekly_duration":
        data["expand"]["durationMinutes"] = {"path": "/apiKey"}
    else:
        spec["recipe"]["endpoints"][0]["pagination"] = {
            "type": "page",
            "param": "page",
            "totalPath": "/session_token",
        }
    assert "pointer.secret" in lint_spec(spec, CTX).codes()


@pytest.mark.parametrize(
    ("location", "key", "value", "code"),
    [
        ("collection", "selector", "li:has(a)", "html.selector"),
        ("compose", "selector", "li:has(a)", "html.selector"),
        ("compose", "attr", "onclick", "html.attr"),
        ("compose", "attr", "data-csrf", "html.attr"),
        ("compose", "pattern", "(a+)+$", "pattern.unsafe"),
        ("weekly", "selector", "li:has(a)", "html.selector"),
        ("weekly", "attr", "onclick", "html.attr"),
        ("weekly", "attr", "data-csrf", "html.attr"),
        ("weekly", "pattern", "(a+)+$", "pattern.unsafe"),
    ],
)
def test_html_safety_recurses_into_new_expression_nodes(
    location: str, key: str, value: str, code: str
) -> None:
    spec = nested("html", weekly=location == "weekly")
    data = response(spec)
    if location == "collection":
        operand = data["collections"][0]
    elif location == "compose":
        operand = data["fields"]["startsAt"]["compose"]["date"]
    else:
        operand = data["expand"]["startDate"]
    operand[key] = value
    assert code in lint_spec(spec, CTX).codes()


@pytest.mark.parametrize(
    "location", ["selector", "pointer", "alias", "attribute", "weekly_pointer"]
)
def test_observed_values_cannot_hide_in_nested_structural_literals(location: str) -> None:
    spec = nested(
        "html" if location in {"selector", "attribute"} else "json",
        weekly=location == "weekly_pointer",
    )
    data = response(spec)
    if location == "selector":
        data["fields"]["startsAt"]["compose"]["date"]["selector"] = "." + MARKER
    elif location == "pointer":
        data["fields"]["startsAt"]["compose"]["date"]["path"] = "/" + MARKER
    elif location == "alias":
        data["collections"][0]["as"] = MARKER
        data["fields"]["startsAt"]["compose"]["date"]["from"] = MARKER
    elif location == "attribute":
        data["fields"]["startsAt"]["compose"]["date"]["attr"] = MARKER
    else:
        data["expand"]["excludeDates"] = {"path": "/" + MARKER}
    result = lint_spec(spec, LintContext(observed=[MARKER]))
    assert "observed.value" in result.codes()
    assert MARKER not in str(result.reasons())


def test_weekly_requires_a_stable_declared_series_identity_and_title() -> None:
    spec = nested(weekly=True)
    data = response(spec)
    del data["fields"]["externalId"]
    assert "identity.missing" in lint_spec(spec, CTX).codes()
    data["identity"] = ["startsAt"]
    assert "identity.unknown_field" in lint_spec(spec, CTX).codes()
    data["identity"] = ["title"]
    assert lint_spec(spec, CTX).ok
    del data["fields"]["title"]
    assert "field.missing_required" in lint_spec(spec, CTX).codes()


def test_v1_does_not_accept_v2_fields_and_existing_fixture_still_passes() -> None:
    spec = nested()
    spec["recipe"]["version"] = 1
    assert lint_spec(spec, CTX).codes() == {"schema"}
    assert lint_spec(recipes.full_recipe(), CTX).ok
