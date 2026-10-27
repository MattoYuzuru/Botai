"""Shape checks of answers to agent blocks (jobs/answers._check_values)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from botai.contracts.agent_ui import ChoiceBlock, ChoiceOption
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.jobs.answers import _check_values  # pyright: ignore[reportPrivateUsage]

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _choice(*, mode: str, required: bool) -> ChoiceBlock:
    return ChoiceBlock(
        id=new_id(),
        at=NOW,
        question="?",
        mode=mode,
        required=required,  # type: ignore[arg-type]
        options=[ChoiceOption(value="a", label="А"), ChoiceOption(value="b", label="Б")],
    )


@pytest.mark.parametrize("skip", [[], "", [""]])
def test_optional_single_choice_can_be_skipped(skip: object) -> None:
    _check_values(_choice(mode="single", required=False), skip)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [[], "", ["a", "b"], ["zzz"]])
def test_required_single_choice_needs_exactly_one_known_value(bad: object) -> None:
    with pytest.raises(BotaiError):
        _check_values(_choice(mode="single", required=True), bad)  # type: ignore[arg-type]


def test_multi_choice_accepts_several_and_optional_empty() -> None:
    _check_values(_choice(mode="multi", required=True), ["a", "b"])
    _check_values(_choice(mode="multi", required=False), [])
