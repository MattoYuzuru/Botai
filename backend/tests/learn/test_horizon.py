"""When a recipe may be read as a walk over windows, and how far apart the windows are."""

from __future__ import annotations

import copy
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.learn.author import build_spec
from botai.learn.horizon import walked
from botai.learn.lint import LintContext, lint_spec
from tests.sources.helpers import NOW
from tests.sources.test_date_window import endpoint as window_endpoint

TZ = ZoneInfo("Europe/Moscow")
ORIGIN = "https://fakeuni.test"


def plain(query: dict[str, str], **response: Any) -> dict[str, Any]:
    candidate = window_endpoint()
    candidate.pop("pagination")
    candidate.pop("uiUrl")
    candidate["request"]["query"] = query
    candidate["response"].update(response)
    return candidate


def step_of(query: dict[str, str]) -> int:
    wide = walked(plain(query), NOW, TZ)
    assert wide is not None
    return wide["pagination"]["stepDays"]


def test_week_start_steps_by_whole_weeks_and_a_range_by_its_inclusive_length() -> None:
    assert step_of({"begin_date": "{weekStart}"}) == 7
    assert step_of({"from": "{weekStart}", "to": "{plusDays:6}"}) == 7  # snaps to Mondays
    assert step_of({"from": "{today}", "to": "{plusDays:6}"}) == 7  # 7 days, both ends included
    assert step_of({"from": "{today}", "to": "{plusDays:7}"}) == 8  # 8 days, not 7 and not 6
    assert step_of({"from": "{today}", "to": "{plusDays:13}"}) == 14
    assert step_of({"day": "{today}"}) == 7


@pytest.mark.parametrize(
    "response",
    [
        {"identity": ["title"]},  # the weeks of a repeating timetable would collapse
        {"identity": None},  # a bare source id says nothing about which week
    ],
)
def test_a_recipe_whose_identity_ignores_the_time_is_not_widened(response: dict[str, Any]) -> None:
    candidate = plain({"begin_date": "{weekStart}"})
    if response["identity"] is None:
        candidate["response"].pop("identity")
    else:
        candidate["response"]["identity"] = response["identity"]
    assert walked(candidate, NOW, TZ) is None


def lint_codes(candidate: dict[str, Any]) -> set[str]:
    spec = build_spec(
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="a" * 64,
        endpoints=[candidate],
        bootstrap=[],
        auth={"type": "cookie"},
        require_kinds=["scheduleEvents"],
    )
    return lint_spec(
        spec, LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN)
    ).codes()


def test_lint_requires_whole_weeks_for_week_start_and_the_time_in_the_identity() -> None:
    assert lint_codes(window_endpoint()) == set()
    odd = window_endpoint()
    odd["pagination"]["stepDays"] = 5
    assert "pagination.window_step" in lint_codes(odd)
    month = window_endpoint()
    month["request"]["query"]["begin_date"] = "{today}"
    month["pagination"]["stepDays"] = 5
    assert "pagination.window_step" not in lint_codes(month)  # no Monday snapping to repeat
    collapsing = copy.deepcopy(window_endpoint())
    collapsing["response"]["identity"] = ["title"]
    assert "pagination.window_identity" in lint_codes(collapsing)
