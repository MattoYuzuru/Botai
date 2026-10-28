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


def test_render_contract_is_get_html_and_execution_fails_closed() -> None:
    from copy import deepcopy

    from botai.contracts.specs import RecipeSpec
    from botai.core.errors import BotaiError, ErrorCode
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
    with pytest.raises(BotaiError) as exc:
        RecipeAdapter(parsed)
    assert exc.value.code == ErrorCode.RECIPE_REJECTED
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
