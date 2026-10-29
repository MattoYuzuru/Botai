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
