"""Adapter factory (ADR-0005): the spec's `adapter` field selects one of four fixed executors.

A new university or a new document is a new spec, never new code. A new adapter type is a rare
event and needs an ADR.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from botai.contracts.specs import (
    AdapterSpec,
    DocumentSpec,
    IcalSpec,
    MoodleWsSpec,
    RecipeSpec,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapter import SourceAdapter
from botai.sources.adapters.document import DocumentAdapter
from botai.sources.adapters.ical import IcalAdapter
from botai.sources.adapters.moodle_ws import MoodleWsAdapter
from botai.sources.adapters.recipe import RecipeAdapter

_SPEC: TypeAdapter[AdapterSpec] = TypeAdapter(AdapterSpec)

ADAPTER_KINDS: tuple[str, ...] = ("recipe", "moodle_ws", "ical", "document")


def parse_spec(data: dict[str, Any]) -> AdapterSpec:
    """Typed view of a stored spec. Stored specs have already passed the lint."""
    try:
        return _SPEC.validate_python(data)
    except ValidationError as exc:
        raise BotaiError(ErrorCode.RECIPE_REJECTED, "Спецификация не соответствует схеме") from exc


class AdapterFactory:
    def create(self, spec: dict[str, Any] | AdapterSpec) -> SourceAdapter:
        parsed = parse_spec(spec) if isinstance(spec, dict) else spec
        match parsed:
            case RecipeSpec():
                return RecipeAdapter(parsed)
            case MoodleWsSpec():
                return MoodleWsAdapter(parsed)
            case IcalSpec():
                return IcalAdapter(parsed)
            case DocumentSpec():
                return DocumentAdapter(parsed)
