"""Only an observed successful read can choose an executor-owned authentication policy."""

from __future__ import annotations

from typing import Any

import pytest

from botai.core.browser import ObservedRequest
from botai.learn.auth_bindings import BindingEvidenceError, compile_auth_binding

ORIGIN = "https://fakeuni.test"


def observed(**changes: Any) -> ObservedRequest:
    return ObservedRequest.model_validate(
        {
            "evidence_id": "r1",
            "method": "GET",
            "url": ORIGIN + "/api/students/private-id/calendar?from=2026-10-26",
            "status": 200,
            "has_cookie": True,
            "has_authorization": True,
            "authorization_scheme": "bearer",
            "auth_proven": True,
            **changes,
        }
    )


def compile_policy(fact: ObservedRequest, **changes: Any) -> dict[str, Any]:
    return compile_auth_binding(
        {"r1": fact},
        changes.pop("evidence_id", "r1"),
        origin=changes.pop("origin", ORIGIN),
        method=changes.pop("method", "GET"),
        path=changes.pop("path", "/api/students/{studentId}/calendar"),
        query=changes.pop("query", {"from": "{weekStart}"}),
        **changes,
    )


def test_binding_preserves_combined_credentials_without_private_values_or_references() -> None:
    fact = observed(response_sample="private-body-token")
    assert compile_policy(fact) == {"cookies": True, "bearer": "observedAuthorization"}
    assert "private" not in repr(compile_policy(fact))
    assert "r1" not in repr(compile_policy(fact))


@pytest.mark.parametrize(
    ("fact_changes", "request_changes", "reason"),
    [
        ({}, {"evidence_id": "invented"}, "evidence_unknown"),
        ({"auth_proven": False}, {}, "evidence_auth_unproven"),
        ({"status": 401}, {}, "evidence_response_unverified"),
        ({"status": None}, {}, "evidence_response_unverified"),
        ({}, {"origin": "https://id.fakeuni.test"}, "evidence_origin_mismatch"),
        ({}, {"origin": "https://fakeuni.test:444"}, "evidence_origin_mismatch"),
        ({}, {"method": "POST"}, "evidence_method_mismatch"),
        ({}, {"path": "/api/other/{studentId}/calendar"}, "evidence_path_mismatch"),
        ({}, {"path": "/api/{studentId}"}, "evidence_path_mismatch"),
        ({}, {"query": {"until": "{weekStart}"}}, "evidence_query_mismatch"),
        ({}, {"query": {"from": "2026-11-01"}}, "evidence_query_mismatch"),
        ({"authorization_scheme": "other"}, {}, "evidence_auth_scheme_unsupported"),
        ({"has_authorization": False}, {}, "evidence_auth_inconsistent"),
    ],
)
def test_unverified_or_different_requests_do_not_inherit_credentials(
    fact_changes: dict[str, Any], request_changes: dict[str, Any], reason: str
) -> None:
    with pytest.raises(BindingEvidenceError) as error:
        compile_policy(observed(**fact_changes), **request_changes)
    assert str(error.value) == error.value.code == reason
    assert "private-id" not in str(error.value)


def test_proven_public_and_cookie_only_requests_do_not_invent_bearer_authentication() -> None:
    assert compile_policy(observed(has_authorization=False, authorization_scheme="none")) == {
        "cookies": True,
        "bearer": "none",
    }
    assert compile_policy(
        observed(has_cookie=False, has_authorization=False, authorization_scheme="none")
    ) == {"cookies": False, "bearer": "none"}


@pytest.mark.parametrize(
    "path", ["//other.test/calendar", "/api/../calendar", "/api/%2e%2e/calendar"]
)
def test_generalization_cannot_create_an_absolute_or_traversing_path(path: str) -> None:
    with pytest.raises(BindingEvidenceError, match="evidence_path_mismatch"):
        compile_policy(observed(), path=path)


def test_duplicate_query_parameters_are_ambiguous_for_a_recipe_mapping() -> None:
    with pytest.raises(BindingEvidenceError, match="evidence_query_mismatch"):
        compile_policy(observed(url=ORIGIN + "/api/students/private-id/calendar?from=one&from=two"))


# --- a key that repeats in the address, and words that name the current student --------------

TASKS = "/api/lms/tasks/student?state=backlog&state=inProgress&state=review&limit=50"


def tasks_fact() -> ObservedRequest:
    return observed(url=ORIGIN + TASKS, has_authorization=False, authorization_scheme="none")


def tasks_policy(query: dict[str, Any] | None) -> dict[str, Any]:
    return compile_policy(tasks_fact(), path="/api/lms/tasks/student", query=query)


def test_a_repeated_key_is_a_list_of_every_value_in_any_order() -> None:
    states = ["backlog", "inProgress", "review"]
    expected = {"cookies": True, "bearer": "none"}
    assert tasks_policy({"state": states, "limit": "50"}) == expected
    assert tasks_policy({"state": list(reversed(states)), "limit": "50"}) == expected


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "50"},  # the key is omitted
        {"state": "backlog", "limit": "50"},  # one of three values
        {"state": ["backlog", "inProgress"], "limit": "50"},  # fewer than sent
        {"state": ["backlog", "inProgress", "review", "failed"], "limit": "50"},  # more than sent
        {"state": ["backlog", "inProgress", "failed"], "limit": "50"},  # another value
        {"state": ["backlog", "backlog", "review"], "limit": "50"},  # a value twice
        {"state": ["backlog", "inProgress", "review"], "limit": ["50", "50"]},
        {"state": ["backlog", "inProgress", "review"]},  # a key less
    ],
)
def test_the_keys_of_the_query_are_compared_as_a_multiset(query: dict[str, Any]) -> None:
    with pytest.raises(BindingEvidenceError) as error:
        tasks_policy(query)
    assert error.value.code == "evidence_query_mismatch"


def test_the_finding_names_keys_and_counts_never_values() -> None:
    with pytest.raises(BindingEvidenceError) as error:
        tasks_policy({"state": "backlog", "limit": "50"})
    assert error.value.detail == (
        "в наблюдённом запросе ключи query: limit×1, state×3; в описании: limit×1, state×1"
    )
    for value in ("backlog", "inProgress", "50"):
        assert value not in (error.value.detail or "")


def test_a_template_takes_one_value_of_a_repeated_key() -> None:
    fact = observed(url=ORIGIN + "/api/x?day=2026-10-26&day=2026-10-27")
    assert compile_policy(fact, path="/api/x", query={"day": ["{weekStart}", "2026-10-27"]})
    with pytest.raises(BindingEvidenceError, match="evidence_query_mismatch"):
        compile_policy(fact, path="/api/x", query={"day": ["{weekStart}", "{weekStart}x"]})


@pytest.mark.parametrize("word", ["me", "Me", "self", "current", "my", "own"])
def test_a_placeholder_never_replaces_a_word_for_the_current_student(word: str) -> None:
    fact = observed(
        url=f"{ORIGIN}/api/lms/students/{word}/timetables",
        has_authorization=False,
        authorization_scheme="none",
    )
    ok = compile_auth_binding(
        {"r1": fact}, "r1", origin=ORIGIN, method="GET", path=f"/api/lms/students/{word}/timetables"
    )
    assert ok == {"cookies": True, "bearer": "none"}
    with pytest.raises(BindingEvidenceError) as error:
        compile_auth_binding(
            {"r1": fact},
            "r1",
            origin=ORIGIN,
            method="GET",
            path="/api/lms/students/{accountId}/timetables",
        )
    assert error.value.code == "evidence_path_literal_replaced"
    assert error.value.detail is not None and "№4" in error.value.detail


def test_a_placeholder_still_generalises_an_identifier() -> None:
    fact = observed(url=f"{ORIGIN}/api/lms/students/4f2a9c/timetables")
    assert compile_auth_binding(
        {"r1": fact},
        "r1",
        origin=ORIGIN,
        method="GET",
        path="/api/lms/students/{accountId}/timetables",
    )
