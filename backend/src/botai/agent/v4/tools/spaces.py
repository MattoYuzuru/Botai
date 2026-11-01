"""`space_*` tools of the v4 agent: the student's universities (spaces) are created by the agent.

A space is a university of a student (`03-sources-and-ux.md` §1.1: no space without a university).
Until this toolkit existed the v4 agent could create a source only inside a space the app had
already made — so from an empty account ("Вузов пока нет") it could not do the very thing the chat
is for: make the university the student named. The UI deliberately has no "create university"
form (03 §3.1: connecting is a conversation in the chat); the button «Подключить вуз» only opens
this chat. `space_create` is therefore the product path, not a helper for tests.

The tool is a thin wrapper over `spaces.service.create_space` — the same service behind
`POST /spaces` — so the registry row, the one-space-per-university invariant and the accent hue
are the app's own rules, not a copy. Calling it twice with the same university returns the
existing space (`created: false`), never a second one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.core.errors import BotaiError
from botai.spaces import service as spaces_service
from botai.spaces.schemas import SpaceCreate


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        max_length=120,
        description="Название вуза словами, как его сказал студент",
    )
    university_id: str | None = Field(
        default=None,
        description="id известного вуза из справочника (GET /universities), если он есть",
    )
    homepage_url: str | None = Field(
        default=None,
        max_length=500,
        description="Сайт вуза, если студент его назвал: попадёт в справочник как адрес",
    )


class SpaceToolkit:
    """Builds the `space_*` tools over one session factory (the same one the source tools use)."""

    def __init__(self, *, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm
        self._handlers: list[ToolHandler] = [FunctionTool(self.create_spec, self._create)]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    async def _create(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        try:
            args = _Args.model_validate(arguments)
        except ValidationError as exc:
            return _invalid_arguments(exc)
        try:
            async with self._sm() as session:
                space, university, created = await spaces_service.create_space(
                    session,
                    user_id,
                    SpaceCreate(
                        title=args.title,
                        university_id=args.university_id,
                        homepage_url=args.homepage_url,
                    ),
                )
                data = _space_data(space, university, created)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        summary = (
            f"Вуз «{university.title}» создан" if created else f"Вуз «{university.title}» уже есть"
        )
        return ToolResult.success(summary=summary, data=data)

    create_spec = ToolSpec(
        name="space_create",
        description=(
            "Создать вуз (пространство) студента по названию. Вызывай, когда у студента нет вуза, "
            "в который нужно добавить источник: приложение не даёт студенту создать вуз самому, "
            "это делаешь ты. Название бери словами студента; university_id — если вуз уже есть в "
            "справочнике; homepage_url — если студент назвал сайт. Повтор с тем же вузом вернёт "
            "существующее пространство. Затем добавь источник через source_create с "
            "space_id из ответа."
        ),
        parameters=object_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "university_id": {"type": ["string", "null"]},
                "homepage_url": {"type": ["string", "null"], "maxLength": 500},
            },
            required=["title"],
        ),
        timeout_s=60.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )


def _space_data(space: Any, university: Any, created: bool) -> dict[str, Any]:
    """What the model may know about the new university: ids and human names, no secrets."""
    return {
        "space": {
            "id": space.id,
            "title": space.title,
            "shortTitle": university.short_title,
            "universityId": space.university_id,
            "timeZone": space.time_zone,
            "created": created,
        },
        "university": {
            "id": university.id,
            "title": university.title,
            "shortTitle": university.short_title,
        },
    }


def _invalid_arguments(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "arguments"
        parts.append(f"{location}: {error['msg']}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


def build_space_tools(toolkit: SpaceToolkit) -> list[ToolHandler]:
    """The `space_*` set; the wiring adds it next to `source_*` when the v4 engine is on."""
    return toolkit.tools()


__all__ = ["SpaceToolkit", "build_space_tools"]
