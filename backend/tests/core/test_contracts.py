"""Contracts: JSON Schema and Pydantic mirrors accept the same valid examples and reject invalid ones."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from botai.contracts import schemas
from botai.contracts.agent_ui import AgentUiBlock
from botai.contracts.specs import AdapterSpec, LoginPreset

ROOT = Path(__file__).resolve().parents[3] / "contracts"
EXAMPLES = ROOT / "examples"

CASES = [
    ("agent-ui.blocks.json", "agent-ui.schema.json", TypeAdapter(AgentUiBlock)),
    ("adapter-spec.json", "adapter-spec.schema.json", TypeAdapter(AdapterSpec)),
    ("login-preset.json", "login-preset.schema.json", TypeAdapter(LoginPreset)),
]

# v4 contracts have no Pydantic mirror in `botai.contracts` yet (the runtime uses
# `agent.v4.protocol`, sources use `sources.v4`), so only the JSON Schema side is checked here.
SCHEMA_CASES = [
    ("tools.contract.json", "tools.schema.json"),
    ("run.trace.json", "run.schema.json"),
    ("playbook.json", "playbook.schema.json"),
]


def _load(kind: str, name: str) -> list[object]:
    return json.loads((EXAMPLES / kind / name).read_text("utf-8"))


def test_packaged_schemas_match_repo_copies() -> None:
    packaged = Path(schemas.__file__).parent / "schemas"
    for f in ROOT.glob("*.schema.json"):
        assert (packaged / f.name).read_text("utf-8") == f.read_text("utf-8"), (
            f"run `make gen`: {f.name} drifted"
        )


@pytest.mark.parametrize(("example", "schema", "adapter"), CASES)
def test_valid_examples_pass_both(example: str, schema: str, adapter: TypeAdapter[object]) -> None:
    for item in _load("valid", example):
        assert schemas.errors(schema, item) == [], item
        adapter.validate_python(item)


@pytest.mark.parametrize(("example", "schema", "adapter"), CASES)
def test_invalid_examples_fail_schema(
    example: str, schema: str, adapter: TypeAdapter[object]
) -> None:
    for item in _load("invalid", example):
        assert schemas.errors(schema, item), item


@pytest.mark.parametrize(("example", "schema"), SCHEMA_CASES)
def test_v4_valid_examples_pass_schema(example: str, schema: str) -> None:
    for item in _load("valid", example):
        assert schemas.errors(schema, item) == [], item


@pytest.mark.parametrize(("example", "schema"), SCHEMA_CASES)
def test_v4_invalid_examples_fail_schema(example: str, schema: str) -> None:
    for item in _load("invalid", example):
        assert schemas.errors(schema, item), item


def test_every_contract_schema_is_registered() -> None:
    """A new schema file must be added to `schemas.SCHEMA_FILES`, so CI can reference it."""
    on_disk = {path.name for path in ROOT.glob("*.schema.json")}
    registered = set(schemas.SCHEMA_FILES)
    assert on_disk - registered == set(), (
        f"add to botai.contracts.schemas.SCHEMA_FILES: {sorted(on_disk - registered)}"
    )
    assert registered - on_disk == set(), f"missing schema files: {sorted(registered - on_disk)}"


def test_block_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(AgentUiBlock).validate_python(
            {"type": "script", "id": "01JAAAAAAAAAAAAAAAAAAAAAA3", "at": "2026-10-27T10:00:00Z"}
        )


def test_block_serializes_camel_case() -> None:
    block = TypeAdapter(AgentUiBlock).validate_python(_load("valid", "agent-ui.blocks.json")[2])
    dumped = block.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert dumped["inputKind"] == "password"
    assert schemas.errors("agent-ui.schema.json", dumped) == []


def test_render_contract_requires_get_html_without_request_body() -> None:
    from copy import deepcopy

    from botai.contracts.specs import RecipeSpec
    from botai.sources.adapters.recipe import RecipeAdapter

    valid = _load("valid", "rendered-recipe.json")[0]
    spec = cast(dict[str, Any], deepcopy(valid))
    recipe = spec["recipe"]
    endpoint = recipe["endpoints"][0]
    endpoint["request"] = {"method": "GET", "path": "/calendar", "render": True}
    endpoint["response"] = {
        "format": "html",
        "itemsSelector": "article",
        "fields": {"title": {"selector": "h2"}},
    }
    assert schemas.errors("adapter-spec.schema.json", spec) == []
    parsed = RecipeSpec.model_validate(spec)
    assert RecipeAdapter(parsed).recipe.endpoints[0].request.render
    for request, response in (
        (
            {"method": "POST", "path": "/calendar", "render": True, "readOnlyPost": True},
            endpoint["response"],
        ),
        ({"method": "GET", "path": "/calendar", "render": True, "body": {}}, endpoint["response"]),
        (
            endpoint["request"],
            {"format": "json", "itemsPath": "/items", "fields": {"title": {"path": "/title"}}},
        ),
    ):
        bad = deepcopy(spec)
        bad["recipe"]["endpoints"][0].update(request=request, response=response)
        assert schemas.errors("adapter-spec.schema.json", bad)
        with pytest.raises(ValidationError):
            RecipeSpec.model_validate(bad)
