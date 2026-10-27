"""Blocks the learning jobs show to the student (contracts/agent-ui.schema.json).

Every stage is a `ThinkingBlock` that is emitted as running and re-emitted under the same id when
it finishes (a block is idempotent by id). Nothing here carries page contents or secrets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from botai.contracts.agent_ui import (
    AgentUiAnswer,
    ChoiceBlock,
    ChoiceOption,
    ConfirmBlock,
    InputBlock,
    SummaryBlock,
    SummaryGroup,
    SummaryItem,
    TextBlock,
    ThinkingBlock,
)
from botai.core.clock import Clock
from botai.core.ids import new_id
from botai.core.jobs import JobContext

State = Literal["running", "ok", "failed", "skipped"]


class Stage:
    """One line of the progress stream: running first, then ok, failed or skipped."""

    def __init__(self, ui: Ui, block_id: str, name: str, detail: str | None) -> None:
        self._ui, self.id, self.name, self.detail = ui, block_id, name, detail

    async def finish(self, state: State = "ok", detail: str | None = None) -> None:
        await self._ui.ctx.emit(
            ThinkingBlock(
                id=self.id,
                at=self._ui.clock.now(),
                stage=self.name[:120],
                detail=(detail if detail is not None else self.detail),
                state=state,
            )
        )


class Ui:
    def __init__(self, ctx: JobContext, clock: Clock) -> None:
        self.ctx = ctx
        self.clock = clock

    def _now(self) -> datetime:
        return self.clock.now()

    async def stage(self, name: str, detail: str | None = None) -> Stage:
        await self.ctx.status(name)
        block_id = new_id()
        await self.ctx.emit(
            ThinkingBlock(
                id=block_id, at=self._now(), stage=name[:120], detail=detail, state="running"
            )
        )
        return Stage(self, block_id, name, detail)

    async def note(self, name: str, detail: str, state: State = "ok") -> None:
        await self.ctx.emit(
            ThinkingBlock(
                id=new_id(), at=self._now(), stage=name[:120], detail=detail[:2000], state=state
            )
        )

    async def say(self, markdown: str) -> None:
        await self.ctx.emit(TextBlock(id=new_id(), at=self._now(), markdown=markdown[:8000]))

    async def summary(self, title: str, groups: list[SummaryGroup]) -> None:
        await self.ctx.emit(
            SummaryBlock(id=new_id(), at=self._now(), title=title[:200], groups=groups)
        )

    async def confirm(self, question: str, yes: str, no: str) -> bool:
        answer = await self.ctx.ask(
            ConfirmBlock(
                id=new_id(),
                at=self._now(),
                question=question[:500],
                confirm_label=yes[:40],
                cancel_label=no[:40],
            )
        )
        return truthy(answer)

    async def choose(self, question: str, options: list[tuple[str, str, str | None]]) -> str | None:
        """Single choice among (value, label, hint). Returns the chosen value."""
        answer = await self.ctx.ask(
            ChoiceBlock(
                id=new_id(),
                at=self._now(),
                question=question[:500],
                mode="single",
                required=True,
                options=[
                    ChoiceOption(value=v[:120], label=label[:200], hint=(hint or None))
                    for v, label, hint in options
                ],
            )
        )
        values = answer.values
        if isinstance(values, list):
            return values[0] if values else None
        return values if isinstance(values, str) else None

    async def ask_url(self, question: str, placeholder: str = "https://…") -> str | None:
        answer = await self.ctx.ask(
            InputBlock(
                id=new_id(),
                at=self._now(),
                question=question[:500],
                input_kind="url",
                placeholder=placeholder,
                required=True,
                autocomplete="url",
            )
        )
        value = answer.values
        text = value[0] if isinstance(value, list) and value else value
        return text.strip() if isinstance(text, str) and text.strip() else None


def truthy(answer: AgentUiAnswer) -> bool:
    value = answer.values
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        value = value[0] if value else None
    return isinstance(value, str) and value.strip().lower() in {"true", "yes", "confirm", "1", "да"}


def item(label: str, value: str, url: str | None = None, *, confirmed: bool = True) -> SummaryItem:
    return SummaryItem(
        label=label[:120],
        value=value[:500],
        source_url=url,  # type: ignore[arg-type]
        confidence="confirmed" if confirmed else "guess",
    )
