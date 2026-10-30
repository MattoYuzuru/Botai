"""Version two extends a declarative contract without weakening stored version-one recipes."""

from __future__ import annotations

import copy
import json

import pytest

from botai.contracts.schemas import errors, validator
from tests.sources.recipes import recipe_spec, timetable_endpoint


def base(version: int = 2) -> dict:
    spec = recipe_spec([timetable_endpoint()], require=("scheduleEvents",))
    spec["recipe"]["version"] = version
    return spec


def nested() -> dict:
    spec = base()
    spec["recipe"]["endpoints"][0]["response"] = {
        "format": "json",
        "collections": [{"as": "day", "path": "/days"}, {"as": "lesson", "path": "/lessons"}],
        "fields": {
            "title": {"from": "lesson", "path": "/title", "required": True},
            "startsAt": {
                "compose": {
                    "type": "dateTime",
                    "date": {"from": "day", "path": "/date"},
                    "time": {"from": "lesson", "path": "/start"},
                    "timeZone": "Europe/Moscow",
                },
                "required": True,
            },
            "durationMinutes": {"from": "lesson", "path": "/duration", "transform": "number"},
        },
        "identity": ["title", "startsAt"],
    }
    return spec


def test_schema_itself_and_legacy_examples_remain_valid() -> None:
    value = validator("recipe.schema.json")
    value.check_schema(value.schema)
    assert errors("adapter-spec.schema.json", base(1)) == []
    assert errors("adapter-spec.schema.json", base(2)) == []


def test_v2_nested_composition_does_not_become_a_v1_feature() -> None:
    spec = nested()
    assert errors("adapter-spec.schema.json", spec) == []
    spec["recipe"]["version"] = 1
    assert errors("adapter-spec.schema.json", spec)


@pytest.mark.parametrize(
    "mutation", ["both_sources", "depth", "alias", "timezone_length", "unknown_compose"]
)
def test_bounded_collection_and_expression_shape(mutation: str) -> None:
    spec = nested()
    response = spec["recipe"]["endpoints"][0]["response"]
    if mutation == "both_sources":
        response["itemsPath"] = "/items"
    elif mutation == "depth":
        response["collections"] *= 3
    elif mutation == "alias":
        response["collections"][0]["as"] = "root"
    elif mutation == "timezone_length":
        response["fields"]["startsAt"]["compose"]["timeZone"] = "x" * 101
    else:
        response["fields"]["startsAt"]["compose"]["type"] = "eval"
    assert errors("adapter-spec.schema.json", spec)


def test_executor_auth_has_no_credential_value_and_requires_a_request_binding() -> None:
    spec = base()
    spec["recipe"]["auth"] = {
        "type": "session",
        "version": 1,
        "bindings": {"bCookiesBearer": {"cookies": True, "bearer": "observedAuthorization"}},
    }
    assert errors("adapter-spec.schema.json", spec)
    request = spec["recipe"]["endpoints"][0]["request"]
    request["authBinding"] = "bCookiesBearer"
    assert errors("adapter-spec.schema.json", spec) == []
    spec["recipe"]["auth"]["bindings"]["bCookiesBearer"]["token"] = "forbidden-fixture"
    assert errors("adapter-spec.schema.json", spec)


def test_browser_plan_requires_rendered_get_and_verified_data_controls() -> None:
    spec = base()
    endpoint = spec["recipe"]["endpoints"][0]
    endpoint["response"] = {
        "format": "html",
        "itemsSelector": ".lesson",
        "fields": {"title": {"selector": ".title"}},
    }
    step = {
        "op": "set_date",
        "target": {"selector": "#date", "tag": "input"},
        "value": "{weekStart}",
        "region": {"selector": "#schedule", "tag": "section"},
    }
    endpoint["request"]["browserPlan"] = {"version": 1, "steps": [step]}
    assert errors("adapter-spec.schema.json", spec)
    endpoint["request"]["render"] = True
    assert errors("adapter-spec.schema.json", spec) == []
    step["value"] = "2026-10-29"
    assert errors("adapter-spec.schema.json", spec)
    step["value"] = "{today}"
    step.pop("region")
    assert errors("adapter-spec.schema.json", spec)


def test_weekly_expansion_is_bounded_and_schedule_only() -> None:
    spec = base()
    endpoint = spec["recipe"]["endpoints"][0]
    endpoint["response"] = {
        "format": "json",
        "itemsPath": "/series",
        "fields": {"externalId": {"path": "/id"}, "title": {"path": "/title"}},
        "expand": {
            "type": "weekly",
            "timeZone": "Europe/Moscow",
            "horizon": {"from": "{weekStart}", "days": 56},
            "startDate": {"path": "/from"},
            "untilDate": {"path": "/until"},
            "weekdays": {"path": "/weekdays"},
            "startTime": {"path": "/start"},
            "durationMinutes": 90,
        },
    }
    assert errors("adapter-spec.schema.json", spec) == []
    invalid = copy.deepcopy(spec)
    invalid["recipe"]["endpoints"][0]["kind"] = "deadlines"
    assert errors("adapter-spec.schema.json", invalid)
    expand = endpoint["response"]["expand"]
    expand["intervalWeeks"] = 2
    assert errors("adapter-spec.schema.json", spec)
    expand["anchorDate"] = {"path": "/anchor"}
    assert errors("adapter-spec.schema.json", spec) == []
    expand["endTime"] = {"path": "/end"}
    assert errors("adapter-spec.schema.json", spec)


@pytest.mark.parametrize("name", ["date-window-recipe.json", "weekly-rules-recipe.json"])
def test_v2_examples_pass_and_fail_both_validators(name: str) -> None:
    from pathlib import Path

    from pydantic import ValidationError

    from botai.contracts.specs import RecipeSpec

    examples = Path(__file__).resolve().parents[3] / "contracts" / "examples"
    for spec in json.loads((examples / "valid" / name).read_text("utf-8")):
        assert errors("adapter-spec.schema.json", spec) == []
        RecipeSpec.model_validate(spec)
    for spec in json.loads((examples / "invalid" / name).read_text("utf-8")):
        assert errors("adapter-spec.schema.json", spec), spec
        with pytest.raises(ValidationError):
            RecipeSpec.model_validate(spec)
