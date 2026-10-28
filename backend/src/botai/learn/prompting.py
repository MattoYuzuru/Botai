"""Prompts and response schemas of the learning agent.

The text lives in `prompts/*.md` (no university names there). One request authors one kind of
data; the observation comes first and the kind-specific task last, so the long prefix is shared
between the requests of one exploration and the provider's prefix cache is hit.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from botai.contracts.schemas import validator
from botai.contracts.specs import Kind
from botai.core.llm import LlmMessage, LlmRequest
from botai.sources.vocab import KIND_LABELS, REQUIRED, VOCAB

PROMPTS = Path(__file__).parent / "prompts"
MAX_CANDIDATES = 3

KIND_HINTS: dict[Kind, str] = {
    "scheduleEvents": "Это занятия студента по расписанию: пара, время начала и конца, аудитория, преподаватель.",
    "deadlines": "Это сроки сдачи заданий студента: название, срок, статус сдачи.",
    "courses": "Это курсы и дисциплины студента.",
    "assignments": "Это задания курсов: название, описание, максимальный балл.",
    "grades": "Это оценки студента за работы и дисциплины.",
    "news": "Это новости учебного заведения или курсов: заголовок, дата публикации, ссылка.",
    "people": "Это преподаватели и сотрудники: имя и роль.",
    "locations": "Это аудитории и корпуса.",
}
FIELD_HELP: dict[str, str] = {
    "externalId": "стабильный идентификатор записи",
    "title": "название",
    "kind": "тип занятия (лекция, семинар, лабораторная, экзамен…)",
    "courseId": "идентификатор курса или дисциплины",
    "startsAt": "время начала (`iso`, `unixSeconds`…)",
    "endsAt": "время окончания",
    "durationMinutes": "длительность в минутах, если нет времени окончания",
    "format": "очно, онлайн или смешанно",
    "location": "аудитория или «онлайн»",
    "teacher": "преподаватель (`joinNames`, если их несколько)",
    "url": "ссылка на запись",
    "code": "код курса",
    "term": "семестр или учебный период",
    "description": "описание",
    "archived": "курс завершён",
    "maxScore": "максимальный балл",
    "assignmentId": "идентификатор задания",
    "dueAt": "срок сдачи",
    "effectiveDueAt": "срок с учётом продления",
    "state": "статус: открыто, сдано, оценено, пропущено",
    "value": "значение оценки",
    "scale": "шкала оценки",
    "weight": "вес",
    "gradedAt": "когда выставлена",
    "name": "имя",
    "role": "роль: преподаватель, ассистент, сотрудник",
    "email": "рабочая почта преподавателя, только если она публична",
    "building": "корпус",
    "address": "адрес",
    "body": "текст",
    "publishedAt": "дата публикации",
}


@lru_cache
def load(name: str) -> str:
    return (PROMPTS / name).read_text("utf-8")


def system_prompt() -> str:
    return load("system.md").strip()


def _fields_help(kind: Kind) -> str:
    return "\n".join(f"- `{name}` — {FIELD_HELP.get(name, name)}" for name in sorted(VOCAB[kind]))


PROBLEMS: dict[str, str] = {
    "not_found": "адрес больше не отвечает (ошибка «не найдено»)",
    "status": "сайт отвечает ошибкой на прежний запрос",
    "format": "ответ пришёл в другом формате, чем описано",
    "fields": "записи есть, но описанные поля в них больше не находятся",
    "selector": "по прежним селекторам ничего не находится",
    "bootstrap": "шаг подготовки не получает нужные значения",
    "empty": "по прежнему описанию записей не находится, хотя раньше они были",
}


def _feedback_note(feedback: list[str] | None) -> str:
    if not feedback:
        return ""
    lines = "\n".join(f"- {line}" for line in feedback[:12])
    return (
        "### Замечания к предыдущему варианту\n\n"
        "Предыдущий вариант отклонила проверка. Исправь и верни новый ответ целиком. "
        f"Замечания указывают место и причину, но не значения:\n{lines}"
    )


def task_prompt(
    kind: Kind,
    feedback: list[str] | None = None,
    *,
    previous: dict[str, Any] | None = None,
    problem: str | None = None,
) -> str:
    """The kind-specific task. With `previous` it is a repair of an endpoint that stopped working."""
    required = sorted(REQUIRED[kind])
    if kind == "scheduleEvents":
        required = [*required, "endsAt (или durationMinutes)"]
    template = load("repair.md" if previous is not None else "author.md")
    text = (
        template.replace("{{kind_label}}", KIND_LABELS[kind])
        .replace("{{kind}}", kind)
        .replace("{{kind_hint}}", KIND_HINTS[kind])
        .replace("{{fields}}", _fields_help(kind))
        .replace("{{required}}", ", ".join(f"`{r}`" for r in required))
        .replace("{{feedback}}", _feedback_note(feedback))
        .replace("{{problem}}", PROBLEMS.get(problem or "", "сайт отвечает не так, как описано"))
    )
    if previous is not None:
        text = text.replace("{{previous}}", json.dumps(previous, ensure_ascii=False, indent=1))
    return text.strip()


@lru_cache
def _recipe_defs() -> dict[str, Any]:
    schema: dict[str, Any] = validator("recipe.schema.json").schema  # pyright: ignore[reportAssignmentType]
    return schema["$defs"]


def author_schema(kind: Kind) -> dict[str, Any]:
    """The model answers with candidates for one kind, shaped by the recipe contract."""
    defs = copy.deepcopy(_recipe_defs())
    defs["endpoint"]["properties"]["kind"] = {"const": kind}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["found", "endpoints"],
        "properties": {
            "found": {"type": "boolean"},
            "reasoning": {"type": "string", "maxLength": 600},
            "endpoints": {
                "type": "array",
                "maxItems": MAX_CANDIDATES,
                "items": {"$ref": "#/$defs/endpoint"},
            },
            "bootstrap": {
                "type": "array",
                "maxItems": 4,
                "items": {"$ref": "#/$defs/bootstrapStep"},
            },
            "auth": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"enum": ["cookie", "bearer", "none"]},
                    "headerName": {"type": "string"},
                },
            },
            "doubts": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 200},
            },
        },
        "$defs": defs,
    }


def author_request(
    kind: Kind,
    observation: str,
    feedback: list[str] | None = None,
    *,
    previous: dict[str, Any] | None = None,
    problem: str | None = None,
) -> LlmRequest:
    task = task_prompt(kind, feedback, previous=previous, problem=problem)
    return LlmRequest(
        messages=[
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(role="user", content=f"{observation}\n\n{task}"),
        ],
        json_schema=author_schema(kind),
        max_tokens=4000,
        temperature=0.1,
        purpose="learn" if previous is None else "repair",
    )


def registry_request(page_text: str) -> LlmRequest:
    return LlmRequest(
        messages=[
            LlmMessage(role="system", content=load("registry.md").strip()),
            LlmMessage(role="user", content=f"<observation>\n{page_text[:6000]}\n</observation>"),
        ],
        json_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["warning"],
            "properties": {"warning": {"type": ["string", "null"], "maxLength": 300}},
        },
        max_tokens=400,
        temperature=0.0,
        purpose="extract",
    )


def discovery_request(observation: dict[str, Any], intent: object) -> LlmRequest:
    """A separate planner schema; recipe authoring stays unchanged and independently linted."""
    from botai.learn.intent import LearningIntent

    if not isinstance(intent, LearningIntent):
        raise TypeError("learning intent required")
    return LlmRequest(
        messages=[
            LlmMessage(role="system", content=load("discover.md").strip()),
            LlmMessage(
                role="user",
                content=json.dumps(
                    {
                        "mode": intent.discovery_mode,
                        "requestedKinds": intent.requested_kinds,
                        "absentKinds": intent.absent_kinds,
                        "goal": intent.user_note,
                        "kindLabels": KIND_LABELS,
                        "observation": observation,
                    },
                    ensure_ascii=False,
                ),
            ),
        ],
        json_schema={
            "title": "botai-observation-plan-v1",
            "type": "object",
            "additionalProperties": False,
            "required": ["stateRef", "action", "ref", "discoveries", "evidenceRefs"],
            "properties": {
                "stateRef": {"type": "integer", "minimum": 0},
                "action": {"enum": ["navigate", "inspect", "activate", "finish"]},
                "ref": {"type": ["string", "null"], "maxLength": 12},
                "discoveries": {
                    "type": "array",
                    "maxItems": len(KIND_LABELS),
                    "items": {"enum": list(KIND_LABELS)},
                    "uniqueItems": True,
                },
                "evidenceRefs": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "maxLength": 12},
                    "uniqueItems": True,
                },
            },
        },
        max_tokens=1200,
        temperature=0.0,
        purpose="learn",
    )
