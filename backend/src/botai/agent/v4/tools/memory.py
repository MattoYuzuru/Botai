"""`memory_remember`, `memory_forget`, `memory_list`: what the v4 agent keeps about the student.

The notes are the same `user_memory` rows as in v3 (`agent/notes.py`, FR-050): at most 300
characters, deduplicated case-insensitively, at most 50 per student, visible to the student and
deletable by them. Every chat turn already shows them in the context message (`turn_context`) as
data about the student, so a fact remembered once reaches every later chat.

The guard is the one of `notes.clean_note`: a note that looks like a password, a code or a token,
holds an address, or reads as an order or a permission for the assistant is refused at the door,
so neither a primary secret nor a standing instruction planted by a page can become memory. On top
of it a note that holds a value the run's secret broker issued is refused (`mask`), whatever it
looks like.

`kind="rule"` stores a standing instruction of the student instead (D-045, `notes.clean_rule`):
only on the student's explicit request; it reaches later turns as its own block «Правила
студента». The guard of a rule allows the imperative, and still refuses secrets, addresses and any
power over the action policy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import notes
from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.core.errors import BotaiError


class _RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000)
    kind: Literal["note", "rule"] = "note"


class _ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: str = Field(min_length=1, max_length=26)


def _invalid(exc: ValidationError) -> ToolResult:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    return ToolResult.failure(
        "invalid_arguments", f"Некорректные аргументы: {location}: {first.get('msg', 'ошибка')}"
    )


class MemoryToolkit:
    """The memory tools over one session factory; `mask` is the run's secret masker."""

    remember_spec = ToolSpec(
        name="memory_remember",
        description=(
            "Запомнить надолго устойчивый факт о студенте: группа, программа, курс, научный "
            "руководитель, тема курсовой, идущая заявка или дело и его этап, договорённость, "
            "предпочтение, шаг, который сработал. Заметка видна во всех следующих чатах и самому "
            f"студенту. Одна заметка — один факт, до {notes.NOTE_CHARS} знаков, без адресов. "
            "Нельзя: пароли, коды, токены, ссылки-ключи, указания самому себе. Повтор той же "
            "заметки ничего не дублирует; устаревший факт сначала забудь (memory_forget). "
            "kind=rule — постоянное указание студента тебе («про оценки сначала смотри ЦУ», "
            "«сроки показывай по Москве»): только когда студент сам просит запомнить, как "
            f"поступать; до {notes.MAX_RULES} правил, без секретов, адресов и полномочий на действия."
        ),
        parameters=object_schema(
            {
                "note": {"type": "string", "minLength": 1, "maxLength": notes.NOTE_CHARS},
                "kind": {"type": "string", "enum": list(notes.NOTE_KINDS), "default": "note"},
            },
            required=["note"],
        ),
        timeout_s=15.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )
    forget_spec = ToolSpec(
        name="memory_forget",
        description=(
            "Забыть заметку или правило студента: устаревший или неверный факт, отменённое "
            "правило, просьба студента. note_id — из блоков «Память о студенте»/«Правила "
            "студента» в контексте или из memory_list."
        ),
        parameters=object_schema(
            {"note_id": {"type": "string", "minLength": 1, "maxLength": 26}},
            required=["note_id"],
        ),
        timeout_s=15.0,
        policy="write_soft",
        serial_only=True,
    )
    list_spec = ToolSpec(
        name="memory_list",
        description=(
            "Все заметки и правила студента с note_id и kind, от старых к новым. Только чтение."
        ),
        parameters=object_schema(),
        timeout_s=15.0,
    )

    def __init__(
        self,
        *,
        sm: async_sessionmaker[AsyncSession],
        mask: Callable[[str], str] | None = None,
        student_text: str | None = None,
    ) -> None:
        self._sm = sm
        self._mask = mask
        # The student's own message of this turn (chat only): the evidence a rule needs.
        self._student_text = student_text
        self._handlers: list[ToolHandler] = [
            FunctionTool(self.remember_spec, self._remember),
            FunctionTool(self.forget_spec, self._forget),
            FunctionTool(self.list_spec, self._list),
        ]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    async def _remember(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult.failure("no_user", "Память без пользователя невозможна.")
        try:
            args = _RememberArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        if self._mask is not None and self._mask(args.note) != args.note:
            return ToolResult.failure(
                "secret_in_note",
                "В заметке есть значение секрета. Секреты не запоминаются: запиши факт без него.",
            )
        if args.kind == "rule":
            refused = self._rule_refusal(args.note, context)
            if refused is not None:
                return refused
        try:
            async with self._sm() as session:
                added = await notes.add_note(session, context.user_id, args.note, kind=args.kind)
                note_id, text, kind = added.note.id, added.note.note, added.note.kind
        except BotaiError as exc:
            return ToolResult.failure(
                "note_refused",
                f"Не запомнено: {exc.detail}.",
                hint=(
                    "Перепиши правило как указание о данных или ответах, без секретов, адресов и "
                    "полномочий на действия."
                    if args.kind == "rule"
                    else "Перепиши как короткий факт о студенте, без адресов, секретов и указаний."
                ),
            )
        return ToolResult.success(
            summary="Запомнено." if added.created else "Это уже есть в памяти.",
            data={"noteId": note_id, "note": text, "kind": kind, "created": added.created},
        )

    def _rule_refusal(self, rule: str, context: ToolContext) -> ToolResult | None:
        """A rule is a standing instruction: only a chat turn may write it, and only when the
        student's own message of this turn says it (a page or a tool result cannot plant one)."""
        if context.metadata.get("kind") != "chat":
            return ToolResult.failure(
                "note_refused",
                "Не запомнено: правило студента пишется только в чате.",
                hint="Правило сохраняется только по прямой просьбе студента в чате.",
            )
        if not notes.asked_by_student(rule, self._student_text):
            return ToolResult.failure(
                "note_refused",
                "Не запомнено: в сообщении студента этого правила нет.",
                hint="Правило сохраняется только по прямой просьбе студента; спроси его.",
            )
        return None

    async def _forget(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult.failure("no_user", "Память без пользователя невозможна.")
        try:
            args = _ForgetArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            async with self._sm() as session:
                await notes.delete_note(session, context.user_id, args.note_id.strip())
        except BotaiError:
            return ToolResult.failure(
                "not_found",
                "Такой заметки нет.",
                hint="Возьми note_id из блока «Память о студенте» в контексте или из memory_list.",
            )
        return ToolResult.success(summary="Забыто.", data={"noteId": args.note_id.strip()})

    async def _list(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del arguments
        if context.user_id is None:
            return ToolResult.failure("no_user", "Память без пользователя невозможна.")
        async with self._sm() as session:
            rows = await notes.list_notes(session, context.user_id)
            items = [
                {
                    "noteId": row.id,
                    "note": row.note,
                    "kind": row.kind,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        rules = sum(1 for item in items if item["kind"] == "rule")
        return ToolResult.success(
            summary=(
                f"Заметок: {len(items) - rules} из {notes.MAX_NOTES}; "
                f"правил: {rules} из {notes.MAX_RULES}."
            ),
            data={"notes": items},
        )


def build_memory_tools(toolkit: MemoryToolkit) -> list[ToolHandler]:
    """The memory set; the wiring adds it next to `source_*` when the v4 engine is on."""
    return toolkit.tools()


__all__ = ["MemoryToolkit", "build_memory_tools"]
