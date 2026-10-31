"""`http_request`: any HTTP(S) request from the run (plan 1.2, architecture §4.4).

Arbitrary hosts, methods, headers and bodies — the free harness has no allowlist (D-005). The
response is structured data: status, final URL, redirect chain, headers, body/text/JSON, byte
counts, optional file saved into the workspace. Transport failures are structured errors with a
`retryable` flag, not exceptions.

The client is created by `botai.agent.v4.http.build_client` (single factory);
`transport` for tests is accepted by `HttpRequestTool(transport=...)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from botai.agent.v4.http import build_client
from botai.agent.v4.protocol import ToolContext, ToolResult, ToolSpec, object_schema

HTTP_METHODS: tuple[str, ...] = (
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "TRACE",
)
DEFAULT_MAX_BYTES = 2_000_000
MAX_TOOL_TIMEOUT_S = 310.0


class HttpRequestArgs(BaseModel):
    """Validated arguments of one `http_request` call."""

    model_config = ConfigDict(extra="forbid")

    method: str = "GET"
    url: str = Field(min_length=8, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    body_kind: Literal["json", "form", "raw"] = "json"
    cookies: dict[str, str] = Field(default_factory=dict)
    follow_redirects: bool = True
    timeout_s: float = Field(default=30.0, ge=0.5, le=300)
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=1024, le=20_000_000)
    save_to: str | None = Field(default=None, max_length=1024)

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        method = value.strip().upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"method must be one of {', '.join(HTTP_METHODS)}")
        return method


class HttpRequestTool:
    """`http_request` handler and its model-facing schema."""

    spec = ToolSpec(
        name="http_request",
        description=(
            "Выполнить произвольный HTTP(S)-запрос от имени задачи: любой метод, URL, заголовки, "
            "тело, cookie. Ответ возвращается структурированно: status, URL после редиректов, "
            "заголовки, текст/JSON, размер. Большое тело можно сохранить в workspace через "
            "save_to и не тащить в контекст. Первичные секреты не передаются в URL."
        ),
        parameters=object_schema(
            {
                "method": {
                    "type": "string",
                    "enum": list(HTTP_METHODS),
                    "description": "HTTP-метод; по умолчанию GET.",
                },
                "url": {
                    "type": "string",
                    "minLength": 8,
                    "maxLength": 4096,
                    "description": "Полный URL, http:// или https://.",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Заголовки запроса.",
                },
                "body": {
                    "description": "Тело: объект/массив для json и form, строка для raw.",
                },
                "body_kind": {
                    "type": "string",
                    "enum": ["json", "form", "raw"],
                    "default": "json",
                },
                "cookies": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Cookie запроса (сессия — рабочий материал агента).",
                },
                "follow_redirects": {"type": "boolean", "default": True},
                "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 300},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 20000000,
                    "description": "Сколько байт ответа прочитать (лишнее отбрасывается).",
                },
                "save_to": {
                    "type": ["string", "null"],
                    "maxLength": 1024,
                    "description": "Путь в workspace задачи, куда сохранить тело ответа.",
                },
            },
            required=["url"],
        ),
        timeout_s=MAX_TOOL_TIMEOUT_S,
        # GET-like calls are reads. The phase-3 policy engine classifies mutating methods per
        # call (POST/PUT/DELETE on a site are `write_soft`/`write_destructive` there).
        policy="read",
        idempotent=False,
    )

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            args = HttpRequestArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure(
                "invalid_arguments", _invalid_detail(exc), cost_ms=_elapsed_ms(started)
            )
        parts = urlsplit(args.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ToolResult.failure(
                "invalid_url",
                "URL должен быть полным HTTP(S)-адресом.",
                hint="Например: https://example.test/path?x=1",
                cost_ms=_elapsed_ms(started),
            )
        if parts.username or parts.password:
            return ToolResult.failure(
                "credentials_in_url",
                "Логин и пароль в URL не передаются: они попадут в трассу.",
                hint="Сессия/cookie передаются отдельно; первичные секреты — через secret_use.",
                cost_ms=_elapsed_ms(started),
            )
        send_kwargs = _body_kwargs(args)
        if isinstance(send_kwargs, ToolResult):
            return send_kwargs
        try:
            async with asyncio.timeout(args.timeout_s + 1.0):
                data = await self._send(args, context, send_kwargs)
        except TimeoutError:
            return ToolResult.failure(
                "timeout",
                f"Запрос не уложился в {args.timeout_s:g} с.",
                retryable=True,
                cost_ms=_elapsed_ms(started),
            )
        except httpx.TimeoutException:
            return ToolResult.failure(
                "timeout",
                "Сервер не ответил вовремя.",
                retryable=True,
                cost_ms=_elapsed_ms(started),
            )
        except httpx.TransportError as exc:
            return ToolResult.failure(
                "network_error",
                f"Сетевая ошибка ({type(exc).__name__}).",
                retryable=True,
                cost_ms=_elapsed_ms(started),
            )
        except (httpx.InvalidURL, ValueError):
            return ToolResult.failure(
                "invalid_url", "URL не разобран.", cost_ms=_elapsed_ms(started)
            )
        if isinstance(data, ToolResult):
            return data
        return ToolResult.success(
            summary=data["summary"],
            data=data["data"],
            truncated=data["truncated"],
            cost_ms=_elapsed_ms(started),
        )

    async def _send(
        self,
        args: HttpRequestArgs,
        context: ToolContext,
        send_kwargs: dict[str, Any],
    ) -> dict[str, Any] | ToolResult:
        async with (
            build_client(
                timeout=httpx.Timeout(args.timeout_s),
                follow_redirects=args.follow_redirects,
                cookies=args.cookies or None,
                transport=self._transport,
            ) as client,
            client.stream(
                args.method,
                args.url,
                headers=args.headers,
                **send_kwargs,
            ) as response,
        ):
            raw, body_truncated = await _read_capped(response, args.max_bytes)
            headers = _headers_dict(response.headers.multi_items())
            history = [str(hop.url) for hop in response.history]
            status = response.status_code
            final_url = str(response.url)
            reason = response.reason_phrase
            content_type = response.headers.get("content-type", "")
        saved: dict[str, Any] | None = None
        if args.save_to is not None:
            written = _save_to_workspace(context.workspace, args.save_to, raw)
            if isinstance(written, ToolResult):
                return written
            saved = written
        textual = _is_textual(content_type, raw)
        text = raw.decode("utf-8", errors="replace") if textual else None
        parsed_json: Any = None
        if textual and text is not None and len(raw) <= args.max_bytes and "json" in content_type:
            try:
                parsed_json = json.loads(text)
            except ValueError:
                parsed_json = None
        data: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "url": final_url,
            "redirects": history,
            "headers": headers,
            "content_type": content_type,
            "bytes": len(raw),
            "body_truncated": body_truncated,
            "json": parsed_json,
            "text": text,
            "saved_to": saved["path"] if saved else None,
            "sha256": saved["sha256"] if saved else hashlib.sha256(raw).hexdigest(),
        }
        size_note = f", {len(raw)} байт" + (" (тело обрезано)" if body_truncated else "")
        summary = f"{status} {reason}{size_note}"
        return {
            "summary": summary,
            "data": data,
            "truncated": body_truncated,
        }


def _invalid_detail(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _body_kwargs(args: HttpRequestArgs) -> dict[str, Any] | ToolResult:
    if args.body is None:
        return {}
    if args.body_kind == "json":
        return {"json": args.body}
    if args.body_kind == "form":
        if not isinstance(args.body, dict):
            return ToolResult.failure(
                "invalid_arguments", "Для body_kind=form тело должно быть объектом."
            )
        return {"data": {str(key): _form_value(value) for key, value in args.body.items()}}
    if isinstance(args.body, dict | list):
        return ToolResult.failure(
            "invalid_arguments", "Для body_kind=raw тело должно быть строкой."
        )
    return {"content": str(args.body).encode()}


def _form_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        room = max_bytes - len(chunks)
        if len(chunk) > room:
            chunks.extend(chunk[:room])
            truncated = True
            break
        chunks.extend(chunk)
    return bytes(chunks), truncated


def _headers_dict(items: list[tuple[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for name, value in items:
        key = name.lower()
        merged[key] = f"{merged[key]}; {value}" if key in merged else value
    return merged


def _is_textual(content_type: str, raw: bytes) -> bool:
    lowered = content_type.lower()
    if any(marker in lowered for marker in ("json", "text/", "xml", "javascript", "x-www-form")):
        return True
    if not lowered and raw:
        return b"\x00" not in raw[:4096]
    return False


def _save_to_workspace(
    workspace: Path, relative: str, payload: bytes
) -> dict[str, Any] | ToolResult:
    from botai.agent.v4.tools.fs import resolve_in_workspace

    try:
        target = resolve_in_workspace(workspace, relative)
    except ValueError as exc:
        return ToolResult.failure("workspace_escape", str(exc))
    if target.is_dir():
        return ToolResult.failure("invalid_path", "save_to указывает на каталог.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        return ToolResult.failure("write_failed", f"Не удалось записать файл: {type(exc).__name__}")
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


__all__ = ["DEFAULT_MAX_BYTES", "HTTP_METHODS", "HttpRequestArgs", "HttpRequestTool"]
