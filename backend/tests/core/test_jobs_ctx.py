from __future__ import annotations

from datetime import UTC, datetime

import pytest

from botai.contracts.agent_ui import ChoiceBlock, ChoiceOption, InputBlock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext, JobCancelled

NOW = datetime(2026, 10, 27, tzinfo=UTC)


async def test_ask_and_ask_secret() -> None:
    def responder(block: object) -> str | list[str] | None:
        if isinstance(block, InputBlock):
            return "P@ssw0rd-FIXTURE"
        if isinstance(block, ChoiceBlock):
            return ["c1"]
        return None

    ctx = InMemoryJobContext(responder=responder)
    choice = ChoiceBlock(
        id=new_id(),
        at=NOW,
        question="Твои предметы?",
        mode="multi",
        required=True,
        options=[ChoiceOption(value="c1", label="А"), ChoiceOption(value="c2", label="Б")],
    )
    assert (await ctx.ask(choice)).values == ["c1"]
    pwd = InputBlock(
        id=new_id(), at=NOW, question="Пароль", input_kind="password", required=True, secret=True
    )
    assert (await ctx.ask_secret(pwd)).get_secret_value() == "P@ssw0rd-FIXTURE"
    with pytest.raises(ValueError, match="ask_secret"):
        await ctx.ask(pwd)


async def test_unanswered_block_cancels() -> None:
    ctx = InMemoryJobContext()
    block = InputBlock(id=new_id(), at=NOW, question="?", input_kind="text", required=True)
    with pytest.raises(JobCancelled):
        await ctx.ask(block)
