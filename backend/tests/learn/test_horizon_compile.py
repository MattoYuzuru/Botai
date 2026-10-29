"""Date windows, interface addresses and card sections pass the compiler and the lint as data."""

from __future__ import annotations

import copy
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.core.browser import ObservedRequest
from botai.learn.author import build_spec
from botai.learn.compiler import FactoryCompileError, compile_candidate, ui_url_for
from botai.learn.lint import LintContext, lint_spec
from tests.sources.helpers import NOW
from tests.sources.test_date_window import endpoint as window_endpoint

ORIGIN = "https://fakeuni.test"
TZ = ZoneInfo("Europe/Moscow")
API = "/api/get-myschedule"


def observed(begin: str = "2026-10-26", page: str | None = "/schedule?begin_date=2026-10-26"):
    return ObservedRequest.model_validate(
        {
            "url": f"{ORIGIN}{API}?begin_date={begin}",
            "method": "GET",
            "status": 200,
            "resource_type": "fetch",
            "auth_proven": True,
            "has_cookie": True,
            "has_authorization": False,
            "authorization_scheme": "none",
            "initiator_url": f"{ORIGIN}{page}" if page else None,
        }
    )


def authored(**changes: Any) -> dict[str, Any]:
    candidate = window_endpoint()
    candidate.pop("uiUrl")  # the author never writes it: the compiler derives it
    candidate["requestEvidenceId"] = "r1"
    candidate.update(changes)
    return candidate


def compiled(candidate: dict[str, Any], fact: ObservedRequest | None = None):
    return compile_candidate(
        candidate,
        [],
        origin=ORIGIN,
        request_facts={"r1": fact or observed()},
        browser_plans={},
        browser_plan_urls={},
        now=NOW,
        time_zone=TZ,
    )


def spec_of(result: Any) -> dict[str, Any]:
    return build_spec(
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="a" * 64,
        endpoints=[result.endpoint],
        bootstrap=result.bootstrap,
        auth=result.auth,
        require_kinds=["scheduleEvents"],
    )


def test_window_zero_reproduces_the_observation_and_the_page_address_is_derived() -> None:
    result = compiled(authored())
    assert result.endpoint["pagination"]["type"] == "dateWindow"
    assert result.endpoint["uiUrl"] == {
        "path": "/schedule",
        "query": {"begin_date": "{weekStart}"},
    }
    spec = spec_of(result)
    assert spec["recipe"]["version"] == 2
    lint = lint_spec(spec, LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN))
    assert lint.ok, lint.reasons()


def test_an_observation_of_a_neighbour_window_also_certifies_the_walk() -> None:
    compiled(authored(), observed("2026-11-02", "/schedule?begin_date=2026-11-02"))
    with pytest.raises(FactoryCompileError) as outside:  # five weeks ahead: not in the walk
        compiled(authored(), observed("2026-12-07"))
    assert outside.value.code == "window_template_mismatch"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda e: e["pagination"].update(param="other"), "window_param_missing"),
        (
            lambda e: e["request"]["query"].update(begin_date="2026-10-26"),
            "window_template_unsupported",
        ),
        (
            lambda e: e["request"]["query"].update(begin_date="{userId}"),
            "window_template_unsupported",
        ),
    ],
)
def test_a_window_that_cannot_be_certified_is_refused_with_a_static_code(
    change: Any, code: str
) -> None:
    candidate = authored()
    change(candidate)
    with pytest.raises(FactoryCompileError) as caught:
        compiled(candidate)
    assert caught.value.code in {code, "evidence_query_mismatch"}


def test_the_author_cannot_set_the_interface_address() -> None:
    with pytest.raises(FactoryCompileError) as caught:
        compiled(authored(uiUrl={"path": "/anywhere"}))
    assert caught.value.code == "factory_execution_fields_forbidden"


@pytest.mark.parametrize(
    "page",
    [
        "/student/7421963/schedule?begin_date=2026-10-26",  # an identifier in the path
        "/",  # the site root says nothing about the data
        None,  # the initiator was not observed
    ],
)
def test_no_address_is_better_than_a_personal_or_empty_one(page: str | None) -> None:
    ui = ui_url_for(observed(page=page), authored()["request"], ORIGIN, now=NOW, tz=TZ)
    assert ui is None


def test_query_values_are_generalised_or_left_out() -> None:
    fact = observed(
        page="/timetable/week?from=2026-10-26&to=2026-11-01&group=7421963&mode=list&day=2026-10-29"
    )
    request = {"query": {"from": "{weekStart}", "to": "{plusDays:5}"}}
    ui = ui_url_for(fact, request, ORIGIN, now=NOW, tz=TZ)
    assert ui == {
        "path": "/timetable/week",
        "query": {
            "from": "{weekStart}",
            "to": "{plusDays:5}",
            "mode": "list",
            "day": "{plusDays:2}",
        },
    }


def lint(candidate: dict[str, Any]) -> set[str]:
    spec = spec_of(compiled(candidate))
    return lint_spec(
        spec, LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN)
    ).codes()


def test_lint_guards_the_hand_written_parts_of_a_window_and_card() -> None:
    single = authored()
    single["pagination"].update(windows=1, startOffset=0)
    assert "pagination.window_count" in lint(single)

    structured = authored()
    structured["response"]["fields"]["links"] = {"path": "/url1"}
    assert "field.unknown" in lint(structured)

    secret_label = authored()
    secret_label["response"]["attributes"][0]["path"] = "/session_token"
    assert "pointer.secret" in lint(secret_label)


def test_a_label_copied_from_the_data_is_an_observed_value() -> None:
    candidate = authored()
    candidate["response"]["attributes"][0]["label"] = "ДРИП 3 курс 26/27"
    spec = spec_of(compiled(candidate))
    ctx = LintContext(
        allowed_hosts=["fakeuni.test"],
        expected_origin=ORIGIN,
        observed=["ДРИП 3 курс 26/27"],
    )
    assert "observed.value" in lint_spec(spec, ctx).codes()


def test_a_personal_interface_address_is_rejected_by_the_lint_itself() -> None:
    result = compiled(authored())
    spec = spec_of(result)
    endpoint = spec["recipe"]["endpoints"][0]
    endpoint["uiUrl"] = {"path": "/student/7421963/schedule", "query": {"group": "7421963"}}
    codes = lint_spec(
        copy.deepcopy(spec), LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN)
    ).codes()
    assert "uiurl.personal" in codes
