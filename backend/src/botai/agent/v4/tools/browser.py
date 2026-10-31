"""`browser_*` tools: full browser control for the v4 agent (plan 1.3, architecture §4.4).

These are the runtime handlers over `botai.browser.control.BrowserControl`: navigation, clicks,
typing, keys, selects, waits, reading, JavaScript, screenshots, cookies/storage, network log and
HAR, tabs, downloads/uploads and session (storage state) export/import. The control decides what
the browser can do; this module only turns its results into `ToolResult`s and bounds what enters
the model context.

Two things this layer guarantees:

* **nothing is written outside the run's workspace** — screenshots, downloads, HAR and session
  files go to files under `ToolContext.workspace`; upload paths must resolve inside it;
* **primary secrets are masked in observations** — the injected `mask` callable replaces values
  the run obtained through the secret broker in network bodies, HAR, read text, JS results and
  error details. Cookie values are session material (D-008) and stay readable through the
  explicit `browser_cookies` tool; the passive network/HAR observations mask credential headers
  (`authorization`, `cookie`, `set-cookie`, `x-api-key`, …) so a trace does not spread them.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.protocol import (
    ToolArtifact,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.agent.v4.tools.fs import WorkspaceEscape, resolve_in_workspace
from botai.browser.control import BrowserControl
from botai.core.errors import BotaiError, ErrorCode

Mask = Callable[[str], str]

MAX_READ_CHARS = 100_000
MAX_EVAL_JSON_CHARS = 100_000
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_NETWORK_ENTRIES = 500
MAX_HAR_ENTRIES = 2_000
MAX_COOKIES = 500

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)

_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Элемент: одна стратегия — selector (CSS/text=/xpath=), text (видимый текст), "
        "role+name (ARIA), test_id, label или x/y (только клик)."
    ),
    "properties": {
        "selector": {"type": "string", "maxLength": 2000},
        "text": {"type": "string", "maxLength": 500},
        "role": {"type": "string", "maxLength": 64},
        "name": {"type": "string", "maxLength": 500},
        "test_id": {"type": "string", "maxLength": 200},
        "label": {"type": "string", "maxLength": 500},
        "x": {"type": "number"},
        "y": {"type": "number"},
    },
}


def _identity(value: str) -> str:
    return value


def _safe_relpath(workspace: Path, relative: str) -> Path:
    try:
        return resolve_in_workspace(workspace, relative)
    except WorkspaceEscape as exc:
        raise BotaiError(ErrorCode.VALIDATION, str(exc)) from None


def _inside(workspace: Path, path: str | Path) -> Path:
    """A created file must be inside the workspace; return its relative path if so."""
    root = workspace.resolve()
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise BotaiError(
            ErrorCode.SOURCE_UNAVAILABLE, "Файл оказался вне workspace задачи"
        ) from None
    return resolved


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _masked_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: ("***" if name.lower() in SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }


def _invalid(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


def _failure(exc: BotaiError) -> ToolResult:
    retryable = exc.code in {ErrorCode.SOURCE_UNAVAILABLE}
    return ToolResult.failure(exc.code.value, exc.detail or exc.code.value, retryable=retryable)


class _BrowserTool:
    """Shared plumbing: browser control, workspace, secret masker, error mapping."""

    spec: ClassVar[ToolSpec]

    def __init__(
        self,
        *,
        control: BrowserControl,
        workspace: Path,
        mask: Mask | None = None,
    ) -> None:
        self._control = control
        self._workspace = workspace.resolve()
        self._mask = mask or _identity

    @staticmethod
    def _info(info: Any) -> dict[str, Any]:
        data = info.to_dict() if hasattr(info, "to_dict") else info
        return dict(data)

    def _ok(
        self,
        *,
        summary: str,
        data: Any = None,
        artifacts: tuple[ToolArtifact, ...] = (),
        truncated: bool = False,
        cost_ms: int = 0,
    ) -> ToolResult:
        return ToolResult.success(
            summary=summary,
            data=data,
            artifacts=artifacts,
            truncated=truncated,
            cost_ms=cost_ms,
        )

    def _file(self, relative: str) -> Path:
        target = _safe_relpath(self._workspace, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _store_bytes(self, relative: str, data: bytes, *, mode: str = "wb") -> Path:
        target = self._file(relative)
        with target.open(mode) as handle:
            handle.write(data)
        return target

    def _read_eval(self, result: Any) -> Any:
        return _mask_value(result, self._mask)


def _mask_value(value: Any, mask: Mask) -> Any:
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [_mask_value(item, mask) for item in value]
    if isinstance(value, dict):
        return {str(key): _mask_value(item, mask) for key, item in value.items()}
    return value


# --- navigation and actions --------------------------------------------------------------------


class _NavigateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=4096)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load"
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserNavigateTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_navigate",
        description=(
            "Открыть произвольный http(s)-адрес в браузере агента (редиректы, IP, любые хосты). "
            "Возвращает url/title/активную вкладку."
        ),
        parameters=object_schema(
            {
                "url": {"type": "string", "minLength": 8, "maxLength": 4096},
                "wait_until": {
                    "type": "string",
                    "enum": ["commit", "domcontentloaded", "load", "networkidle"],
                    "default": "load",
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            },
            required=["url"],
        ),
        timeout_s=60.0,
        serial_only=True,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _NavigateArgs.model_validate(arguments)
            info = await self._control.navigate(
                args.url, wait_until=args.wait_until, timeout_s=args.timeout_s
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary=f"Открыта {info.url}", data=self._info(info))


class _OpenArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, max_length=4096)
    new_tab: bool = False
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load"
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserOpenTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_open",
        description=(
            "Открыть кабинет/страницу: при необходимости — в новой вкладке, затем навигация. "
            "Браузер запущен на весь run; этот инструмент управляет вкладками, а не процессом."
        ),
        parameters=object_schema(
            {
                "url": {"type": ["string", "null"], "maxLength": 4096},
                "new_tab": {"type": "boolean", "default": False},
                "wait_until": {
                    "type": "string",
                    "enum": ["commit", "domcontentloaded", "load", "networkidle"],
                    "default": "load",
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            }
        ),
        timeout_s=60.0,
        serial_only=True,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _OpenArgs.model_validate(arguments)
            if args.new_tab:
                info = await self._control.new_tab(None)
                if args.url is not None:
                    info = await self._control.navigate(
                        args.url, wait_until=args.wait_until, timeout_s=args.timeout_s
                    )
            elif args.url is not None:
                info = await self._control.navigate(
                    args.url, wait_until=args.wait_until, timeout_s=args.timeout_s
                )
            else:
                info = await self._control.page_info()
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary=f"Открыта {info.url}", data=self._info(info))


class _ClickArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=5)
    modifiers: list[str] = Field(default_factory=list, max_length=4)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserClickTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_click",
        description="Нажать элемент страницы (CSS/текст/роль/test id/label/координаты).",
        parameters=object_schema(
            {
                "target": _TARGET_SCHEMA,
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                },
                "click_count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["Control", "Shift", "Alt", "Meta"]},
                    "maxItems": 4,
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            },
            required=["target"],
        ),
        timeout_s=60.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _ClickArgs.model_validate(arguments)
            info = await self._control.click(
                args.target,
                button=args.button,
                click_count=args.click_count,
                modifiers=args.modifiers,
                timeout_s=args.timeout_s,
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary=f"Нажато; url {info.url}", data=self._info(info))


class _TypeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    text: str = Field(max_length=10_000)
    clear: bool = True
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserTypeTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_type",
        description=(
            "Ввести текст в поле. Для первичных секретов используйте secret_use (браузерное поле), "
            "чтобы значение не попало в контекст и трассу."
        ),
        parameters=object_schema(
            {
                "target": _TARGET_SCHEMA,
                "text": {"type": "string", "maxLength": 10000},
                "clear": {"type": "boolean", "default": True},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            },
            required=["target", "text"],
        ),
        timeout_s=60.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _TypeArgs.model_validate(arguments)
            info = await self._control.type(
                args.target, args.text, clear=args.clear, timeout_s=args.timeout_s
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary="Текст введён", data=self._info(info))


class _PressArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any] | None = None
    key: str = Field(min_length=1, max_length=64)
    modifiers: list[str] = Field(default_factory=list, max_length=4)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserPressTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_press",
        description="Нажать клавишу/комбинацию (Enter, Control+A, …).",
        parameters=object_schema(
            {
                "target": {
                    "type": ["object", "null"],
                    "description": "Элемент страницы (см. target).",
                },
                "key": {"type": "string", "minLength": 1, "maxLength": 64},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["Control", "Shift", "Alt", "Meta"]},
                    "maxItems": 4,
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            },
            required=["key"],
        ),
        timeout_s=60.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _PressArgs.model_validate(arguments)
            info = await self._control.press(
                args.target, args.key, modifiers=args.modifiers, timeout_s=args.timeout_s
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary="Клавиша нажата", data=self._info(info))


class _SelectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    value: str | None = Field(default=None, max_length=1000)
    label: str | None = Field(default=None, max_length=1000)
    index: int | None = Field(default=None, ge=0)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserSelectTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_select",
        description="Выбрать значение в <select> по value, label или index.",
        parameters=object_schema(
            {
                "target": _TARGET_SCHEMA,
                "value": {"type": ["string", "null"], "maxLength": 1000},
                "label": {"type": ["string", "null"], "maxLength": 1000},
                "index": {"type": ["integer", "null"], "minimum": 0},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
            },
            required=["target"],
        ),
        timeout_s=60.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _SelectArgs.model_validate(arguments)
            info = await self._control.select(
                args.target,
                value=args.value,
                label=args.label,
                index=args.index,
                timeout_s=args.timeout_s,
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary="Значение выбрано", data=self._info(info))


class _WaitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["selector", "text", "network_idle", "timeout", "navigation"]
    value: str | float | None = None
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserWaitTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_wait",
        description="Дождаться селектора, текста, network idle, навигации или просто времени.",
        parameters=object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["selector", "text", "network_idle", "timeout", "navigation"],
                },
                "value": {"type": ["string", "number", "null"]},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.1, "maximum": 300},
            },
            required=["kind"],
        ),
        timeout_s=120.0,
        serial_only=True,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _WaitArgs.model_validate(arguments)
            info = await self._control.wait(args.kind, args.value, timeout_s=args.timeout_s)
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary="Ожидание выполнено", data=self._info(info))


# --- reading -----------------------------------------------------------------------------------


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["text", "dom", "html", "links", "tables", "aria"] = "text"
    target: dict[str, Any] | None = None
    max_chars: int = Field(default=20_000, ge=1, le=MAX_READ_CHARS)


class BrowserReadTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_read",
        description=(
            "Прочитать текущую страницу: text (видимый текст), dom/html, links, tables, aria. "
            "Результат — данные страницы; большие значения усекаются (truncated)."
        ),
        parameters=object_schema(
            {
                "mode": {
                    "type": "string",
                    "enum": ["text", "dom", "html", "links", "tables", "aria"],
                    "default": "text",
                },
                "target": {
                    "type": ["object", "null"],
                    "description": "Элемент страницы (см. target).",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_CHARS,
                    "default": 20_000,
                },
            }
        ),
        timeout_s=60.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _ReadArgs.model_validate(arguments)
            text = await self._control.read(args.mode, args.target, max_chars=args.max_chars)
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        masked = self._mask(text)
        truncated = len(masked) >= args.max_chars
        return self._ok(
            summary=f"Прочитано {args.mode}: {len(masked)} символов",
            data={"mode": args.mode, "text": masked, "url": self._control.url},
            truncated=truncated,
        )


class _EvalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=200_000)
    arg: Any = None
    await_promise: bool = True
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserEvalTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_eval",
        description=(
            "Выполнить JavaScript в активной странице и вернуть JSON-результат. expression — "
            "выражение или функция; arg передаётся аргументом функции."
        ),
        parameters=object_schema(
            {
                "expression": {"type": "string", "minLength": 1, "maxLength": 200000},
                "arg": {},
                "await_promise": {"type": "boolean", "default": True},
                "timeout_s": {"type": ["number", "null"], "minimum": 0.1, "maximum": 300},
            },
            required=["expression"],
        ),
        timeout_s=60.0,
        serial_only=True,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _EvalArgs.model_validate(arguments)
            result = await self._control.eval_js(
                args.expression,
                arg=args.arg,
                await_promise=args.await_promise,
                timeout_s=args.timeout_s,
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        masked = _mask_value(result, self._mask)
        try:
            encoded = json.dumps(masked, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            encoded = json.dumps(str(masked), ensure_ascii=False)
        truncated = len(encoded) > MAX_EVAL_JSON_CHARS
        if truncated:
            masked = {"truncated": True, "text": encoded[:MAX_EVAL_JSON_CHARS]}
        return self._ok(summary="JS выполнен", data=masked, truncated=truncated)


# --- screenshots, cookies, storage, network, tabs ----------------------------------------------


class _ScreenshotArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_page: bool = False
    target: dict[str, Any] | None = None
    image_format: Literal["png", "jpeg"] = "png"
    quality: int | None = Field(default=None, ge=1, le=100)
    name: str = Field(default="screenshot", max_length=100)


class BrowserScreenshotTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_screenshot",
        description=(
            "Сделать скриншот страницы или элемента и сохранить файл в workspace задачи "
            "(screenshots/<name>-<ms>.png|jpeg). Возвращает путь, sha256 и размер."
        ),
        parameters=object_schema(
            {
                "full_page": {"type": "boolean", "default": False},
                "target": {
                    "type": ["object", "null"],
                    "description": "Элемент страницы (см. target).",
                },
                "image_format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                "quality": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "name": {"type": "string", "maxLength": 100, "default": "screenshot"},
            }
        ),
        timeout_s=60.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _ScreenshotArgs.model_validate(arguments)
            data = await self._control.screenshot(
                full_page=args.full_page,
                target=args.target,
                image_format=args.image_format,
                quality=args.quality,
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        if len(data) > MAX_SCREENSHOT_BYTES:
            return ToolResult.failure("result_too_large", "Скриншот больше 10 МБ")
        suffix = "png" if args.image_format == "png" else "jpg"
        safe_name = "".join(char for char in args.name if char.isalnum() or char in "-_")[:60]
        relative = f"screenshots/{safe_name or 'screenshot'}-{int(time.time() * 1000)}.{suffix}"
        self._store_bytes(relative, data)
        digest = _sha256_bytes(data)
        return self._ok(
            summary=f"Скриншот сохранён: {relative}",
            data={"path": relative, "bytes": len(data), "sha256": digest},
            artifacts=(
                ToolArtifact(
                    id=relative,
                    kind="screenshot",
                    bytes=len(data),
                    sha256=digest,
                    url=self._control.url or None,
                ),
            ),
        )


class _CookiesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "set", "clear"] = "list"
    cookies: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_COOKIES)
    urls: list[str] = Field(default_factory=list, max_length=50)


class BrowserCookiesTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_cookies",
        description=(
            "Cookie активного контекста: list / set (name, value, domain/path или url) / clear. "
            "Cookie — рабочий материал агента (сессия), поэтому значения возвращаются."
        ),
        parameters=object_schema(
            {
                "action": {"type": "string", "enum": ["list", "set", "clear"], "default": "list"},
                "cookies": {
                    "type": "array",
                    "maxItems": MAX_COOKIES,
                    "items": {"type": "object"},
                    "description": "Для set: [{name, value, domain|url, path?}].",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 4096},
                    "maxItems": 50,
                },
            }
        ),
        timeout_s=30.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _CookiesArgs.model_validate(arguments)
            if args.action == "list":
                cookies = await self._control.cookies(args.urls or None)
                rows = [
                    {
                        "name": cookie.get("name", ""),
                        "value": cookie.get("value", ""),
                        "domain": cookie.get("domain", ""),
                        "path": cookie.get("path", ""),
                        "expires": cookie.get("expires"),
                        "httpOnly": cookie.get("httpOnly", False),
                        "secure": cookie.get("secure", True),
                    }
                    for cookie in cookies
                ]
                return self._ok(
                    summary=f"Cookies: {len(rows)}", data={"cookies": rows[:MAX_COOKIES]}
                )
            if args.action == "set":
                added = await self._control.set_cookies(args.cookies)
                return self._ok(summary=f"Установлено cookies: {added}", data={"added": added})
            await self._control.clear_cookies()
            return self._ok(summary="Cookies очищены", data={"cleared": True})
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)


class _StorageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["get", "set"] = "get"
    kind: Literal["local", "session"] = "local"
    data: dict[str, str] = Field(default_factory=dict, max_length=500)
    origin: str | None = Field(default=None, max_length=4096)
    clear: bool = False


class BrowserStorageTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_storage",
        description="localStorage/sessionStorage текущей страницы: get / set (данные сессии).",
        parameters=object_schema(
            {
                "action": {"type": "string", "enum": ["get", "set"], "default": "get"},
                "kind": {"type": "string", "enum": ["local", "session"], "default": "local"},
                "data": {"type": "object", "additionalProperties": {"type": "string"}},
                "origin": {"type": ["string", "null"], "maxLength": 4096},
                "clear": {"type": "boolean", "default": False},
            }
        ),
        timeout_s=30.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _StorageArgs.model_validate(arguments)
            if args.action == "get":
                items = await self._control.get_storage(args.kind, origin=args.origin)
                return self._ok(
                    summary=f"storage {args.kind}: {len(items)} записей",
                    data={"kind": args.kind, "items": items},
                )
            written = await self._control.set_storage(
                args.data, args.kind, origin=args.origin, clear=args.clear
            )
            return self._ok(
                summary=f"storage {args.kind}: записано {written}", data={"written": written}
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)


class _NetworkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["log", "clear", "har_start", "har_stop"] = "log"
    url_filter: str | None = Field(default=None, max_length=1000)
    max_entries: int = Field(default=100, ge=1, le=MAX_NETWORK_ENTRIES)
    name: str = Field(default="har", max_length=60)


class BrowserNetworkTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_network",
        description=(
            "Сеть браузера: log (метод/URL/статус/заголовки/тела) с маскировкой секретов; "
            "clear; har_start/har_stop — HAR 1.2 сохраняется файлом в workspace "
            "(browser/<name>-<ms>.har.json), возвращаются путь, размер и число записей."
        ),
        parameters=object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["log", "clear", "har_start", "har_stop"],
                    "default": "log",
                },
                "url_filter": {"type": ["string", "null"], "maxLength": 1000},
                "max_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_NETWORK_ENTRIES,
                    "default": 100,
                },
                "name": {"type": "string", "maxLength": 60, "default": "har"},
            }
        ),
        timeout_s=30.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _NetworkArgs.model_validate(arguments)
            if args.action == "clear":
                self._control.clear_network_log()
                return self._ok(summary="Сетевой лог очищен", data={"cleared": True})
            if args.action == "har_start":
                self._control.har_start()
                return self._ok(summary="HAR: запись начата", data={"recording": True})
            if args.action == "har_stop":
                har = self._control.har_stop()
                entries = har["log"]["entries"][-MAX_HAR_ENTRIES:]
                masked = _mask_har(entries, self._mask)
                relative = f"browser/{args.name}-{int(time.time() * 1000)}.har.json"
                data = json.dumps(masked, ensure_ascii=False).encode("utf-8")
                self._store_bytes(relative, data)
                digest = _sha256_bytes(data)
                return self._ok(
                    summary=f"HAR сохранён: {relative} ({len(entries)} записей)",
                    data={
                        "path": relative,
                        "entries": len(entries),
                        "bytes": len(data),
                        "sha256": digest,
                    },
                    artifacts=(
                        ToolArtifact(id=relative, kind="har", bytes=len(data), sha256=digest),
                    ),
                )
            entries = self._control.network_log()
            if args.url_filter:
                entries = [entry for entry in entries if args.url_filter in entry.url]
            selected = entries[-args.max_entries :]
            rows = [_mask_network_entry(entry, self._mask) for entry in selected]
            return self._ok(
                summary=f"Сеть: {len(rows)} записей (из {len(entries)})",
                data={"entries": rows, "total": len(entries)},
                truncated=len(entries) > len(rows),
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)


class _TabsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "new", "switch", "close"] = "list"
    tab_id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=4096)


class BrowserTabsTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_tabs",
        description="Вкладки браузера: list / new (url?) / switch (tab_id) / close (tab_id).",
        parameters=object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "new", "switch", "close"],
                    "default": "list",
                },
                "tab_id": {"type": ["string", "null"], "maxLength": 64},
                "url": {"type": ["string", "null"], "maxLength": 4096},
            }
        ),
        timeout_s=60.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _TabsArgs.model_validate(arguments)
            if args.action == "list":
                tabs = await self._control.tabs()
                return self._ok(
                    summary=f"Вкладок: {len(tabs)}",
                    data={"tabs": [tab.to_dict() for tab in tabs]},
                )
            if args.action == "new":
                info = await self._control.new_tab(args.url)
                return self._ok(summary="Новая вкладка", data=self._info(info))
            if args.action == "switch":
                if not args.tab_id:
                    return ToolResult.failure("invalid_arguments", "switch требует tab_id")
                info = await self._control.switch_tab(args.tab_id)
                return self._ok(summary="Вкладка выбрана", data=self._info(info))
            if not args.tab_id:
                return ToolResult.failure("invalid_arguments", "close требует tab_id")
            info = await self._control.close_tab(args.tab_id)
            return self._ok(summary="Вкладка закрыта", data=self._info(info))
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)


# --- downloads, uploads, session state ---------------------------------------------------------


class _DownloadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    filename: str | None = Field(default=None, max_length=200)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserDownloadTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_download",
        description=(
            "Скачать файл по ссылке/кнопке в workspace задачи (downloads/). Возвращает путь "
            "относительно workspace, размер и sha256."
        ),
        parameters=object_schema(
            {
                "target": _TARGET_SCHEMA,
                "filename": {"type": ["string", "null"], "maxLength": 200},
                "timeout_s": {"type": ["number", "null"], "minimum": 1, "maximum": 300},
            },
            required=["target"],
        ),
        timeout_s=120.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _DownloadArgs.model_validate(arguments)
            directory = self._workspace / "downloads"
            directory.mkdir(parents=True, exist_ok=True)
            info = await self._control.download(
                args.target,
                save_dir=directory,
                filename=args.filename,
                timeout_s=args.timeout_s,
            )
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        try:
            relative = str(_inside(self._workspace, info.path).relative_to(self._workspace))
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(
            summary=f"Скачано: {relative} ({info.size_bytes} байт)",
            data={
                "path": relative,
                "bytes": info.size_bytes,
                "sha256": info.sha256,
                "suggested_filename": info.suggested_filename,
                "url": info.url,
            },
            artifacts=(
                ToolArtifact(
                    id=relative,
                    kind="download",
                    bytes=info.size_bytes,
                    sha256=info.sha256,
                    url=info.url or None,
                ),
            ),
        )


class _UploadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    paths: list[str] = Field(min_length=1, max_length=20)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class BrowserUploadTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_upload",
        description="Загрузить файлы из workspace задачи в input[type=file].",
        parameters=object_schema(
            {
                "target": _TARGET_SCHEMA,
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": "string", "maxLength": 4096},
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 1, "maximum": 300},
            },
            required=["target", "paths"],
        ),
        timeout_s=120.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _UploadArgs.model_validate(arguments)
            files = [_safe_relpath(self._workspace, relative) for relative in args.paths]
            missing = [str(path) for path in files if not path.is_file()]
            if missing:
                return ToolResult.failure("not_found", f"Файлы не найдены: {', '.join(missing)}")
            uploaded = await self._control.upload(args.target, files, timeout_s=args.timeout_s)
        except ValidationError as exc:
            return _invalid(exc)
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(summary=f"Загружено файлов: {uploaded}", data={"uploaded": uploaded})


class _SessionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["export", "import"] = "export"
    name: str = Field(default="session", max_length=60)
    include_state: bool = False
    state: dict[str, Any] | None = None
    path: str | None = Field(default=None, max_length=4096)
    indexed_db: bool = False


class BrowserSessionTool(_BrowserTool):
    spec = ToolSpec(
        name="browser_session",
        description=(
            "Сессия браузера (cookies + localStorage): export в sessions/<name>.json рабочей "
            "области или import из state/файла. Это рабочий материал агента (D-008); "
            "первичные секреты сюда не попадают."
        ),
        parameters=object_schema(
            {
                "action": {"type": "string", "enum": ["export", "import"], "default": "export"},
                "name": {"type": "string", "maxLength": 60, "default": "session"},
                "include_state": {"type": "boolean", "default": False},
                "state": {"type": ["object", "null"]},
                "path": {"type": ["string", "null"], "maxLength": 4096},
                "indexed_db": {"type": "boolean", "default": False},
            }
        ),
        timeout_s=60.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            args = _SessionArgs.model_validate(arguments)
            if args.action == "export":
                state = await self._control.export_storage_state(indexed_db=args.indexed_db)
                relative = f"sessions/{_safe_name(args.name)}.json"
                encoded = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
                self._store_bytes(relative, encoded)
                # Cookie values are session material, but a passive export stays compact: the file
                # is what the agent uses next time; inline state is opt-in.
                data: dict[str, Any] = {
                    "path": relative,
                    "cookies": len(state.get("cookies") or []),
                    "origins": len(state.get("origins") or []),
                    "bytes": len(encoded),
                }
                if args.include_state:
                    data["state"] = state
                return self._ok(summary=f"Сессия сохранена: {relative}", data=data)
            if args.state is not None:
                state = args.state
            elif args.path:
                path = _safe_relpath(self._workspace, args.path)
                state = json.loads(path.read_text(encoding="utf-8"))
            else:
                return ToolResult.failure(
                    "invalid_arguments", "import требует state или path в workspace"
                )
            imported = await self._control.import_storage_state(state)
        except ValidationError as exc:
            return _invalid(exc)
        except (OSError, json.JSONDecodeError):
            return ToolResult.failure("invalid_arguments", "Файл сессии не читается как JSON")
        except BotaiError as exc:
            return _failure(exc)
        return self._ok(
            summary=(
                "Сессия импортирована: "
                f"cookies {imported.cookies_added}, origins {imported.origins_applied}"
            ),
            data=imported.to_dict(),
        )


def _safe_name(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_")[:60] or "session"


def _mask_network_entry(entry: Any, mask: Mask) -> dict[str, Any]:
    data = entry.to_dict()
    data["request_headers"] = _masked_headers(data.get("request_headers") or {})
    data["response_headers"] = _masked_headers(data.get("response_headers") or {})
    data["request_body"] = mask(data["request_body"]) if data.get("request_body") else None
    data["response_body"] = mask(data["response_body"]) if data.get("response_body") else None
    return data


def _mask_har(entries: list[dict[str, Any]], mask: Mask) -> dict[str, Any]:
    masked: list[dict[str, Any]] = []
    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        request["headers"] = [
            {
                "name": str(item.get("name", "")),
                "value": (
                    "***"
                    if str(item.get("name", "")).lower() in SENSITIVE_HEADERS
                    else str(item.get("value", ""))
                ),
            }
            for item in request.get("headers") or []
        ]
        response["headers"] = [
            {
                "name": str(item.get("name", "")),
                "value": (
                    "***"
                    if str(item.get("name", "")).lower() in SENSITIVE_HEADERS
                    else str(item.get("value", ""))
                ),
            }
            for item in response.get("headers") or []
        ]
        if isinstance(request.get("postData"), dict):
            text = request["postData"].get("text")
            if isinstance(text, str):
                request["postData"]["text"] = mask(text)
        content = response.get("content")
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            content["text"] = mask(content["text"])
        masked.append(entry)
    return {
        "log": {"version": "1.2", "creator": {"name": "BotAI", "version": "v4"}, "entries": masked}
    }


def build_browser_tools(
    control: BrowserControl, *, workspace: Path, mask: Mask | None = None
) -> list[ToolHandler]:
    """All `browser_*` handlers for one run; the caller owns the control's lifetime."""
    tools: list[ToolHandler] = [
        BrowserOpenTool(control=control, workspace=workspace, mask=mask),
        BrowserNavigateTool(control=control, workspace=workspace, mask=mask),
        BrowserClickTool(control=control, workspace=workspace, mask=mask),
        BrowserTypeTool(control=control, workspace=workspace, mask=mask),
        BrowserPressTool(control=control, workspace=workspace, mask=mask),
        BrowserSelectTool(control=control, workspace=workspace, mask=mask),
        BrowserWaitTool(control=control, workspace=workspace, mask=mask),
        BrowserReadTool(control=control, workspace=workspace, mask=mask),
        BrowserEvalTool(control=control, workspace=workspace, mask=mask),
        BrowserScreenshotTool(control=control, workspace=workspace, mask=mask),
        BrowserCookiesTool(control=control, workspace=workspace, mask=mask),
        BrowserStorageTool(control=control, workspace=workspace, mask=mask),
        BrowserNetworkTool(control=control, workspace=workspace, mask=mask),
        BrowserTabsTool(control=control, workspace=workspace, mask=mask),
        BrowserDownloadTool(control=control, workspace=workspace, mask=mask),
        BrowserUploadTool(control=control, workspace=workspace, mask=mask),
        BrowserSessionTool(control=control, workspace=workspace, mask=mask),
    ]
    return tools


__all__ = [
    "SENSITIVE_HEADERS",
    "BrowserClickTool",
    "BrowserCookiesTool",
    "BrowserDownloadTool",
    "BrowserEvalTool",
    "BrowserNavigateTool",
    "BrowserNetworkTool",
    "BrowserOpenTool",
    "BrowserPressTool",
    "BrowserReadTool",
    "BrowserScreenshotTool",
    "BrowserSelectTool",
    "BrowserSessionTool",
    "BrowserStorageTool",
    "BrowserTabsTool",
    "BrowserTypeTool",
    "BrowserUploadTool",
    "build_browser_tools",
]
