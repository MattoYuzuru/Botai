"""JSON Schema validators for contracts. The JSON files are copied from /contracts by `make gen`
(CI fails if the copy drifts). Specs written by the agent are validated here before anything else."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_FILES = (
    "agent-ui.schema.json",
    "recipe.schema.json",
    "login-preset.schema.json",
    "adapter-spec.schema.json",
    "domain.schema.json",
)


@lru_cache
def _registry() -> Registry:
    resources_: list[tuple[str, Resource[Any]]] = []
    for name in SCHEMA_FILES:
        text = resources.files("botai.contracts").joinpath("schemas", name).read_text("utf-8")
        doc = json.loads(text)
        res = Resource.from_contents(doc)
        resources_.append((doc["$id"], res))
        resources_.append((name, res))  # relative $ref like "recipe.schema.json"
    return Registry().with_resources(resources_)


@lru_cache
def validator(name: str) -> Draft202012Validator:
    text = resources.files("botai.contracts").joinpath("schemas", name).read_text("utf-8")
    schema = json.loads(text)
    return Draft202012Validator(schema, registry=_registry())


def errors(name: str, instance: Any) -> list[str]:
    """Human-readable validation errors, empty when valid. Never echoes instance values."""
    out: list[str] = []
    for err in sorted(validator(name).iter_errors(instance), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "<root>"
        out.append(f"{path}: {err.validator} violation")
    return out
