"""The rules that the incident taught the author reach every request that writes a recipe."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from botai.learn import prompting
from botai.learn.compiler import HINTS
from botai.sources.vocab import VOCAB
from tests.fakeuni import lms
from tests.sources import recipes

RULES = (
    "Оставь такой путь ровно как его отправила страница",  # `me` is a word, not an id
    "записывай списком всех значений в том же порядке",  # a key that repeats in the address
    "читай секцией `collections`",  # a list inside a list
    'разверни его секцией `expand` с `type: "weekly"`',  # a rule of repetition is not a date
)


@pytest.mark.parametrize("repair", [False, True], ids=["first-reading", "repair"])
def test_every_request_that_writes_a_recipe_carries_the_rules(repair: bool) -> None:
    previous = recipes.timetable_endpoint() if repair else None
    for kind in ("scheduleEvents", "deadlines"):
        text = prompting.task_prompt(kind, previous=previous, problem="empty" if repair else None)  # type: ignore[arg-type]
        for rule in RULES:
            assert rule in text, (repair, rule)
        assert "{{" not in text
        assert "`me`" in text


# --- the prompt does not hand the model the answer of the fixture -----------------------------


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return {str(k) for k in node} | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _declared_names() -> set[str]:
    """Names the recipe language and the domain vocabulary legitimately speak in."""
    names: set[str] = set()
    schema: dict[str, Any] = json.loads(
        (Path(__file__).parents[3] / "contracts" / "recipe.schema.json").read_text("utf-8")
    )
    names |= _keys(schema.get("$defs", {}))
    for fields in VOCAB.values():
        names |= set(fields)
    return names


def _production_text() -> str:
    """Everything the author reads that is not data of the source: prompts and the finding texts."""
    prompts = Path(prompting.__file__).parent / "prompts"
    parts = [file.read_text("utf-8") for file in sorted(prompts.glob("*.md"))]
    parts += [*HINTS.values(), prompting._factory_rules()]  # pyright: ignore[reportPrivateUsage]
    return "\n".join(parts)


def test_the_fixture_vocabulary_is_not_in_what_the_author_is_told() -> None:
    """The keys and address words that only the synthetic cabinet speaks in (`eventRows`,
    `calendarEvent`, `hosts`...) never appear in a prompt, a hint or a schema description: an
    example in them would be the answer, and the L2 run would measure copying, not reading."""
    fixture = _keys(lms.default_timetables()) | _keys(lms.default_tasks())
    fixture |= {
        word
        for path in (lms.TIMETABLES_PATH, lms.TASKS_PATH, lms.ME_PATH)
        for word in path.split("/")
        if word
    }
    fixture |= set(lms.STATES) | {"timetables", "lms", "students"}
    own = (
        fixture
        - _declared_names()
        - {"api", "account", "me", "id", "name", "type", "state", "student"}
    )
    assert {"eventRows", "calendarEvent", "hosts", "courseName", "timetables"} <= own
    text = _production_text()
    schema_text = (Path(__file__).parents[3] / "contracts" / "recipe.schema.json").read_text(
        "utf-8"
    )
    leaked = {
        word
        for word in own
        if re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text + schema_text)
    }
    assert not leaked, sorted(leaked)
