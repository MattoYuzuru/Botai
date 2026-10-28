"""User-specific read restrictions never mutate a shared declarative specification."""

from __future__ import annotations

import copy

from botai.sources.scope import read_scope, scoped_spec
from tests.sources import recipes


def test_scope_filters_endpoint_reads_and_validation_without_changing_the_shared_recipe() -> None:
    spec = recipes.recipe_spec()
    original = copy.deepcopy(spec)
    effective = scoped_spec(spec, ("scheduleEvents",))
    assert [entry["kind"] for entry in effective["recipe"]["endpoints"]] == ["scheduleEvents"]
    assert effective["recipe"]["validation"]["requireKinds"] == ["scheduleEvents"]
    assert spec == original


def test_automatic_and_legacy_connections_keep_the_declared_recipe() -> None:
    spec = recipes.recipe_spec()
    assert scoped_spec(spec, None) == spec
    assert read_scope(None) is None


def test_unknown_or_empty_scopes_fail_closed_instead_of_importing_all_kinds() -> None:
    assert read_scope(["unknown", "scheduleEvents", "scheduleEvents"]) == ("scheduleEvents",)
    assert read_scope(["unknown"]) == ()
    assert scoped_spec(recipes.recipe_spec(), ())["recipe"]["endpoints"] == []


def test_scope_removes_unneeded_bootstrap_reads_but_preserves_transitive_dependencies() -> None:
    spec = recipes.recipe_spec()
    spec["recipe"]["endpoints"][0]["request"]["path"] = "/api/{accountId}/schedule"
    spec["recipe"]["endpoints"][1]["request"]["path"] = "/api/{gradeScope}/deadlines"
    spec["recipe"]["bootstrap"] = [
        {"id": "root", "method": "GET", "path": "/root", "extract": {"rootId": "/id"}},
        {
            "id": "account",
            "method": "GET",
            "path": "/account/{rootId}",
            "extract": {"accountId": "/id"},
        },
        {"id": "grades", "method": "GET", "path": "/grades", "extract": {"gradeScope": "/id"}},
    ]
    original = copy.deepcopy(spec)
    effective = scoped_spec(spec, ("scheduleEvents",))
    assert [step["id"] for step in effective["recipe"]["bootstrap"]] == ["root", "account"]
    assert spec == original


def test_optional_selected_kind_still_has_valid_validation_metadata() -> None:
    spec = recipes.recipe_spec(require=("deadlines",))
    effective = scoped_spec(spec, ("scheduleEvents",))
    assert effective["recipe"]["validation"]["requireKinds"] == ["scheduleEvents"]
