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


def test_the_interval_pointer_is_checked_like_every_other_operand() -> None:
    spec = nested(weekly=True)
    expansion = response(spec)["expand"]
    expansion["intervalWeeks"] = {"from": "day", "path": "/interval"}
    assert lint_spec(spec, CTX).ok
    expansion["intervalWeeks"] = {"from": "day", "path": "/apiKey"}
    assert "pointer.secret" in lint_spec(spec, CTX).codes()


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
    data["fields"]["description"] = {"path": "/description"}
    assert "field.missing_required" in lint_spec(spec, CTX).codes()


def test_v1_does_not_accept_v2_fields_and_existing_fixture_still_passes() -> None:
    spec = nested()
    spec["recipe"]["version"] = 1
    assert lint_spec(spec, CTX).codes() == {"schema"}
    assert lint_spec(recipes.full_recipe(), CTX).ok


# --- repeated query keys and the constants of the source's own vocabulary ---------------------

STATES = ["backlog", "inProgress", "review", "failed", "evaluated"]


def tasks_spec(states: list[str]) -> dict[str, Any]:
    endpoint = {
        "id": "deadlines",
        "kind": "deadlines",
        "request": {
            "method": "GET",
            "path": "/api/tasks",
            "query": {"state": states if len(states) > 1 else states[0]},
        },
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {
                "externalId": {"path": "/id", "required": True},
                "title": {"path": "/exercise/name", "required": True},
                "dueAt": {"path": "/deadline", "transform": "iso", "required": True},
            },
        },
    }
    spec = recipes.recipe_spec([endpoint], require=("deadlines",))
    spec["recipe"]["version"] = 2
    return spec


def test_a_repeated_key_of_filter_values_passes_lint() -> None:
    assert lint_spec(tasks_spec(STATES), CTX).ok


def seen_ctx(*values: str) -> LintContext:
    return LintContext(
        allowed_hosts=CTX.allowed_hosts, expected_origin=CTX.expected_origin, observed=list(values)
    )


@pytest.mark.parametrize(
    "status",
    ["inProgress", "IN_PROGRESS", "notEvaluated", "awaiting-review", "Backlog", "hasSolution"],
)
def test_a_filter_made_of_status_words_is_not_a_personal_value(status: str) -> None:
    seen = seen_ctx(*STATES, status)
    assert lint_spec(tasks_spec([*STATES[:2], status]), seen).ok
    assert lint_spec(tasks_spec([status, "backlog"]), seen).ok


@pytest.mark.parametrize(
    "value",
    [
        "Ivanov",
        "ГРП-26",
        "ABC12",
        "Evaluated2",
        "group7",
        "IVANOV",
        # camel and snake case do not make a status of a login or a name
        "jSmith",
        "ivanPetrov",
        "deLaCruz",
        "PETROV_AA",
        "smithJohn",
        "WHITE_FOX",
    ],
)
def test_a_name_or_a_code_of_the_student_is_still_refused_in_a_query(value: str) -> None:
    seen = seen_ctx(value)
    for states in ([value], ["backlog", value], [value, "inProgress"]):
        result = lint_spec(tasks_spec(states), seen)
        assert "observed.value" in result.codes(), (value, states)
        assert value not in str(result.reasons())


def test_the_exemption_is_for_a_query_not_for_a_path_or_a_body() -> None:
    seen = seen_ctx("inProgress")
    spec = tasks_spec(["backlog", "inProgress"])
    assert lint_spec(spec, seen).ok
    spec["recipe"]["endpoints"][0]["request"]["path"] = "/api/tasks/inProgress"
    assert "observed.value" in lint_spec(spec, seen).codes()


def test_lint_checks_every_value_of_a_repeated_key() -> None:
    spec = tasks_spec(["backlog", "{missing}"])
    assert "template.unknown" in lint_spec(spec, CTX).codes()
    spec = tasks_spec(["backlog", "inProgress"])
    spec["recipe"]["endpoints"][0]["request"]["query"] = {"password": ["a", "b"]}
    assert "param.secret_name" in lint_spec(spec, CTX).codes()
