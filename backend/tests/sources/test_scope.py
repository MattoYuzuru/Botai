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
