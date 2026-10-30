"""Blocks the learning jobs show to the student (contracts/agent-ui.schema.json).

Every stage is a `ThinkingBlock` that is emitted as running and re-emitted under the same id when
it finishes (a block is idempotent by id). Nothing here carries page contents or secrets.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

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
from botai.contracts.specs import Kind
from botai.core.clock import Clock
from botai.core.dialogue import DialogueContext
from botai.core.ids import new_id
from botai.core.jobs import JobContext
from botai.learn.summary import DETAILS_MAX, UNITS, counted
from botai.sources.adapter import Record
from botai.sources.vocab import KIND_LABELS

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

    async def confirm(
        self, question: str, yes: str, no: str, *, details: str | None = None
    ) -> bool:
        """Ask for confirmation. `details` (markdown) is what is confirmed: it is shown in the same
        message as the question and the buttons."""
        answer = await self.ctx.ask(
            ConfirmBlock(
                id=new_id(),
                at=self._now(),
                question=question[:500],
                confirm_label=yes[:40],
                cancel_label=no[:40],
                details=details[:DETAILS_MAX] if details else None,
            )
        )
        return truthy(answer)

    async def choose(
        self, question: str, options: Sequence[tuple[str, str, str | None]]
    ) -> str | None:
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

    async def ask_text(self, question: str, placeholder: str = "") -> str | None:
        answer = await self.ctx.ask(
            InputBlock(
                id=new_id(),
                at=self._now(),
                question=question[:500],
                input_kind="text",
                placeholder=placeholder[:120] or None,
                required=True,
                max_length=200,
            )
        )
        value = answer.values
        text = value[0] if isinstance(value, list) and value else value
        return text.strip()[:200] if isinstance(text, str) and text.strip() else None

    async def ask_learning_turn(self, question: str) -> str | None:
        """A bounded conversational answer: a link, an explanation, or both."""
        block = InputBlock(
            id=new_id(),
            at=self._now(),
            question=question[:500],
            input_kind="text",
            placeholder="Ссылка и пояснение: что здесь есть и что подключить",
            required=True,
            max_length=2000,
        )
        if isinstance(self.ctx, DialogueContext):
            return (await self.ctx.ask_dialogue(block)).strip()[:2000] or None
        answer = await self.ctx.ask(block)
        value = answer.values
        text = value[0] if isinstance(value, list) and value else value
        return text.strip()[:2000] if isinstance(text, str) and text.strip() else None


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


SAMPLE = 3
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _day(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{_WEEKDAYS[local.weekday()]}, {local.day} {_MONTHS[local.month - 1]}"


def describe(record: Record, tz: ZoneInfo) -> tuple[str, str]:
    """A one-line (label, value) for the summary of what was collected."""
    f = record.fields

    def when(name: str) -> datetime | None:
        value = f.get(name)
        return datetime.fromisoformat(value) if isinstance(value, str) else None

    title = str(f.get("title") or f.get("name") or f.get("value") or "Без названия")
    if record.kind == "scheduleEvents" and (start := when("startsAt")):
        end = when("endsAt")
        span = f"{start.astimezone(tz):%H:%M}" + (
            f"–{end.astimezone(tz):%H:%M}" if end and end != start else ""
        )
        place = f", {f['location']}" if f.get("location") else ""
        return title, f"{_day(start, tz)}, {span}{place}"
    if record.kind == "deadlines" and (due := when("dueAt")):
        return title, f"до {_day(due, tz)}, {due.astimezone(tz):%H:%M}"
    if record.kind == "news" and (published := when("publishedAt")):
        return title, _day(published, tz)
    if record.kind == "grades":
        return str(f.get("title") or "Оценка"), str(f.get("value", "—"))
    return title, str(f.get("code") or "—")


def sample_of(records: list[Record], kind: Kind, now: datetime) -> list[Record]:
    key = {"scheduleEvents": "startsAt", "deadlines": "dueAt", "news": "publishedAt"}.get(kind)
    if key is None:
        return records[:SAMPLE]
    ordered = sorted(records, key=lambda r: str(r.fields.get(key, "")))
    upcoming = [r for r in ordered if str(r.fields.get(key, "")) >= now.isoformat()]
    return (upcoming or ordered)[:SAMPLE]


def kinds_text(kinds: dict[str, dict[str, int]], only: Collection[str] | None = None) -> str:
    """Counts per kind in a fixed order, in words: "расписание — 35 занятий, дедлайны — 12 заданий
    со сроками", from the Normalizer's per-kind stats."""
    return ", ".join(
        f"{KIND_LABELS[k].lower()} — {counted(v['inserted'] + v['updated'] + v['unchanged'], UNITS[k])}"
        for k in KIND_LABELS
        if k in kinds and (only is None or k in only)
        for v in [kinds[k]]
    )
