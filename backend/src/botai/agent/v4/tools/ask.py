"""`ask_user`: a blocking question to the owner, without DB (architecture §4.4, §4.5).

The runtime passes an `AskUser` callback in `ToolContext`; the worker/UI later implements it. A
`secret` question is answered through the same callback but its value never appears in the tool
result: the model learns only that the input was received. The secret broker (phase 1.4) will
turn such an input into a handle the executor can use.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.protocol import (
    AskQuestion,
    ToolContext,
    ToolResult,
    ToolSpec,
    object_schema,
)

MAX_OPTIONS = 10


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    options: list[str] = Field(default_factory=list, max_length=MAX_OPTIONS)
    input_kind: Literal["text", "choice", "secret"] = "text"
    placeholder: str | None = Field(default=None, max_length=200)
    timeout_s: float | None = Field(default=None, gt=0, le=3600)


class AskUserTool:
    """Ask the student; blocks the run until an answer or the timeout."""

    spec = ToolSpec(
        name="ask_user",
        description=(
            "Задать вопрос владельцу и дождаться ответа: свободный текст, выбор из вариантов "
            "или защищённый ввод (пароль/код — значение не возвращается модели). "
            "Используйте, когда без человека действие или данные не получить."
        ),
        parameters=object_schema(
            {
                "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                "options": {
                    "type": "array",
                    "maxItems": MAX_OPTIONS,
                    "items": {"type": "string", "maxLength": 500},
                    "description": "Варианты ответа для input_kind=choice.",
                },
                "input_kind": {
                    "type": "string",
                    "enum": ["text", "choice", "secret"],
                    "default": "text",
                },
                "placeholder": {"type": ["string", "null"], "maxLength": 200},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.1, "maximum": 3600},
            },
            required=["question"],
        ),
        timeout_s=600.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = AskUserArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure("invalid_arguments", _invalid(exc))
        if args.input_kind == "choice" and not args.options:
            return ToolResult.failure(
                "invalid_arguments", "Для input_kind=choice нужен непустой options."
            )
        ask = context.ask
        if ask is None:
            return ToolResult.failure(
                "no_user_channel",
                "Канал вопросов владельцу недоступен в этом запуске.",
                hint="Продолжите без вопроса или честно скажите, что нужен ответ человека.",
            )
        question = AskQuestion(
            question=args.question,
            options=tuple(args.options),
            input_kind=args.input_kind,
            placeholder=args.placeholder,
            timeout_s=args.timeout_s,
        )
        try:
            if args.timeout_s is not None:
                async with asyncio.timeout(args.timeout_s):
                    answer = await ask(question)
            else:
                answer = await ask(question)
        except TimeoutError:
            return ToolResult.failure(
                "ask_timeout",
                f"Владелец не ответил за {args.timeout_s:g} с.",
                retryable=True,
            )
        if answer.cancelled:
            return ToolResult.failure(
                "ask_cancelled",
                "Вопрос отменён.",
                data={"cancelled": True},
            )
        if args.input_kind == "secret":
            # The value is for the executor / secret broker, never for the model context.
            return ToolResult.success(
                summary="Защищённый ввод получен; значение модели не показывается.",
                data={"answered": True, "hidden": True},
            )
        data: dict[str, Any] = {"answer": answer.text, "choice": answer.choice}
        if answer.choice is not None and 0 <= answer.choice < len(args.options):
            data["chosen"] = args.options[answer.choice]
        return ToolResult.success(summary="Ответ владельца получен.", data=data)


def _invalid(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


__all__ = ["AskUserArgs", "AskUserTool"]
