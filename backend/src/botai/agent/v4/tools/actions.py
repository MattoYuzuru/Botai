"""`action_propose`, `action_execute`, `action_history`: write actions of the v4 harness (D-006).

The runtime does not decide whether a write is allowed — the owner's policy does. These tools:

* `action_propose` — journal the exact request and resolve the policy without sending anything;
  the agent can show it to the owner and stop there (the "заполненная форма" path stays manual);
* `action_execute` — same proposal, then follow the resolved mode: `auto` sends now, `ask` asks
  the owner through the job's existing ask channel (`ctx.ask` → `ChoiceBlock`, so the worker parks
  and the SSE/answers flow just works) and sends only when allowed, `never` refuses. A retried
  call with the same idempotency key returns the journaled result instead of sending twice;
* `action_history` — what was proposed/decided/sent earlier (redacted journal rows).

Registration: `build_action_tools(services=services, user_id=..., http_transport=...,
control=...)` returns `list[ToolHandler]` for `build_agent_wiring`. A browser action uses the run's
`BrowserControl`; outside a run (API) it stays `approval_required`/`failed(browser_unavailable)`
because only the run owns a browser.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from botai.actions.engine import ActionEngine, ActionReport
from botai.actions.models import ActionRequest
from botai.agent.v4.protocol import (
    ToolContext,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.core.errors import BotaiError
from botai.core.services import Services
from botai.db.models import ACTION_CLASSES

_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
_BODY_KINDS = ["json", "form", "raw"]
_TRANSPORTS = ["http", "browser"]
_ACTIONS = ["navigate", "click", "type", "press", "select"]
_DECISIONS = [
    "auto",
    "asked",
    "approved",
    "never",
    "executing",
    "executed",
    "failed",
    "denied",
    "expired",
]

_ACTION_PARAMETERS = object_schema(
    {
        "transport": {
            "type": "string",
            "enum": _TRANSPORTS,
            "description": "http — прямой запрос; browser — шаги в браузере задачи.",
        },
        "intent": {
            "type": "string",
            "minLength": 3,
            "maxLength": 300,
            "description": (
                "Обязательно. Что именно делает действие, человеческим языком (это видит владелец)."
            ),
        },
        "method": {
            "type": "string",
            "enum": _METHODS,
            "description": (
                "Обязателен при transport=http (POST/PUT/PATCH/DELETE); для browser не нужен."
            ),
        },
        "url": {
            "type": "string",
            "maxLength": 4096,
            "description": (
                "Полный URL: обязателен при transport=http; для browser — опционально."
            ),
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Заголовки. Значения не попадают в журнал.",
        },
        "cookies": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Cookie запроса; в журнале остаются только имена.",
        },
        "body": {"description": "Тело: объект для json/form, строка для raw."},
        "body_kind": {"type": "string", "enum": _BODY_KINDS, "default": "json"},
        "browser_steps": {
            "type": "array",
            "maxItems": 30,
            "description": "Шаги браузера для transport=browser.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": _ACTIONS},
                    "url": {"type": "string", "maxLength": 4096},
                    "target": {
                        "type": "object",
                        "description": "selector / text / role / x,y — как у browser_click.",
                    },
                    "text": {"type": "string", "maxLength": 4000},
                    "key": {"type": "string", "maxLength": 40},
                    "value": {"type": "string", "maxLength": 2000},
                    "label": {"type": "string", "maxLength": 200},
                    "index": {"type": "integer", "minimum": 0, "maximum": 500},
                    "clear": {"type": "boolean", "default": True},
                },
                "required": ["action"],
            },
        },
        "action_class": {
            "type": "string",
            "enum": list(ACTION_CLASSES),
            "description": (
                "Класс действия. Может только повысить класс, который следует из запроса; "
                "деструктивные/денежные действия по умолчанию спрашивают."
            ),
        },
        "source_id": {
            "type": "string",
            "maxLength": 26,
            "description": "Источник, к которому относится действие (для политики и журнала).",
        },
        "space_id": {"type": "string", "maxLength": 26},
        "idempotency_key": {
            "type": "string",
            "minLength": 4,
            "maxLength": 120,
            "description": "Ключ повтора: с тем же ключом действие не выполнится дважды.",
        },
        "undo_hint": {
            "type": "string",
            "maxLength": 500,
            "description": "Обратное действие, если playbook его знает (показывается владельцу).",
        },
        "follow_redirects": {"type": "boolean", "default": True},
        "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 300},
    },
    required=["transport", "intent"],
)

_REQUEST_KEYS = (
    "transport",
    "intent",
    "method",
    "url",
    "headers",
    "cookies",
    "body",
    "body_kind",
    "browser_steps",
    "action_class",
    "source_id",
    "space_id",
    "idempotency_key",
    "undo_hint",
    "follow_redirects",
    "timeout_s",
)


def _parse_request(arguments: dict[str, Any]) -> ActionRequest | ToolResult:
    payload = {key: arguments[key] for key in _REQUEST_KEYS if key in arguments}
    if "browser_steps" in payload:
        payload["steps"] = payload.pop("browser_steps")
    try:
        return ActionRequest.model_validate(payload)
    except ValidationError as exc:
        return ToolResult.failure("invalid_arguments", _validation_detail(exc))


def _validation_detail(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def _job_id(context: ToolContext) -> str | None:
    raw = context.metadata.get("job_id")
    return raw if isinstance(raw, str) and raw else None


def _user_id(context: ToolContext, fallback: str) -> str:
    return context.user_id or fallback


class ActionProposeTool:
    """Journal a write proposal and resolve the policy; send nothing."""

    spec = ToolSpec(
        name="action_propose",
        description=(
            "Подготовить и показать write-действие (POST/PUT/PATCH/DELETE или шаги браузера), "
            "не отправляя его. Возвращает класс действия, политику владельца (auto/ask/never) и "
            "точный запрос с замаскированными секретами; запись попадает в журнал действий. "
            "Отправка — отдельным action_execute с тем же idempotency_key."
        ),
        parameters=_ACTION_PARAMETERS,
        timeout_s=30.0,
        policy="read",
        idempotent=False,
        max_result_chars=20_000,
    )

    def __init__(self, engine: ActionEngine, *, user_id: str) -> None:
        self._engine = engine
        self._user_id = user_id

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        request = _parse_request(arguments)
        if isinstance(request, ToolResult):
            return request
        try:
            prepared = await self._engine.prepare(
                user_id=_user_id(context, self._user_id),
                request=request,
                run_id=context.run_id,
                job_id=_job_id(context),
            )
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        data = prepared.entry.public()
        data.update(
            {
                "action_id": prepared.entry.id,
                "policy": prepared.policy.public(),
                "duplicate": prepared.duplicate,
            }
        )
        if prepared.policy.mode == "never":
            return ToolResult.failure(
                "policy_denied",
                "Политика владельца запрещает этот класс действий (never): ничего не отправлено.",
                hint="Попросите владельца изменить политику, если действие действительно нужно.",
                data=data,
            )
        summary = (
            f"Действие подготовлено: {prepared.policy.action_class}, "
            f"политика {prepared.policy.mode}. Ничего не отправлено."
        )
        return ToolResult.success(summary=summary, data=data)


class ActionExecuteTool:
    """Run a write action under the owner's policy (auto/ask/never), exactly once per key."""

    spec = ToolSpec(
        name="action_execute",
        description=(
            "Выполнить write-действие по политике владельца: auto — отправить сейчас, ask — "
            "спросить владельца с точными деталями запроса и отправить только после разрешения, "
            "never — отказать. Каждое действие журналируется (метод, URL, хеш тела); повтор с тем "
            "же idempotency_key не отправляет второй раз. Деструктивные и денежные действия по "
            "умолчанию спрашивают."
        ),
        parameters=_ACTION_PARAMETERS,
        timeout_s=310.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
        max_result_chars=20_000,
    )

    def __init__(self, engine: ActionEngine, *, user_id: str, control: Any = None) -> None:
        self._engine = engine
        self._user_id = user_id
        self._control = control

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        request = _parse_request(arguments)
        if isinstance(request, ToolResult):
            return request
        try:
            report = await self._engine.run(
                user_id=_user_id(context, self._user_id),
                request=request,
                run_id=context.run_id,
                job_id=_job_id(context),
                ask=context.ask,
                control=self._control,
            )
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        return _report_result(report)


class ActionHistoryTool:
    """Read the action journal (redacted rows: no secret values were ever stored)."""

    spec = ToolSpec(
        name="action_history",
        description=(
            "История действий: что предлагалось, как решено (auto/ask/never, подтверждено/"
            "отклонено) и что отправлено — метод, URL, хеш тела и краткий результат. Значений "
            "секретов и тел запросов в журнале нет."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string", "maxLength": 26},
                "action_class": {"type": "string", "enum": list(ACTION_CLASSES)},
                "decision": {"type": "string", "enum": _DECISIONS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            }
        ),
        timeout_s=30.0,
        policy="read",
        idempotent=True,
        max_result_chars=20_000,
    )

    def __init__(self, engine: ActionEngine, *, user_id: str) -> None:
        self._engine = engine
        self._user_id = user_id

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        limit = arguments.get("limit", 20)
        entries = await self._engine.history(
            user_id=_user_id(context, self._user_id),
            source_id=arguments.get("source_id"),
            action_class=arguments.get("action_class"),
            decision=arguments.get("decision"),
            limit=limit if isinstance(limit, int) else 20,
        )
        rows = [entry.public() for entry in entries]
        return ToolResult.success(
            summary=f"Действий в журнале: {len(rows)}",
            data={"actions": rows, "count": len(rows)},
        )


class ActionToolkit:
    """The three action tools over one engine; `control` is the run's browser (if any)."""

    def __init__(self, engine: ActionEngine, *, user_id: str, control: Any = None) -> None:
        self._engine = engine
        self._user_id = user_id
        self._control = control

    def tools(self) -> list[Any]:
        return [
            ActionProposeTool(self._engine, user_id=self._user_id),
            ActionExecuteTool(self._engine, user_id=self._user_id, control=self._control),
            ActionHistoryTool(self._engine, user_id=self._user_id),
        ]


def build_action_tools(
    *,
    services: Services,
    user_id: str,
    http_transport: httpx.AsyncBaseTransport | None = None,
    control: Any = None,
) -> list[Any]:
    """The `action_*` tools for one v4 run (registered by `build_agent_wiring`).

    The engine needs the DB session factory: the journal is the product, so a run without a DB
    refuses to start the action tools instead of pretending writes are journaled.
    """
    db = getattr(services, "db", None)
    if db is None:
        raise RuntimeError("action_* инструменты v4 требуют services.db (журнал действий)")
    engine = ActionEngine(
        db=db,
        clock=services.clock.now,
        http_transport=http_transport,
    )
    return ActionToolkit(engine, user_id=user_id, control=control).tools()


def _report_result(report: ActionReport) -> ToolResult:
    data = report.model_data()
    if report.status == "executed":
        summary = report.outcome.summary if report.outcome is not None else "Действие выполнено."
        if report.duplicate:
            summary = f"Повтор не отправлен: {summary}"
        return ToolResult.success(summary=summary, data=data)
    if report.status == "approval_required":
        return ToolResult.failure(
            "approval_required",
            "Действие по политике требует подтверждения владельца: вопрос показан, действие "
            "ждёт решения. Если владелец подтвердит — повторите action_execute с тем же "
            "idempotency_key.",
            hint="Ничего не отправлено до разрешения.",
            data=data,
        )
    if report.status == "denied":
        return ToolResult.failure(
            "action_denied", "Владелец отклонил действие: ничего не отправлено.", data=data
        )
    if report.status == "never":
        return ToolResult.failure(
            "policy_denied",
            "Политика владельца запрещает этот класс действий: ничего не отправлено.",
            data=data,
        )
    if report.status == "expired":
        return ToolResult.failure(
            "action_expired",
            "Время подтверждения истекло, действие не отправлено. Предложите его заново.",
            data=data,
        )
    if report.status == "failed":
        outcome = report.outcome
        code = (outcome.error if outcome is not None else None) or "action_failed"
        detail = (
            outcome.detail or outcome.summary if outcome is not None else "Действие не выполнено."
        )
        return ToolResult.failure(
            code,
            detail or "Действие не выполнено.",
            retryable=bool(outcome is not None and outcome.retryable),
            data=data,
        )
    return ToolResult.failure(
        "action_pending",
        "Действие уже выполняется или ждёт своего решения; повторная отправка не нужна.",
        hint="Проверьте action_history.",
        data=data,
    )


__all__ = [
    "ActionExecuteTool",
    "ActionHistoryTool",
    "ActionProposeTool",
    "ActionToolkit",
    "build_action_tools",
]
