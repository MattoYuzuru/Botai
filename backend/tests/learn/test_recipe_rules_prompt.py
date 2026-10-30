"""The rules that the incident taught the author reach every request that writes a recipe."""

from __future__ import annotations

import pytest

from botai.learn import prompting
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
