"""Apply a user's durable read scope to a copy of a shared declarative recipe."""

from __future__ import annotations

import copy
from typing import Any, cast

from botai.contracts.specs import Kind
from botai.sources.vocab import KIND_LABELS


def read_scope(raw: list[str] | None) -> tuple[Kind, ...] | None:
    if raw is None:
        return None
    # Unknown values fail closed: they cannot expand a connection to all kinds.
    return tuple(dict.fromkeys(cast(Kind, value) for value in raw if value in KIND_LABELS))


def scoped_spec(spec: dict[str, Any], scope: tuple[Kind, ...] | None) -> dict[str, Any]:
    if scope is None or spec.get("adapter") != "recipe":
        return spec
    result = copy.deepcopy(spec)
    recipe = result["recipe"]
    recipe["endpoints"] = [entry for entry in recipe["endpoints"] if entry["kind"] in scope]
    recipe["validation"]["requireKinds"] = [
        kind for kind in recipe["validation"]["requireKinds"] if kind in scope
    ]
    return result
