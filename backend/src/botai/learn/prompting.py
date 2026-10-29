"""Prompts and response schemas of the learning agent.

The text lives in `prompts/*.md` (no university names there). One request authors one kind of
data; the observation comes first and the kind-specific task last, so the long prefix is shared
between the requests of one exploration and the provider's prefix cache is hit.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from botai.contracts.schemas import validator
from botai.contracts.specs import Kind
from botai.core.browser import ObservedRequest
from botai.core.llm import LlmMessage, LlmRequest
from botai.sources.vocab import KIND_LABELS, REQUIRED, STRUCTURED_FIELDS, VOCAB

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
    "kindText": "тип занятия словами источника (например «Текущий рубежный контроль»)",
    "group": "группа или поток, например «ИВТ 2 курс»",
    "lessonNumber": "номер пары в дне (`number`)",
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
    "address": "адрес: улица и дом здания",
    "body": "текст",
    "publishedAt": "дата публикации",
}


@lru_cache
def load(name: str) -> str:
    return (PROMPTS / name).read_text("utf-8")


def system_prompt() -> str:
    return load("system.md").strip()


def _fields_help(kind: Kind) -> str:
    names = sorted(VOCAB[kind] - STRUCTURED_FIELDS)
    return "\n".join(f"- `{name}` — {FIELD_HELP.get(name, name)}" for name in names)


PROBLEMS: dict[str, str] = {
    "not_found": "адрес больше не отвечает (ошибка «не найдено»)",
    "status": "сайт отвечает ошибкой на прежний запрос",
    "format": "ответ пришёл в другом формате, чем описано",
    "fields": "записи есть, но описанные поля в них больше не находятся",
    "selector": "по прежним селекторам ничего не находится",
    "bootstrap": "шаг подготовки не получает нужные значения",
    "empty": "по прежнему описанию записей не находится, хотя раньше они были",
    "horizon": "описание читает одно окно дат, а соседние окна источника дают больше записей",
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
    factory_v2: bool = False,
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
        .replace("{{factory_rules}}", _factory_rules() if factory_v2 else "")
    )
    if previous is not None:
        text = text.replace("{{previous}}", json.dumps(previous, ensure_ascii=False, indent=1))
        if factory_v2:
            text += "\n\n" + _factory_rules()
    return text.strip()


@lru_cache
def _recipe_defs() -> dict[str, Any]:
    schema: dict[str, Any] = validator("recipe.schema.json").schema  # pyright: ignore[reportAssignmentType]
    return schema["$defs"]


def _factory_rules() -> str:
    return (
        "### Проверенные ссылки фабрики\n\n"
        "Для HTTP укажи endpoint.requestEvidenceId из наблюдаемого успешного запроса; "
        "каждый bootstrap тоже требует свой requestEvidenceId. Это поля верхнего уровня "
        "эндпоинта/шага, а не request. Не придумывай ссылки. Сервер сам привязывает cookie и bearer; "
        "authBinding, browserPlan, имена заголовков, storage keys и значения секретов не задавай. "
        "Старое поле auth не выбирает авторизацию в этом режиме.\n"
        "Для чтения отрисованного HTML выбери endpoint.browserPlanRef из проверенных планов. "
        "Только GET+HTML без body: сервер подставит исходную страницу и проверенные действия. "
        "Не совмещай browserPlanRef и requestEvidenceId. Простой render=true допустим лишь "
        "с requestEvidenceId наблюдаемого document-запроса той же страницы.\n"
        "Схема также описывает вложенные collections, ссылки from на именованный контекст, "
        "составление даты и времени compose и ограниченное разворачивание недельного расписания "
        "expand. Применяй их только когда такая структура подтверждена наблюдением.\n"
        "Адрес страницы для человека (uiUrl) сервер выводит сам из страницы, где браузер сделал "
        "запрос; не задавай uiUrl. Окна дат (pagination.type=dateWindow) двигают шаблон даты "
        "из query: окно 0 должно воспроизводить наблюдённый запрос."
    )


def _reachable_defs(schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    def refs(node: Any) -> set[str]:
        if isinstance(node, dict):
            result = set().union(*(refs(value) for value in node.values()))
            reference = node.get("$ref", "")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                result.add(reference.removeprefix("#/$defs/"))
            return result
        if isinstance(node, list):
            return set().union(*(refs(value) for value in node))
        return set()

    pending, included = refs(schema), set()
    while pending:
        name = pending.pop()
        if name not in included:
            included.add(name)
            pending.update(refs(definitions[name]) - included)
    return {name: definitions[name] for name in sorted(included)}


def _reference_schema(keys: set[str]) -> dict[str, Any]:
    return {"type": "string", "enum": sorted(keys)} if keys else {"not": {}}


def author_schema(
    kind: Kind,
    *,
    factory_v2: bool = False,
    request_facts: Mapping[str, ObservedRequest] | None = None,
    browser_plans: Mapping[str, object] | None = None,
    browser_plan_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The model answers with candidates for one kind, shaped by the recipe contract."""
    defs = copy.deepcopy(_recipe_defs())
    endpoint_name = "endpointV2" if factory_v2 else "endpoint"
    bootstrap_name = "bootstrapStepV2" if factory_v2 else "bootstrapStep"
    endpoint = defs[endpoint_name]
    endpoint["properties"]["kind"] = {"const": kind}
    if factory_v2:
        request_refs = _reference_schema(set(request_facts or {}))
        plan_refs = _reference_schema(set(browser_plans or {}) & set(browser_plan_urls or {}))
        request = endpoint["properties"]["request"]
        request["properties"].pop("authBinding", None)
        request["properties"].pop("browserPlan", None)
        request["allOf"] = [
            rule for rule in request.get("allOf", []) if "browserPlan" not in json.dumps(rule)
        ]
        endpoint["properties"].pop("uiUrl", None)
        endpoint["properties"]["requestEvidenceId"] = request_refs
        endpoint["properties"]["browserPlanRef"] = plan_refs
        endpoint["oneOf"] = [{"required": ["requestEvidenceId"]}, {"required": ["browserPlanRef"]}]
        bootstrap = defs[bootstrap_name]
        bootstrap["properties"].pop("authBinding", None)
        bootstrap["properties"]["requestEvidenceId"] = copy.deepcopy(request_refs)
        bootstrap["required"].append("requestEvidenceId")
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["found", "endpoints"],
        "properties": {
            "found": {"type": "boolean"},
            "reasoning": {"type": "string", "maxLength": 600},
            "endpoints": {
                "type": "array",
                "maxItems": MAX_CANDIDATES,
                "items": {"$ref": f"#/$defs/{endpoint_name}"},
            },
            "bootstrap": {
                "type": "array",
                "maxItems": 4,
                "items": {"$ref": f"#/$defs/{bootstrap_name}"},
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
    }
    schema["$defs"] = _reachable_defs(schema, defs)
    return schema


def author_request(
    kind: Kind,
    observation: str,
    feedback: list[str] | None = None,
    *,
    previous: dict[str, Any] | None = None,
    problem: str | None = None,
    factory_v2: bool = False,
    request_facts: Mapping[str, ObservedRequest] | None = None,
    browser_plans: Mapping[str, object] | None = None,
    browser_plan_urls: Mapping[str, str] | None = None,
) -> LlmRequest:
    task = task_prompt(kind, feedback, previous=previous, problem=problem, factory_v2=factory_v2)
    return LlmRequest(
        messages=[
            LlmMessage(role="system", content=system_prompt()),
            LlmMessage(role="user", content=f"{observation}\n\n{task}"),
        ],
        json_schema=author_schema(
            kind,
            factory_v2=factory_v2,
            request_facts=request_facts,
            browser_plans=browser_plans,
            browser_plan_urls=browser_plan_urls,
        ),
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
                "action": {"enum": ["navigate", "inspect", "activate", "use", "probe", "finish"]},
                "ref": {"type": ["string", "null"], "maxLength": 12},
                "valueRef": {"type": ["string", "null"], "maxLength": 40},
                "overrides": {
                    "type": ["object", "null"],
                    "maxProperties": 4,
                    "propertyNames": {"type": "string", "maxLength": 40},
                    "additionalProperties": {"type": "string", "maxLength": 60},
                },
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
