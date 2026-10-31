"""Secret handles for the v4 agent: ask for a primary secret, use it, never see it (phase 1.4).

The model may need a password/MFA code/API key to do its job, but a primary secret must not appear
in the prompt, the conversation, a tool result or the trace (D-008, architecture §4.4/§7). This
module implements that split:

* `secret_request` asks the owner through the runtime's `ask` channel (`input_kind="secret"`),
  stores the value in the Vault under an unguessable handle and returns **only the handle**;
* `secret_use` resolves the handle inside the tool and applies the value itself: type it into a
  browser field or put it into an HTTP header/JSON/form body. The tool returns the page info or
  the HTTP response with the secret value masked; the model never gets the value back.

Handles are issued per run and kept in this broker's memory: a resumed run must ask again. The
Vault record outlives the handle registry (kind=transient with TTL); later phases may persist the
registry. The broker also carries the masker used by `browser_*` observations, so a secret echoed
by a page or an API response is replaced with `***` before it reaches the context or the trace.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.http import build_client
from botai.agent.v4.protocol import (
    AskQuestion,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.browser.control import BrowserControl
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.core.vault import SecretKind, Vault

MASK = "***"
MIN_MASKABLE_CHARS = 4
MAX_SECRET_CHARS = 4096
MAX_HANDLES = 50
MAX_HTTP_BODY_CHARS = 20_000

HANDLE_PATTERN = re.compile(r"^sec_[0-9A-HJKMNP-TV-Z]{26}$")
_VALUE_SLOT = "{secret}"
_METHOD = re.compile(r"^[A-Z]{3,10}$")
_MASKED_RESPONSE_HEADERS = frozenset({"set-cookie"})


def _invalid(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


class SecretBroker:
    """Run-scoped handle registry: Vault storage plus in-memory masking material."""

    def __init__(
        self,
        vault: Vault,
        *,
        user_id: str,
        scope_id: str,
        secret_ttl_s: float = 1800.0,
    ) -> None:
        self._vault = vault
        self._user_id = user_id
        self._scope_id = scope_id
        self._ttl_s = secret_ttl_s
        self._values: dict[str, str] = {}

    @property
    def handles(self) -> tuple[str, ...]:
        return tuple(self._values)

    async def issue(self, value: str) -> str:
        """Store a primary secret in the Vault and hand out a handle (never the value)."""
        if not value:
            raise ValueError("empty secret")
        if len(value) > MAX_SECRET_CHARS:
            raise ValueError("secret is too long")
        if len(self._values) >= MAX_HANDLES:
            raise ValueError("too many secret handles in one run")
        handle = f"sec_{new_id()}"
        await self._vault.put(
            self._user_id,
            self._vault_scope(handle),
            SecretKind.TRANSIENT,
            value,
            expires_at=datetime.now(UTC) + timedelta(seconds=max(60.0, self._ttl_s)),
        )
        self._values[handle] = value
        return handle

    async def resolve(self, handle: str) -> str:
        """The value behind a handle this run issued; never returned to the model."""
        if not HANDLE_PATTERN.match(handle):
            raise ValueError("invalid secret handle")
        cached = self._values.get(handle)
        if cached is None:
            raise ValueError("unknown secret handle for this run")
        return cached

    def mask(self, text: str) -> str:
        """Replace every issued secret value in `text` with `***`."""
        if not text:
            return text
        masked = text
        for value in self._values.values():
            if len(value) >= MIN_MASKABLE_CHARS and value in masked:
                masked = masked.replace(value, MASK)
        return masked

    def clear(self) -> None:
        self._values.clear()

    def _vault_scope(self, handle: str) -> str:
        # The handle is part of the AAD scope: a handle cannot decrypt another run's slot.
        return f"{self._scope_id}:{handle}"


class _SecretRequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    hint: str | None = Field(default=None, max_length=300)
    timeout_s: float | None = Field(default=None, gt=0, le=3600)


class SecretRequestTool:
    """Ask the owner for a primary secret; the model gets a handle, never the value."""

    spec = ToolSpec(
        name="secret_request",
        description=(
            "Попросить у владельца первичный секрет (пароль, код MFA, API-ключ) через защищённый "
            "ввод. Возвращает только handle: значение не попадает в промпт, результат и трассу. "
            "Используйте handle в secret_use, чтобы подставить его в поле/HTTP."
        ),
        parameters=object_schema(
            {
                "question": {"type": "string", "minLength": 1, "maxLength": 1000},
                "hint": {"type": ["string", "null"], "maxLength": 300},
                "timeout_s": {"type": ["number", "null"], "minimum": 1, "maximum": 3600},
            },
            required=["question"],
        ),
        timeout_s=600.0,
        policy="auth",
    )

    def __init__(self, broker: SecretBroker) -> None:
        self._broker = broker

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = _SecretRequestArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        ask = context.ask
        if ask is None:
            return ToolResult.failure(
                "no_ask_channel",
                "Канал вопросов недоступен: секрет нельзя запросить в этом run.",
            )
        answer = await ask(
            AskQuestion(
                question=args.question,
                input_kind="secret",
                placeholder=args.hint,
                timeout_s=args.timeout_s,
            )
        )
        if answer.cancelled or not answer.text:
            return ToolResult.failure("cancelled", "Владелец не ввёл секрет.")
        try:
            handle = await self._broker.issue(answer.text)
        except (ValueError, BotaiError) as exc:
            return ToolResult.failure("secret_unavailable", f"Секрет не сохранён: {exc}")
        return ToolResult.success(
            summary="Секрет получен; модели доступен только handle",
            data={
                "handle": handle,
                "note": "Значение скрыто. Подставьте handle через secret_use.",
            },
        )


class _SecretUseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    kind: Literal["browser_field", "http"] = "http"
    # browser_field
    target: dict[str, Any] | None = None
    timeout_s: float | None = Field(default=None, gt=0, le=300)
    # http
    method: str = "GET"
    url: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    placement: Literal["header", "json", "form"] = "header"
    name: str = Field(default="Authorization", min_length=1, max_length=200)
    format: str = Field(default=_VALUE_SLOT, max_length=300)
    follow_redirects: bool = True


def _with_secret(template: str, secret: str, *, raw_if_plain: bool) -> str:
    if _VALUE_SLOT in template:
        return template.replace(_VALUE_SLOT, secret)
    if raw_if_plain and not template:
        return secret
    return f"{template}{secret}" if template else secret


class SecretUseTool:
    """Apply a secret handle to a browser field or an HTTP request; value stays out of results."""

    spec = ToolSpec(
        name="secret_use",
        description=(
            "Подставить первичный секрет по handle, не видя значения: в поле браузера "
            "(kind=browser_field, target как у browser_click) или в HTTP-запрос "
            "(kind=http: placement header/json/form, name — имя заголовка/поля, format — "
            "шаблон с {secret}, например 'Bearer {secret}'). Ответ HTTP и страница возвращаются "
            "с замаскированным значением."
        ),
        parameters=object_schema(
            {
                "handle": {"type": "string", "minLength": 1, "maxLength": 64},
                "kind": {"type": "string", "enum": ["browser_field", "http"], "default": "http"},
                "target": {"type": ["object", "null"]},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
                "method": {"type": "string", "maxLength": 16, "default": "GET"},
                "url": {"type": ["string", "null"], "maxLength": 4096},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {},
                "placement": {
                    "type": "string",
                    "enum": ["header", "json", "form"],
                    "default": "header",
                },
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "format": {"type": "string", "maxLength": 300},
                "follow_redirects": {"type": "boolean", "default": True},
            },
            required=["handle"],
        ),
        timeout_s=320.0,
        policy="auth",
        serial_only=True,
    )

    def __init__(
        self,
        broker: SecretBroker,
        *,
        control: BrowserControl | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._broker = broker
        self._control = control
        self._transport = transport

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _SecretUseArgs.model_validate(arguments)
            secret = await self._broker.resolve(args.handle)
        except ValidationError as exc:
            return _invalid(exc)
        except ValueError as exc:
            return ToolResult.failure("unknown_handle", str(exc))
        if args.kind == "browser_field":
            return await self._browser_field(args, secret)
        return await self._http(args, secret)

    async def _browser_field(self, args: _SecretUseArgs, secret: str) -> ToolResult:
        if self._control is None:
            return ToolResult.failure("no_browser", "Браузер в этом run недоступен.")
        if args.target is None:
            return ToolResult.failure(
                "invalid_arguments", "browser_field требует target (selector/label/…)."
            )
        try:
            info = await self._control.type(args.target, secret, timeout_s=args.timeout_s)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, self._broker.mask(exc.detail))
        return ToolResult.success(
            summary="Секрет подставлен в поле (значение скрыто)",
            data={"url": info.url, "title": info.title, "masked": True},
        )

    async def _http(self, args: _SecretUseArgs, secret: str) -> ToolResult:
        if not args.url:
            return ToolResult.failure("invalid_arguments", "kind=http требует url.")
        method = args.method.strip().upper()
        if not _METHOD.match(method):
            return ToolResult.failure("invalid_arguments", "Некорректный HTTP-метод.")
        headers = dict(args.headers)
        json_body: Any = None
        form_body: dict[str, Any] | None = None
        if args.placement == "header":
            headers[args.name] = _with_secret(args.format, secret, raw_if_plain=True)
        else:
            if args.body is None:
                body: dict[str, Any] = {}
            elif isinstance(args.body, dict):
                body = dict(args.body)
            else:
                return ToolResult.failure(
                    "invalid_arguments", f"{args.placement} placement ждёт body-объект."
                )
            body[args.name] = _with_secret(args.format, secret, raw_if_plain=True)
            if args.placement == "json":
                json_body = body
            else:
                form_body = body
        started = time.monotonic()
        try:
            async with build_client(
                follow_redirects=args.follow_redirects,
                transport=self._transport,
                timeout=args.timeout_s or 30.0,
            ) as client:
                response = await client.request(
                    method, args.url, headers=headers, json=json_body, data=form_body
                )
        except httpx.HTTPError as exc:
            return ToolResult.failure(
                "http_failed",
                f"HTTP-запрос с секретом не выполнен: {type(exc).__name__}",
                retryable=True,
            )
        text = self._broker.mask(_safe_text(response))
        headers_out = {
            name: ("***" if name.lower() in _MASKED_RESPONSE_HEADERS else self._broker.mask(value))
            for name, value in response.headers.items()
        }
        data = {
            "status": response.status_code,
            "url": self._broker.mask(str(response.url)),
            "headers": headers_out,
            "text": text[:MAX_HTTP_BODY_CHARS],
            "truncated": len(text) > MAX_HTTP_BODY_CHARS,
            "secret_sent": True,
            "placement": args.placement,
            "name": args.name,
        }
        return ToolResult.success(
            summary=f"Секрет подставлен в {args.placement}; HTTP {response.status_code}",
            data=data,
            truncated=bool(data["truncated"]),
            cost_ms=int((time.monotonic() - started) * 1000),
        )


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (UnicodeError, httpx.DecodingError):
        return ""


def build_secret_tools(
    broker: SecretBroker,
    *,
    control: BrowserControl | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ToolHandler]:
    """`secret_request` + `secret_use` for one run (both share the same handle registry)."""
    return [
        SecretRequestTool(broker),
        SecretUseTool(broker, control=control, transport=transport),
    ]


__all__ = ["SecretBroker", "SecretRequestTool", "SecretUseTool", "build_secret_tools"]
