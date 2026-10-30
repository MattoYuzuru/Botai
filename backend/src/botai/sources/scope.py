"""Apply a user's durable read scope to a copy of a shared declarative recipe."""

from __future__ import annotations

import copy
from typing import Any

from botai.contracts.specs import Kind
from botai.sources.templates import placeholders
from botai.sources.vocab import KIND_LABELS


def read_scope(raw: list[str] | None) -> tuple[Kind, ...] | None:
    if raw is None:
        return None
    # Unknown values fail closed: they cannot expand a connection to all kinds.
    return tuple(dict.fromkeys(value for value in raw if value in KIND_LABELS))


def covered_kinds(spec: dict[str, Any], scope: tuple[Kind, ...] | None) -> tuple[Kind, ...]:
    """The kinds a connection reads now: those of its recipe that its durable scope lets through."""
    if spec.get("adapter") != "recipe":
        return ()
    kinds: tuple[Kind, ...] = tuple(
        dict[Kind, None].fromkeys(entry["kind"] for entry in spec["recipe"]["endpoints"])
    )
    return tuple(kind for kind in kinds if scope is None or kind in scope)


def scoped_spec(spec: dict[str, Any], scope: tuple[Kind, ...] | None) -> dict[str, Any]:
    if scope is None or spec.get("adapter") != "recipe":
        return spec
    result = copy.deepcopy(spec)
    recipe = result["recipe"]
    recipe["endpoints"] = [entry for entry in recipe["endpoints"] if entry["kind"] in scope]
    recipe["validation"]["requireKinds"] = [
        kind for kind in recipe["validation"]["requireKinds"] if kind in scope
    ]
    if not recipe["validation"]["requireKinds"] and recipe["endpoints"]:
        recipe["validation"]["requireKinds"] = list(
            dict.fromkeys(entry["kind"] for entry in recipe["endpoints"])
        )
    # A shared recipe can have bootstrap reads solely for an excluded kind. Retain only
    # parameters the selected requests need, including dependencies between bootstrap steps.
    needed = {name for entry in recipe["endpoints"] for name in _references(entry["request"])}
    kept: list[dict[str, Any]] = []
    for step in reversed(recipe.get("bootstrap", [])):
        if needed.intersection(step["extract"]):
            kept.append(step)
            needed.update(placeholders(step["path"]))
    if "bootstrap" in recipe:
        recipe["bootstrap"] = list(reversed(kept))
    return result


def _references(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(placeholders(value))
    if isinstance(value, dict):
        return set().union(*(_references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value))
    return set()
