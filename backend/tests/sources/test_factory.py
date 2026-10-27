from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapters.document import DocumentAdapter
from botai.sources.adapters.ical import IcalAdapter
from botai.sources.adapters.moodle_ws import MoodleWsAdapter
from botai.sources.adapters.recipe import RecipeAdapter
from botai.sources.factory import ADAPTER_KINDS, AdapterFactory, parse_spec

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((EXAMPLES / name / "adapter-spec.json").read_text())


def test_valid_contract_examples_map_to_their_adapters() -> None:
    specs = load("valid")
    adapters = [AdapterFactory().create(spec) for spec in specs]
    assert [type(a) for a in adapters] == [
        RecipeAdapter,
        MoodleWsAdapter,
        IcalAdapter,
        DocumentAdapter,
    ]
    assert [a.kind for a in adapters] == list(ADAPTER_KINDS)


def test_an_unknown_adapter_type_cannot_be_created() -> None:
    bad = load("invalid")[2]  # {"adapter": "python", "code": "import os"}
    with pytest.raises(BotaiError) as exc:
        AdapterFactory().create(bad)
    assert exc.value.code == ErrorCode.RECIPE_REJECTED


def test_a_spec_with_extra_fields_is_refused_by_the_typed_view() -> None:
    spec = load("valid")[2] | {"script": "rm -rf /"}
    with pytest.raises(BotaiError):
        parse_spec(spec)
