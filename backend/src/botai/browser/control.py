"""Full browser control for the v4 agent harness: no allowlist, no read-only, session access.

`BrowserControl` is deliberately independent of the v3 login flow (`browser/login.py`) and the
fallback screen (`browser/screen.py`): it owns its own browser contexts and pages, and a page it
opens is never the login context of a connection. What it can do (contracts §4.4):

* navigation to any URL, redirect or IP literal;
* click / type / press / select / wait with targets by CSS, visible text, ARIA role, test id,
  label or screen coordinates;
* read text, live DOM, links, tables and an ARIA snapshot; evaluate JavaScript;
* screenshots as bytes; downloads and uploads;
* cookies and localStorage/sessionStorage get/set; storage state export/import;
* a bounded network log and HAR built from Playwright request/response events;
* several tabs and several isolated contexts; closing everything.

Isolation contract: every control session gets its own context(s); contexts of two controls never
share cookies or storage. Cookies, headers, bodies and typed values are working material for the
agent (D-008), so they are returned to the caller — and never written to `log`. Primary secrets
(password, MFA, calendar key-link) are still substituted by the secret broker, never passed here
as plain values that could be logged: this module logs lifecycle counters only.

The class is a library for `agent/tools/browser.py`; runtime tool schemas live there, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Cookie,
    Download,
    FilePayload,
    Geolocation,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
    StorageState,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from botai.browser.gateway import PlaywrightGateway
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_NAV_TIMEOUT_MS = 30_000
MAX_TEXT_CHARS = 20_000
MAX_DOM_CHARS = 200_000
MAX_READ_CHARS = 100_000
MAX_LINKS = 500
MAX_TABLES = 50
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 40
MAX_NETWORK_ENTRIES = 2_000
MAX_HAR_ENTRIES = 2_000
MAX_BODY_CHARS = 20_000
MAX_HEADERS = 80
MAX_HEADER_VALUE_CHARS = 4_000
MAX_DIALOGS = 20
MAX_TABS = 20
MAX_CONTEXTS = 8
MAX_SCRIPTS_CHARS = 200_000
DOWNLOAD_CHUNK = 64 * 1024
_UNSAFE_FILENAME = ("/", "\\", "\x00")
_MODIFIERS = {"Control", "Shift", "Alt", "Meta"}
_READ_MODES = ("text", "dom", "html", "links", "tables", "aria")
_WAIT_KINDS = ("selector", "text", "network_idle", "timeout", "navigation")
_BUTTONS = ("left", "right", "middle")


# --------------------------------------------------------------------------------------------
# Result models: plain data, JSON-serialisable in `to_dict`
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageInfo:
    """Where a control session is looking after an action."""

    url: str
    title: str
    tab_id: str
    context_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "tab_id": self.tab_id,
            "context_id": self.context_id,
        }


@dataclass(frozen=True, slots=True)
class TabInfo:
    tab_id: str
    context_id: str
    url: str
    title: str
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "context_id": self.context_id,
            "url": self.url,
            "title": self.title,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class ContextInfo:
    context_id: str
    tabs: int
    persistent: bool

    def to_dict(self) -> dict[str, Any]:
        return {"context_id": self.context_id, "tabs": self.tabs, "persistent": self.persistent}


@dataclass(frozen=True, slots=True)
class DownloadInfo:
    """A download that reached the server's disk. Content is never logged; the hash lets a trace
    reference it without keeping the bytes in the trace."""

    suggested_filename: str
    url: str
    path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggested_filename": self.suggested_filename,
            "url": self.url,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StorageImportResult:
    cookies_added: int
    origins_applied: int
    origins_failed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies_added": self.cookies_added,
            "origins_applied": self.origins_applied,
            "origins_failed": self.origins_failed,
        }


@dataclass(slots=True)
class NetworkEntry:
    """One request/response pair (or a failed request) as the page's own script made it."""

    method: str
    url: str
    resource_type: str = ""
    status: int | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    request_body: str | None = None
    response_body: str | None = None
    failed: bool = False
    started_at: float = 0.0
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "resource_type": self.resource_type,
            "status": self.status,
            "request_headers": dict(self.request_headers),
            "response_headers": dict(self.response_headers),
            "request_body": self.request_body,
            "response_body": self.response_body,
            "failed": self.failed,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }

    def to_har_entry(self) -> dict[str, Any]:
        content_type = self.response_headers.get("content-type", "")
        started = (
            datetime.fromtimestamp(self.started_at, UTC).isoformat()
            if self.started_at
            else datetime.now(UTC).isoformat()
        )
        body = self.response_body or ""
        size = len(body.encode("utf-8"))
        return {
            "startedDateTime": started,
            "time": self.duration_ms or 0.0,
            "request": {
                "method": self.method,
                "url": self.url,
                "httpVersion": "HTTP/1.1",
                "headers": _har_headers(self.request_headers),
                "queryString": [],
                "cookies": [],
                "headersSize": -1,
                "bodySize": len((self.request_body or "").encode("utf-8")),
                **({"postData": {"text": self.request_body}} if self.request_body else {}),
            },
            "response": {
                "status": self.status or 0,
                "statusText": "",
                "httpVersion": "HTTP/1.1",
                "headers": _har_headers(self.response_headers),
                "cookies": [],
                "content": {"size": size, "mimeType": content_type, "text": body},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": size,
            },
            "cache": {},
            "timings": {"send": 0, "wait": self.duration_ms or 0, "receive": 0},
        }


def _har_headers(headers: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in headers.items()]


# --------------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """How to point at one element (or a screen point). Exactly one strategy must be given."""

    selector: str | None = None
    text: str | None = None
    role: str | None = None
    name: str | None = None
    test_id: str | None = None
    label: str | None = None
    x: float | None = None
    y: float | None = None
    exact: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Target:
        coordinates = value.get("x") is not None or value.get("y") is not None
        if coordinates and (value.get("x") is None or value.get("y") is None):
            raise BotaiError(ErrorCode.VALIDATION, "Точка нажатия: нужны и x, и y")
        return cls(
            selector=_opt_str(value.get("selector")),
            text=_opt_str(value.get("text")),
            role=_opt_str(value.get("role")),
            name=_opt_str(value.get("name")),
            test_id=_opt_str(value.get("test_id")),
            label=_opt_str(value.get("label")),
            x=_opt_float(value.get("x")),
            y=_opt_float(value.get("y")),
            exact=bool(value.get("exact", False)),
        )

    def __post_init__(self) -> None:
        strategies = [
            self.selector,
            self.text,
            self.role,
            self.test_id,
            self.label,
            self.x is not None or self.y is not None,
        ]
        if sum(1 for value in strategies if value) != 1:
            raise BotaiError(
                ErrorCode.VALIDATION,
                "Цель: укажите ровно одно из selector/text/role/test_id/label или координаты x,y",
            )
        if self.role is not None and not self.role:
            raise BotaiError(ErrorCode.VALIDATION, "Цель: роль не должна быть пустой")

    @property
    def point(self) -> tuple[float, float] | None:
        if self.x is not None and self.y is not None:
            return self.x, self.y
        return None

    def describe(self) -> str:
        if self.selector:
            return f"selector {self.selector[:120]}"
        if self.text:
            return f"text {self.text[:120]}"
        if self.role:
            tail = f" name {self.name[:120]}" if self.name else ""
            return f"role {self.role}{tail}"
        if self.test_id:
            return f"test id {self.test_id[:120]}"
        if self.label:
            return f"label {self.label[:120]}"
        return "точка на экране"


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def as_target(value: Target | Mapping[str, Any]) -> Target:
    if isinstance(value, Target):
        return value
    return Target.from_mapping(value)


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlConfig:
    """Options of one control browser session. Everything here is a capability, not a policy."""

    headless: bool = True
    sandbox: bool | None = None
    ignore_https_errors: bool = False
    viewport: tuple[int, int] = (1280, 800)
    locale: str = "ru-RU"
    timezone: str | None = None
    user_agent: str | None = None
    geolocation: tuple[float, float] | None = None
    permissions: tuple[str, ...] = ()
    proxy: str | None = None
    accept_downloads: bool = True
    capture_network_bodies: bool = False
    downloads_dir: Path | None = None
    # Uploads from disk are confined to this directory (the run's workspace/artifacts).
    # `(name, bytes)` payloads need no directory and are always allowed.
    upload_root: Path | None = None
    # Hook the runtime uses to mask primary secrets in captured bodies before they are stored
    # and before HAR is returned (password/MFA/key-link values never appear in a trace).
    redact_body: Callable[[str], str] | None = None
    default_timeout_ms: int = DEFAULT_TIMEOUT_MS
    navigation_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS

    def __post_init__(self) -> None:
        if self.viewport[0] < 160 or self.viewport[1] < 160:
            raise BotaiError(ErrorCode.VALIDATION, "Viewport слишком мал")
        if self.proxy is not None and not self.proxy.startswith(
            ("http://", "https://", "socks5://")
        ):
            raise BotaiError(ErrorCode.VALIDATION, "Прокси: ожидается http(s):// или socks5:// URL")


_CONTROL_BROWSER_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-breakpad",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
)


# --------------------------------------------------------------------------------------------
# Internal context/tab bookkeeping
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Tab:
    tab_id: str
    page: Page


@dataclass(slots=True)
class _ContextState:
    context_id: str
    context: BrowserContext
    persistent: bool
    tabs: list[_Tab] = field(default_factory=list)
    current: int = 0
    network: deque[NetworkEntry] = field(default_factory=lambda: deque(maxlen=MAX_NETWORK_ENTRIES))
    by_request: dict[Request, NetworkEntry] = field(default_factory=dict)
    dialogs: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_DIALOGS))
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def tab(self, tab_id: str | None = None) -> _Tab:
        if not self.tabs:
            raise BotaiError(ErrorCode.CONFLICT, "В контексте нет открытых вкладок")
        if tab_id is None:
            index = max(0, min(self.current, len(self.tabs) - 1))
            return self.tabs[index]
        for tab in self.tabs:
            if tab.tab_id == tab_id:
                return tab
        raise BotaiError(ErrorCode.NOT_FOUND, "Вкладка не найдена")


class BrowserControl:
    """One isolated agent browser: its own contexts, tabs and network log."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        browser: Browser | None,
        playwright: Playwright | None,
        owns_browser: bool,
        persistent: bool = False,
    ) -> None:
        self._config = config
        self._browser = browser
        self._playwright = playwright
        self._owns_browser = owns_browser
        self._persistent_mode = persistent
        self._contexts: list[_ContextState] = []
        self._current_context = 0
        self._closed = False
        self._har_entries: list[NetworkEntry] | None = None
        self._profile_lock: AbstractAsyncContextManager[object] | None = None
        self.stats: dict[str, int] = {
            "actions": 0,
            "requests": 0,
            "downloads": 0,
            "uploads": 0,
        }

    # -- lifecycle ---------------------------------------------------------------------------

    @classmethod
    async def launch(
        cls,
        *,
        gateway: PlaywrightGateway | None = None,
        config: ControlConfig | None = None,
        storage_state: StorageState | None = None,
    ) -> BrowserControl:
        """Start a full-control Chromium session.

        With `gateway`, the browser process is shared with its Playwright driver; contexts stay
        separate (the gateway's proxy/allowlist applies to its own login contexts only). Without
        it a private driver and browser are started and closed with this control.
        """
        cfg = config or ControlConfig()
        control = cls(config=cfg, browser=None, playwright=None, owns_browser=gateway is None)
        try:
            if gateway is not None:
                browser = await gateway.control_browser(headless=cfg.headless, sandbox=cfg.sandbox)
            else:
                playwright = await async_playwright().start()
                control._playwright = playwright
                browser = await playwright.chromium.launch(
                    headless=cfg.headless,
                    chromium_sandbox=cfg.sandbox if cfg.sandbox is not None else False,
                    args=list(_CONTROL_BROWSER_ARGS),
                )
            control._browser = browser
            context = await browser.new_context(**control._context_options(storage_state))
            control._register_context(context, persistent=False)
            await control._open_page(control._contexts[0])
            return control
        except BaseException:
            await control._teardown()
            raise

    @classmethod
    async def launch_persistent(
        cls,
        user_data_dir: Path | str,
        *,
        lock: AbstractAsyncContextManager[object] | None = None,
        gateway: PlaywrightGateway | None = None,
        config: ControlConfig | None = None,
    ) -> BrowserControl:
        """Start a Chromium on a persistent user-data directory (its own or shared driver).

        Cookies, localStorage, IndexedDB, cache and service workers survive between launches:
        that is the server-side persistent profile of a user. The directory is created 0700 when
        missing. Chromium corrupts a user-data directory opened twice, so a
        `BrowserProfileStore.lock(user, profile)` must be passed: this control enters it and
        releases it on close; a profile already open elsewhere fails with a clear conflict."""
        cfg = config or ControlConfig()
        directory = Path(user_data_dir)
        _secure_dir(directory)
        if lock is None:
            raise BotaiError(
                ErrorCode.CONFLICT,
                "Постоянный профиль открывается только под BrowserProfileStore.lock(user, profile)",
            )
        lock_entered = False
        try:
            await lock.__aenter__()
            lock_entered = True
        except BotaiError:
            raise
        except Exception as exc:  # pragma: no cover - defensive: a foreign lock type
            raise BotaiError(
                ErrorCode.CONFLICT, "Не удалось заблокировать профиль браузера"
            ) from exc
        if gateway is not None:
            driver = await gateway.playwright()
            owns_driver = False
        else:
            driver = await async_playwright().start()
            owns_driver = True
        control = cls(
            config=cfg,
            browser=None,
            playwright=driver if owns_driver else None,
            owns_browser=owns_driver,
            persistent=True,
        )
        control._profile_lock = lock if lock_entered else None
        try:
            context = await driver.chromium.launch_persistent_context(
                str(directory),
                headless=cfg.headless,
                chromium_sandbox=cfg.sandbox if cfg.sandbox is not None else False,
                args=list(_CONTROL_BROWSER_ARGS),
                **control._context_options(None, persistent=True),
            )
            control._register_context(context, persistent=True)
            pages = context.pages
            if pages:
                for page in pages:
                    control._register_page(control._contexts[0], page)
            else:
                await control._open_page(control._contexts[0])
            return control
        except BaseException:
            await control._teardown()
            raise

    async def __aenter__(self) -> BrowserControl:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def url(self) -> str:
        """URL of the active page (empty when everything is closed)."""
        tab = self._active_tab(required=False)
        return tab.page.url if tab is not None else ""

    def _context_options(
        self, storage_state: StorageState | None, *, persistent: bool = False
    ) -> dict[str, Any]:
        cfg = self._config
        options: dict[str, Any] = {
            "ignore_https_errors": cfg.ignore_https_errors,
            "user_agent": cfg.user_agent,
            "viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]},
            "locale": cfg.locale,
            "timezone_id": cfg.timezone,
            "geolocation": (
                cast(Geolocation, {"latitude": cfg.geolocation[0], "longitude": cfg.geolocation[1]})
                if cfg.geolocation
                else None
            ),
            "permissions": list(cfg.permissions),
            "accept_downloads": cfg.accept_downloads,
            "proxy": {"server": cfg.proxy} if cfg.proxy else None,
            "storage_state": storage_state,
        }
        if persistent:
            # A persistent context cannot restore a storage state file: it keeps its own.
            options.pop("storage_state", None)
        return options

    async def close(self) -> None:
        """Close every context, then the browser and driver if this control owns them."""
        if self._closed:
            return
        self._closed = True
        contexts = list(self._contexts)
        self._contexts.clear()
        for state in contexts:
            for task in list(state.tasks):
                task.cancel()
            if state.tasks:
                await asyncio.gather(*state.tasks, return_exceptions=True)
            with contextlib.suppress(PlaywrightError):
                await state.context.close()
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if self._owns_browser and browser is not None:
            with contextlib.suppress(PlaywrightError):
                await browser.close()
        if self._owns_browser and playwright is not None:
            with contextlib.suppress(PlaywrightError):
                await playwright.stop()
        lock, self._profile_lock = self._profile_lock, None
        if lock is not None:
            with contextlib.suppress(Exception):
                await lock.__aexit__(None, None, None)
        log.info(
            "browser control closed: %d contexts, %d actions, %d downloads",
            len(contexts),
            self.stats["actions"],
            self.stats["downloads"],
        )

    async def _teardown(self) -> None:
        self._closed = False
        await self.close()

    # -- contexts and tabs -------------------------------------------------------------------

    def _require(self) -> None:
        if self._closed:
            raise BotaiError(ErrorCode.CONFLICT, "Браузер уже закрыт")

    def _register_context(self, context: BrowserContext, *, persistent: bool) -> _ContextState:
        state = _ContextState(context_id=new_id(), context=context, persistent=persistent)
        self._contexts.append(state)
        context.on("page", self._on_page_handler(state))
        context.on("request", self._on_request_handler(state))
        context.on("response", self._on_response_handler(state))
        context.on("requestfailed", self._on_failed_handler(state))
        context.on("close", self._on_context_close_handler(state))
        log.debug("browser control context opened")
        return state

    def _register_page(self, state: _ContextState, page: Page) -> _Tab | None:
        for tab in state.tabs:
            if tab.page is page:
                return tab
        if len(state.tabs) >= MAX_TABS:
            raise BotaiError(ErrorCode.CONFLICT, "Слишком много вкладок: закройте лишние")
        tab = _Tab(tab_id=new_id(), page=page)
        state.tabs.append(tab)
        state.current = len(state.tabs) - 1
        page.set_default_timeout(self._config.default_timeout_ms)
        page.on("close", self._on_page_close_handler(state, page))
        page.on("dialog", self._on_dialog_handler(state))
        return tab

    def _on_page_handler(self, state: _ContextState) -> Callable[[Page], None]:
        def handle(page: Page) -> None:
            with contextlib.suppress(BotaiError):
                self._register_page(state, page)

        return handle

    def _on_page_close_handler(self, state: _ContextState, page: Page) -> Callable[[Page], None]:
        def handle(_: Page) -> None:
            for index, tab in enumerate(state.tabs):
                if tab.page is page:
                    state.tabs.pop(index)
                    state.current = max(0, min(state.current, len(state.tabs) - 1))
                    return

        return handle

    def _on_context_close_handler(self, state: _ContextState) -> Callable[[BrowserContext], None]:
        def handle(_: BrowserContext) -> None:
            if state in self._contexts and not self._closed:
                self._contexts.remove(state)
                if self._contexts:
                    self._current_context = min(self._current_context, len(self._contexts) - 1)

        return handle

    def _on_dialog_handler(self, state: _ContextState) -> Callable[[Any], None]:
        def handle(dialog: Any) -> None:
            message = getattr(dialog, "message", "")
            if isinstance(message, str):
                state.dialogs.append(message[:500])
            task = asyncio.get_running_loop().create_task(self._dismiss(dialog))
            state.tasks.add(task)
            task.add_done_callback(state.tasks.discard)

        return handle

    @staticmethod
    async def _dismiss(dialog: Any) -> None:
        with contextlib.suppress(PlaywrightError):
            await dialog.dismiss()

    async def _open_page(self, state: _ContextState) -> PageInfo:
        page = await state.context.new_page()
        tab = self._register_page(state, page)
        assert tab is not None
        return await self._page_info(state, tab)

    def _current_state(self) -> _ContextState:
        self._require()
        if not self._contexts:
            raise BotaiError(ErrorCode.CONFLICT, "Все контексты закрыты: откройте новый")
        return self._contexts[min(self._current_context, len(self._contexts) - 1)]

    def _active_tab(self, *, required: bool = True) -> _Tab | None:
        if self._closed:
            if required:
                raise BotaiError(ErrorCode.CONFLICT, "Браузер уже закрыт")
            return None
        if not self._contexts:
            if required:
                raise BotaiError(ErrorCode.CONFLICT, "Все контексты закрыты: откройте новый")
            return None
        state = self._contexts[min(self._current_context, len(self._contexts) - 1)]
        if not state.tabs:
            if required:
                raise BotaiError(ErrorCode.CONFLICT, "Нет открытых вкладок")
            return None
        return state.tab()

    def _page(self) -> Page:
        tab = self._active_tab()
        assert tab is not None
        if tab.page.is_closed():
            raise BotaiError(ErrorCode.CONFLICT, "Вкладка закрыта: откройте новую")
        return tab.page

    async def _page_info(self, state: _ContextState, tab: _Tab) -> PageInfo:
        title = ""
        with contextlib.suppress(PlaywrightError):
            title = await asyncio.wait_for(tab.page.title(), 2.0)
        return PageInfo(
            url=tab.page.url, title=title[:300], tab_id=tab.tab_id, context_id=state.context_id
        )

    async def page_info(self) -> PageInfo:
        state = self._current_state()
        return await self._page_info(state, state.tab())

    def contexts(self) -> list[ContextInfo]:
        return [
            ContextInfo(
                context_id=state.context_id, tabs=len(state.tabs), persistent=state.persistent
            )
            for state in self._contexts
        ]

    async def new_context(self, *, storage_state: StorageState | None = None) -> ContextInfo:
        self._require()
        if self._persistent_mode:
            raise BotaiError(
                ErrorCode.NOT_IMPLEMENTED,
                "У постоянного профиля один контекст; новую изоляцию даёт новый профиль",
            )
        if len(self._contexts) >= MAX_CONTEXTS:
            raise BotaiError(ErrorCode.CONFLICT, "Слишком много контекстов: закройте лишние")
        browser = self._browser
        if browser is None:
            raise BotaiError(ErrorCode.CONFLICT, "Браузер недоступен")
        context = await browser.new_context(**self._context_options(storage_state))
        state = self._register_context(context, persistent=False)
        await self._open_page(state)
        self._current_context = len(self._contexts) - 1
        return ContextInfo(context_id=state.context_id, tabs=len(state.tabs), persistent=False)

    def switch_context(self, context_id: str) -> ContextInfo:
        for index, state in enumerate(self._contexts):
            if state.context_id == context_id:
                self._current_context = index
                return ContextInfo(
                    context_id=state.context_id, tabs=len(state.tabs), persistent=state.persistent
                )
        raise BotaiError(ErrorCode.NOT_FOUND, "Контекст не найден")

    async def close_context(self, context_id: str) -> None:
        for index, state in enumerate(self._contexts):
            if state.context_id != context_id:
                continue
            self._contexts.pop(index)
            self._current_context = max(0, min(self._current_context, len(self._contexts) - 1))
            for task in list(state.tasks):
                task.cancel()
            with contextlib.suppress(PlaywrightError):
                await state.context.close()
            return
        raise BotaiError(ErrorCode.NOT_FOUND, "Контекст не найден")

    async def tabs(self) -> list[TabInfo]:
        state = self._current_state()
        active = state.tab().tab_id if state.tabs else ""
        result: list[TabInfo] = []
        for tab in state.tabs:
            title = ""
            with contextlib.suppress(PlaywrightError):
                title = await asyncio.wait_for(tab.page.title(), 1.0)
            result.append(
                TabInfo(
                    tab_id=tab.tab_id,
                    context_id=state.context_id,
                    url=tab.page.url,
                    title=title[:300],
                    active=tab.tab_id == active,
                )
            )
        return result

    async def new_tab(self, url: str | None = None) -> PageInfo:
        state = self._current_state()
        page = await state.context.new_page()
        tab = self._register_page(state, page)
        assert tab is not None
        state.current = state.tabs.index(tab)
        if url is not None:
            await self.navigate(url)
        return await self._page_info(state, tab)

    async def switch_tab(self, tab_id: str) -> PageInfo:
        state = self._current_state()
        for index, tab in enumerate(state.tabs):
            if tab.tab_id == tab_id:
                if tab.page.is_closed():
                    raise BotaiError(ErrorCode.CONFLICT, "Вкладка закрыта")
                state.current = index
                return await self._page_info(state, tab)
        raise BotaiError(ErrorCode.NOT_FOUND, "Вкладка не найдена")

    async def close_tab(self, tab_id: str) -> PageInfo:
        state = self._current_state()
        for index, tab in enumerate(state.tabs):
            if tab.tab_id != tab_id:
                continue
            state.tabs.pop(index)
            state.current = max(0, min(state.current, len(state.tabs) - 1))
            with contextlib.suppress(PlaywrightError):
                await tab.page.close()
            if not state.tabs:
                return PageInfo(url="", title="", tab_id="", context_id=state.context_id)
            return await self._page_info(state, state.tab())
        raise BotaiError(ErrorCode.NOT_FOUND, "Вкладка не найдена")

    # -- network log and HAR -----------------------------------------------------------------

    def _on_request_handler(self, state: _ContextState) -> Callable[[Request], None]:
        def handle(request: Request) -> None:
            entry = NetworkEntry(
                method=request.method,
                url=request.url,
                resource_type=request.resource_type,
                request_headers=_bounded_headers(request.headers),
                started_at=time.time(),
            )
            if self._config.capture_network_bodies:
                post_data = request.post_data
                if isinstance(post_data, str):
                    entry.request_body = self._redact(post_data[:MAX_BODY_CHARS])
            state.network.append(entry)
            state.by_request[request] = entry
            if self._har_entries is not None:
                self._har_entries.append(entry)
                if len(self._har_entries) > MAX_HAR_ENTRIES:
                    del self._har_entries[0]
            self.stats["requests"] += 1

        return handle

    def _on_response_handler(self, state: _ContextState) -> Callable[[Response], None]:
        def handle(response: Response) -> None:
            entry = state.by_request.get(response.request)
            if entry is None:
                return
            entry.status = response.status
            entry.response_headers = _bounded_headers(response.headers)
            timing = response.request.timing
            response_end = _timing_value(timing, "responseEnd")
            request_start = _timing_value(timing, "requestStart")
            if response_end is not None and request_start is not None:
                entry.duration_ms = max(0.0, response_end - request_start)
            if self._config.capture_network_bodies:
                task = asyncio.get_running_loop().create_task(self._capture_body(entry, response))
                state.tasks.add(task)
                task.add_done_callback(state.tasks.discard)

        return handle

    def _on_failed_handler(self, state: _ContextState) -> Callable[[Request], None]:
        def handle(request: Request) -> None:
            entry = state.by_request.get(request)
            if entry is not None:
                entry.failed = True

        return handle

    async def _capture_body(self, entry: NetworkEntry, response: Response) -> None:
        content_type = response.headers.get("content-type", "").lower()
        if not _readable_content(content_type):
            return
        with contextlib.suppress(PlaywrightError, UnicodeError):
            text = await response.text()
            if text:
                entry.response_body = self._redact(text[:MAX_BODY_CHARS])

    def network_log(self) -> list[NetworkEntry]:
        """Requests of the active context, oldest first (bounded; bodies only if configured)."""
        state = self._current_state()
        return list(state.network)

    def clear_network_log(self) -> None:
        state = self._current_state()
        state.network.clear()
        state.by_request.clear()

    def har_start(self) -> None:
        if self._har_entries is not None:
            raise BotaiError(ErrorCode.CONFLICT, "HAR уже пишется: сначала остановите")
        self._har_entries = []
        log.info("browser control HAR started")

    def har_stop(self) -> dict[str, Any]:
        if self._har_entries is None:
            raise BotaiError(ErrorCode.CONFLICT, "HAR не записывался")
        entries, self._har_entries = self._har_entries, None
        log.info("browser control HAR stopped: %d entries", len(entries))
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "BotAI", "version": "v4"},
                "entries": [entry.to_har_entry() for entry in entries],
            }
        }

    def dialogs(self) -> list[str]:
        return list(self._current_state().dialogs)

    # -- navigation and actions --------------------------------------------------------------

    async def navigate(
        self,
        url: str,
        *,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
        timeout_s: float | None = None,
    ) -> PageInfo:
        """Go to any URL (redirects, IP literals and WebSocket pages are allowed)."""
        page = self._page()
        self._check_url(url)
        self.stats["actions"] += 1
        timeout_ms = self._timeout_ms(timeout_s, navigation=True)
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Открыть страницу", exc, url=url) from None
        return await self.page_info()

    @staticmethod
    def _check_url(url: str) -> None:
        parts = urlsplit(url)
        if not parts.scheme or parts.scheme.lower() not in ("http", "https"):
            raise BotaiError(
                ErrorCode.VALIDATION, "Адрес: нужен http(s) URL (без пробелов и пустых схем)"
            )
        if not parts.hostname:
            raise BotaiError(ErrorCode.VALIDATION, "Адрес: не удалось выделить хост")

    async def click(
        self,
        target: Target | Mapping[str, Any],
        *,
        button: Literal["left", "right", "middle"] = "left",
        click_count: int = 1,
        modifiers: Sequence[str] = (),
        timeout_s: float | None = None,
    ) -> PageInfo:
        aim = as_target(target)
        if button not in _BUTTONS:
            raise BotaiError(ErrorCode.VALIDATION, "Кнопка мыши: left, right или middle")
        if click_count < 1 or click_count > 5:
            raise BotaiError(ErrorCode.VALIDATION, "Число нажатий: от 1 до 5")
        _check_modifiers(modifiers)
        timeout_ms = self._timeout_ms(timeout_s)
        point = aim.point
        self.stats["actions"] += 1
        try:
            if point is not None:
                await self._with_modifiers(
                    self._page(),
                    modifiers,
                    lambda page: page.mouse.click(
                        point[0], point[1], button=button, click_count=click_count
                    ),
                )
            else:
                await self._locator(self._page(), aim).click(
                    button=button,
                    click_count=click_count,
                    modifiers=cast(Any, list(modifiers)),
                    timeout=timeout_ms,
                )
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Нажать", exc, target=aim.describe()) from None
        return await self.page_info()

    async def type(
        self,
        target: Target | Mapping[str, Any],
        text: str,
        *,
        clear: bool = True,
        timeout_s: float | None = None,
    ) -> PageInfo:
        """Type into a field. Values are never logged; only the target is named on error."""
        aim = as_target(target)
        if aim.point is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Печатать можно только в элемент, не в точку")
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            locator = self._locator(self._page(), aim)
            if clear:
                await locator.fill(text, timeout=timeout_ms)
            else:
                await locator.press_sequentially(text, timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Ввести текст", exc, target=aim.describe()) from None
        return await self.page_info()

    async def press(
        self,
        target: Target | Mapping[str, Any] | None,
        key: str,
        *,
        modifiers: Sequence[str] = (),
        timeout_s: float | None = None,
    ) -> PageInfo:
        _check_modifiers(modifiers)
        if not key:
            raise BotaiError(ErrorCode.VALIDATION, "Клавиша не указана")
        combination = "+".join([*modifiers, key])
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            page = self._page()
            if target is None:
                await page.keyboard.press(combination)
            else:
                await self._locator(page, as_target(target)).press(combination, timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Нажать клавишу", exc) from None
        return await self.page_info()

    async def select(
        self,
        target: Target | Mapping[str, Any],
        *,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
        timeout_s: float | None = None,
    ) -> PageInfo:
        aim = as_target(target)
        if aim.point is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Выбор в списке: нужен элемент, не точка")
        if value is None and label is None and index is None:
            raise BotaiError(ErrorCode.VALIDATION, "Выбор в списке: укажите value, label или index")
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            await self._locator(self._page(), aim).select_option(
                value=value,
                label=label,
                index=index,
                timeout=timeout_ms,
            )
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Выбрать значение", exc, target=aim.describe()) from None
        return await self.page_info()

    async def wait(
        self,
        kind: str,
        value: str | float | None = None,
        *,
        timeout_s: float | None = None,
    ) -> PageInfo:
        """Wait for a selector, visible text, network idle, a navigation or a plain timeout."""
        if kind not in _WAIT_KINDS:
            raise BotaiError(
                ErrorCode.VALIDATION, "Ожидание: selector, text, network_idle, timeout, navigation"
            )
        timeout_ms = self._timeout_ms(timeout_s)
        page = self._page()
        self.stats["actions"] += 1
        try:
            if kind == "timeout":
                seconds = value if isinstance(value, int | float) else (timeout_s or 1.0)
                await asyncio.sleep(max(0.0, min(float(seconds), timeout_ms / 1000)))
            elif kind == "selector":
                if not isinstance(value, str) or not value:
                    raise BotaiError(ErrorCode.VALIDATION, "Ожидание selector: нужен селектор")
                await page.wait_for_selector(value, state="visible", timeout=timeout_ms)
            elif kind == "text":
                if not isinstance(value, str) or not value:
                    raise BotaiError(ErrorCode.VALIDATION, "Ожидание text: нужен текст")
                await page.get_by_text(value).first.wait_for(state="visible", timeout=timeout_ms)
            elif kind == "network_idle":
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            else:
                if isinstance(value, str) and value:
                    await page.wait_for_url(value, timeout=timeout_ms)
                else:
                    await page.wait_for_load_state("load", timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            hint = value if kind in ("selector", "text") and isinstance(value, str) else None
            raise self._error("Дождаться", exc, target=hint) from None
        return await self.page_info()

    # -- reading -----------------------------------------------------------------------------

    async def read_text(
        self, target: Target | Mapping[str, Any] | None = None, *, max_chars: int | None = None
    ) -> str:
        page = self._page()
        limit = self._limit(max_chars, MAX_TEXT_CHARS)
        try:
            if target is None:
                text = await page.inner_text("body")
            else:
                text = await self._locator(page, as_target(target)).inner_text()
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Прочитать текст", exc) from None
        return text[:limit]

    async def read_dom(self, *, max_chars: int | None = None) -> str:
        """Serialised live DOM (`outerHTML`), values included: working material, not a snapshot."""
        page = self._page()
        limit = self._limit(max_chars, MAX_DOM_CHARS)
        try:
            html = await page.evaluate("() => document.documentElement.outerHTML")
        except PlaywrightError as exc:
            raise self._error("Прочитать DOM", exc) from None
        return str(html)[:limit]

    async def read_links(self, *, max_links: int | None = None) -> list[dict[str, str]]:
        page = self._page()
        limit = self._limit(max_links, MAX_LINKS)
        script = """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
            text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 300),
            href: a.href,
        }))"""
        try:
            raw = await page.evaluate(script)
        except PlaywrightError as exc:
            raise self._error("Прочитать ссылки", exc) from None
        links: list[dict[str, str]] = []
        if isinstance(raw, list):
            for item in raw[:limit]:
                if isinstance(item, dict):
                    links.append(
                        {"text": str(item.get("text", ""))[:300], "href": str(item.get("href", ""))}
                    )
        return links

    async def read_tables(self, *, max_chars: int | None = None) -> list[list[list[str]]]:
        page = self._page()
        limit = self._limit(max_chars, MAX_READ_CHARS)
        script = f"""() => Array.from(document.querySelectorAll('table')).slice(0, {MAX_TABLES})
            .map((t) => Array.from(t.rows).slice(0, {MAX_TABLE_ROWS}).map((r) =>
                Array.from(r.cells).slice(0, {MAX_TABLE_COLUMNS}).map((c) =>
                    (c.innerText || '').trim().slice(0, 500))))"""
        try:
            raw = await page.evaluate(script)
        except PlaywrightError as exc:
            raise self._error("Прочитать таблицы", exc) from None
        tables: list[list[list[str]]] = []
        if isinstance(raw, list):
            for table in raw:
                if not isinstance(table, list):
                    continue
                rows: list[list[str]] = []
                for row in table:
                    if isinstance(row, list):
                        rows.append([str(cell) for cell in row])
                tables.append(rows)
        encoded = json.dumps(tables, ensure_ascii=False)
        if len(encoded) <= limit:
            return tables
        truncated: list[list[list[str]]] = []
        for table in tables:
            if len(json.dumps(truncated, ensure_ascii=False)) > limit:
                break
            truncated.append(table)
        return truncated

    async def read_aria(self, *, max_chars: int | None = None) -> str:
        page = self._page()
        limit = self._limit(max_chars, MAX_READ_CHARS)
        try:
            snapshot = await page.locator("body").aria_snapshot()
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Прочитать ARIA-снимок", exc) from None
        return snapshot[:limit]

    async def read(
        self,
        mode: str,
        target: Target | Mapping[str, Any] | None = None,
        *,
        max_chars: int | None = None,
    ) -> str:
        """`browser_read(mode=...)`: text, dom/html, links, tables or an ARIA snapshot as text."""
        if mode not in _READ_MODES:
            raise BotaiError(
                ErrorCode.VALIDATION, "Режим чтения: text, dom, html, links, tables или aria"
            )
        if mode == "text":
            return await self.read_text(target, max_chars=max_chars)
        if mode in ("dom", "html"):
            return await self.read_dom(max_chars=max_chars)
        if mode == "links":
            return json.dumps(
                await self.read_links(max_links=max_chars and max_chars // 200), ensure_ascii=False
            )
        if mode == "tables":
            return json.dumps(await self.read_tables(max_chars=max_chars), ensure_ascii=False)
        return await self.read_aria(max_chars=max_chars)

    # -- JavaScript and screenshots ----------------------------------------------------------

    async def eval_js(
        self,
        expression: str,
        *,
        arg: Any = None,
        await_promise: bool = True,
        timeout_s: float | None = None,
    ) -> Any:
        """Evaluate JS in the active page and return a JSON-safe value.

        `expression` may be a function (`"() => document.title"`) or an expression using `arg` as
        a function argument. With `await_promise=False` a returned promise is not awaited: the
        call reports `{"pending": true}` instead."""
        if not expression:
            raise BotaiError(ErrorCode.VALIDATION, "Пустое JS-выражение")
        if len(expression) > MAX_SCRIPTS_CHARS:
            raise BotaiError(ErrorCode.VALIDATION, "JS-выражение слишком длинное")
        page = self._page()
        self.stats["actions"] += 1
        timeout_s = timeout_s or 30.0
        try:
            async with asyncio.timeout(timeout_s):
                if await_promise:
                    result = await page.evaluate(expression, arg)
                else:
                    if arg is not None:
                        raise BotaiError(
                            ErrorCode.VALIDATION,
                            "await_promise=False не принимает arg: передайте данные внутри выражения",
                        )
                    wrapper = """(expr) => {
                        let result = (0, eval)(expr);
                        if (typeof result === 'function') result = result();
                        if (result && typeof result.then === 'function') {
                            result.catch(() => {});
                            return { pending: true, value: null };
                        }
                        return { pending: false, value: result === undefined ? null : result };
                    }"""
                    result = await page.evaluate(wrapper, expression)
        except TimeoutError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "JS: выражение не завершилось за отведённое время"
            ) from None
        except (PlaywrightError, BotaiError) as exc:
            if isinstance(exc, BotaiError):
                raise
            raise self._error("Выполнить JS", exc) from None
        return _shrink_js_result(result)

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        target: Target | Mapping[str, Any] | None = None,
        image_format: Literal["png", "jpeg"] = "png",
        quality: int | None = None,
        timeout_s: float | None = None,
    ) -> bytes:
        """PNG/JPEG bytes of the page or of one element (the caller stores the artifact)."""
        if image_format not in ("png", "jpeg"):
            raise BotaiError(ErrorCode.VALIDATION, "Формат снимка: png или jpeg")
        if quality is not None and (quality < 1 or quality > 100):
            raise BotaiError(ErrorCode.VALIDATION, "Качество JPEG: от 1 до 100")
        if image_format == "png" and quality is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Качество задаётся только для JPEG")
        page = self._page()
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            if target is None:
                return await page.screenshot(
                    full_page=full_page,
                    type=image_format,
                    quality=quality,
                    timeout=timeout_ms,
                )
            return await self._locator(page, as_target(target)).screenshot(
                type=image_format, quality=quality, timeout=timeout_ms
            )
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Сделать скриншот", exc) from None

    # -- cookies and storage ------------------------------------------------------------------

    async def cookies(self, urls: Sequence[str] | None = None) -> list[Cookie]:
        state = self._current_state()
        try:
            return await state.context.cookies(list(urls) if urls else None)
        except PlaywrightError as exc:
            raise self._error("Прочитать cookies", exc) from None

    async def set_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> int:
        validated = [_validated_cookie(cookie) for cookie in cookies]
        if not validated:
            raise BotaiError(ErrorCode.VALIDATION, "Cookies: пустой список")
        state = self._current_state()
        try:
            await state.context.add_cookies(cast(Any, validated))
        except PlaywrightError as exc:
            raise self._error("Установить cookies", exc) from None
        return len(validated)

    async def clear_cookies(self) -> None:
        state = self._current_state()
        try:
            await state.context.clear_cookies()
        except PlaywrightError as exc:
            raise self._error("Очистить cookies", exc) from None

    async def get_storage(
        self, kind: Literal["local", "session"] = "local", *, origin: str | None = None
    ) -> dict[str, str]:
        if kind not in ("local", "session"):
            raise BotaiError(ErrorCode.VALIDATION, "Хранилище: local или session")
        page = self._page()
        self._check_origin(origin, page.url)
        try:
            raw = await page.evaluate(
                f"() => Object.fromEntries(Object.entries(window.{_storage_object(kind)}))"
            )
        except PlaywrightError as exc:
            raise self._error("Прочитать хранилище", exc) from None
        return _string_map(raw)

    async def set_storage(
        self,
        data: Mapping[str, Any],
        kind: Literal["local", "session"] = "local",
        *,
        origin: str | None = None,
        clear: bool = False,
    ) -> int:
        if kind not in ("local", "session"):
            raise BotaiError(ErrorCode.VALIDATION, "Хранилище: local или session")
        entries = _string_map(data)
        page = self._page()
        self._check_origin(origin, page.url)
        try:
            if clear:
                await page.evaluate(f"() => window.{_storage_object(kind)}.clear()")
            if entries:
                script = f"""(items) => {{
                    for (const [key, value] of Object.entries(items)) {{
                        window.{_storage_object(kind)}.setItem(key, value);
                    }}
                }}"""
                await page.evaluate(script, entries)
        except PlaywrightError as exc:
            raise self._error("Записать хранилище", exc) from None
        return len(entries)

    @staticmethod
    def _check_origin(origin: str | None, page_url: str) -> None:
        if origin is None:
            return
        try:
            requested = urlsplit(origin)
            current = urlsplit(page_url)
        except ValueError:
            raise BotaiError(ErrorCode.VALIDATION, "Origin: недопустимый адрес") from None
        if requested.scheme not in ("http", "https") or not requested.hostname:
            raise BotaiError(ErrorCode.VALIDATION, "Origin: нужен http(s)://host")
        if (requested.scheme, requested.netloc) != (current.scheme, current.netloc):
            raise BotaiError(
                ErrorCode.VALIDATION,
                "Хранилище читается на текущей странице: сначала откройте нужный origin",
            )

    # -- storage state -----------------------------------------------------------------------

    async def export_storage_state(self, *, indexed_db: bool = False) -> StorageState:
        """Cookies and localStorage of the active context (session material, not for logs)."""
        state = self._current_state()
        try:
            return await state.context.storage_state(indexed_db=indexed_db or None)
        except PlaywrightError as exc:
            raise self._error("Экспортировать состояние", exc) from None

    async def import_storage_state(self, storage_state: Mapping[str, Any]) -> StorageImportResult:
        """Add cookies and apply localStorage of the state to the active context.

        Cookies are applied immediately. localStorage lives per origin: each origin of the state
        is opened once, filled and closed (a navigation, so the site sees the visit)."""
        state_doc = _validated_storage_state(storage_state)
        state = self._current_state()
        cookies = list(state_doc.get("cookies") or [])
        if cookies:
            try:
                await state.context.add_cookies(cast(Any, cookies))
            except PlaywrightError as exc:
                raise self._error("Импортировать cookies", exc) from None
        applied = 0
        failed = 0
        for origin in state_doc.get("origins") or []:
            items = {
                str(entry.get("name", "")): str(entry.get("value", ""))
                for entry in origin.get("localStorage") or []
            }
            if not items:
                continue
            page: Page | None = None
            try:
                page = await state.context.new_page()
                page.set_default_timeout(self._config.default_timeout_ms)
                await page.goto(
                    origin["origin"],
                    wait_until="domcontentloaded",
                    timeout=self._config.default_timeout_ms,
                )
                script = """(items) => {
                    for (const [key, value] of Object.entries(items)) localStorage.setItem(key, value);
                }"""
                await page.evaluate(script, items)
                applied += 1
            except (PlaywrightTimeout, PlaywrightError):
                failed += 1
            finally:
                if page is not None:
                    with contextlib.suppress(PlaywrightError):
                        await page.close()
        return StorageImportResult(
            cookies_added=len(cookies), origins_applied=applied, origins_failed=failed
        )

    # -- downloads and uploads ----------------------------------------------------------------

    async def download(
        self,
        target: Target | Mapping[str, Any],
        *,
        save_dir: Path | str | None = None,
        filename: str | None = None,
        timeout_s: float | None = None,
    ) -> DownloadInfo:
        """Click a download link and put the file on disk (0700 directory, 0600 file)."""
        aim = as_target(target)
        if aim.point is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Скачивание: нужен элемент, не точка")
        page = self._page()
        directory = _secure_dir(
            Path(save_dir)
            if save_dir is not None
            else self._config.downloads_dir
            or Path(tempfile.mkdtemp(prefix="botai-browser-downloads-"))
        )
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            async with page.expect_download(timeout=timeout_ms) as download_info:
                await self._locator(page, aim).click(timeout=timeout_ms)
            download = await download_info.value
            info = await self._save_download(download, directory, filename)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Скачать файл", exc, target=aim.describe()) from None
        self.stats["downloads"] += 1
        log.info("browser control download saved: %d bytes", info.size_bytes)
        return info

    async def _save_download(
        self, download: Download, directory: Path, filename: str | None
    ) -> DownloadInfo:
        name = _safe_filename(filename or download.suggested_filename)
        destination = directory / name
        await download.save_as(destination)
        with contextlib.suppress(OSError):
            os.chmod(destination, 0o600)
        digest = hashlib.sha256()
        size = 0
        with destination.open("rb") as handle:
            for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
        return DownloadInfo(
            suggested_filename=download.suggested_filename,
            url=download.url,
            path=str(destination),
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    async def upload(
        self,
        target: Target | Mapping[str, Any],
        files: Sequence[Path | str | tuple[str, bytes]],
        *,
        timeout_s: float | None = None,
    ) -> int:
        """Set files of a file input; `(name, bytes)` payloads avoid touching the disk."""
        aim = as_target(target)
        if aim.point is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Загрузка: нужен input[type=file]")
        payloads: list[str | FilePayload] = []
        for item in files:
            if isinstance(item, tuple):
                name, data = item
                if not name:
                    raise BotaiError(ErrorCode.VALIDATION, "Файл: нужны name и bytes")
                payloads.append(
                    cast(
                        FilePayload,
                        {
                            "name": _safe_filename(name),
                            "mimeType": _guess_mime(name),
                            "buffer": data,
                        },
                    )
                )
            else:
                path = self._upload_path(Path(item))
                if not await asyncio.to_thread(path.is_file):
                    raise BotaiError(ErrorCode.VALIDATION, "Файл не найден на диске")
                payloads.append(str(path))
        if not payloads:
            raise BotaiError(ErrorCode.VALIDATION, "Нечего загружать: пустой список файлов")
        timeout_ms = self._timeout_ms(timeout_s)
        self.stats["actions"] += 1
        try:
            await self._locator(self._page(), aim).set_input_files(
                cast(Any, payloads), timeout=timeout_ms
            )
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise self._error("Загрузить файл", exc, target=aim.describe()) from None
        self.stats["uploads"] += 1
        return len(payloads)

    # -- internals ----------------------------------------------------------------------------

    def _upload_path(self, item: Path) -> Path:
        """Resolve an upload source inside the configured workspace; never an arbitrary path."""
        root = self._config.upload_root
        if root is None:
            raise BotaiError(
                ErrorCode.VALIDATION,
                "Загрузка с диска: не задан upload_root; передайте файл как (name, bytes)",
            )
        root = root.resolve()
        resolved = (root / item).resolve() if not item.is_absolute() else item.resolve()
        if resolved != root and root not in resolved.parents:
            raise BotaiError(
                ErrorCode.FORBIDDEN,
                "Загрузка: файл вне разрешённого каталога run (upload_root)",
            )
        return resolved

    def _locator(self, page: Page, target: Target) -> Locator:
        if target.selector:
            return page.locator(target.selector)
        if target.text:
            return page.get_by_text(target.text, exact=target.exact)
        if target.role:
            return page.get_by_role(cast(Any, target.role), name=target.name, exact=target.exact)
        if target.test_id:
            return page.get_by_test_id(target.test_id)
        if target.label:
            return page.get_by_label(target.label, exact=target.exact)
        raise BotaiError(ErrorCode.VALIDATION, "Цель: нужен элемент, не координаты")

    async def _with_modifiers(
        self, page: Page, modifiers: Sequence[str], action: Callable[[Page], Awaitable[None]]
    ) -> None:
        pressed: list[str] = []
        try:
            for modifier in modifiers:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await action(page)
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(PlaywrightError):
                    await page.keyboard.up(modifier)

    def _timeout_ms(self, timeout_s: float | None, *, navigation: bool = False) -> int:
        if timeout_s is None:
            return (
                self._config.navigation_timeout_ms
                if navigation
                else self._config.default_timeout_ms
            )
        if timeout_s <= 0 or timeout_s > 600:
            raise BotaiError(ErrorCode.VALIDATION, "Таймаут: от 0 до 600 секунд")
        return int(timeout_s * 1000)

    @staticmethod
    def _limit(value: int | None, default: int) -> int:
        if value is None:
            return default
        if value <= 0:
            raise BotaiError(ErrorCode.VALIDATION, "Лимит символов должен быть положительным")
        return min(value, default)

    def _redact(self, text: str) -> str:
        """Mask primary secrets in a captured body: hook first, then the default field mask."""
        cleaned = text
        hook = self._config.redact_body
        if hook is not None:
            try:
                cleaned = hook(cleaned)
            except Exception:  # a broken hook must not lose the whole capture
                log.warning("browser control body redactor failed; default masking only")
        return mask_primary_secrets(cleaned)

    def _error(
        self,
        action: str,
        exc: BaseException,
        *,
        target: str | None = None,
        url: str | None = None,
    ) -> BotaiError:
        """Turn a Playwright failure into a human message without echoing page content/values."""
        if isinstance(exc, PlaywrightTimeout):
            where = target or _safe_url(url) or "страницы"
            if target is not None:
                return BotaiError(ErrorCode.NOT_FOUND, f"{action}: не дождались элемента ({where})")
            return BotaiError(
                ErrorCode.BROWSER_TIMEOUT, f"{action}: не дождались ответа страницы ({where})"
            )
        message = str(exc).lower()
        if "closed" in message or "target page" in message or "crashed" in message:
            return BotaiError(ErrorCode.CONFLICT, "Браузер или вкладка закрылись")
        host = _safe_url(url)
        suffix = f" ({host})" if host else ""
        return BotaiError(ErrorCode.SOURCE_UNAVAILABLE, f"{action}: страница не ответила{suffix}")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


# Body fields that carry primary secrets (02-architecture §7): masked before a body is stored
# in the network log or a HAR. Session material (cookies, bearers) is working material for the
# agent (D-008) and is not masked here; the runtime's redactor hook can mask more.
_SECRET_FIELD = re.compile(
    r"pass(?:word|wd)?|\bpwd\b|secret|otp|mfa|passcode|cvv|security[-_]?answer|"
    r"private[-_]?key|api[-_]?key|llm[-_]?key|credential",
    re.IGNORECASE,
)
_MASK = "***"


def mask_primary_secrets(text: str) -> str:
    """Mask password/MFA/key-like values in a JSON or form-encoded body (other text untouched)."""

    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            decoded: Any = json.loads(stripped)
        except ValueError:
            return _mask_form_body(text)
        return json.dumps(_mask_json(decoded), ensure_ascii=False, separators=(",", ":"))
    return _mask_form_body(text)


def _mask_json(value: Any) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_FIELD.search(key):
                masked[key] = _MASK
            else:
                masked[key] = _mask_json(item)
        return masked
    if isinstance(value, list):
        return [_mask_json(item) for item in value]
    return value


def _mask_form_body(text: str) -> str:
    if "=" not in text:
        return text
    parts: list[str] = []
    for part in text.split("&"):
        name, separator, _ = part.partition("=")
        if separator and _SECRET_FIELD.search(unquote(name)):
            parts.append(f"{name}={_MASK}")
        else:
            parts.append(part)
    return "&".join(parts)


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def _safe_filename(name: str) -> str:
    cleaned = name.strip().replace("..", "_")
    for bad in _UNSAFE_FILENAME:
        cleaned = cleaned.replace(bad, "_")
    cleaned = "".join(char for char in cleaned if char.isprintable()).strip(". ")
    if not cleaned:
        cleaned = "download"
    return cleaned[:160]


def _guess_mime(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".txt"):
        return "text/plain"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".html"):
        return "text/html"
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return f"image/{lowered.rsplit('.', 1)[-1].replace('jpg', 'jpeg')}"
    return "application/octet-stream"


def _storage_object(kind: str) -> str:
    return "localStorage" if kind == "local" else "sessionStorage"


def _check_modifiers(modifiers: Sequence[str]) -> None:
    unknown = [modifier for modifier in modifiers if modifier not in _MODIFIERS]
    if unknown:
        raise BotaiError(ErrorCode.VALIDATION, "Модификаторы: Control, Shift, Alt или Meta")


def _readable_content(content_type: str) -> bool:
    return any(
        marker in content_type
        for marker in ("json", "text/", "xml", "javascript", "x-www-form-urlencoded")
    )


def _bounded_headers(headers: Mapping[str, str]) -> dict[str, str]:
    bounded: dict[str, str] = {}
    for name, value in list(headers.items())[:MAX_HEADERS]:
        bounded[str(name)] = str(value)[:MAX_HEADER_VALUE_CHARS]
    return bounded


def _timing_value(timing: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(timing, Mapping):
        return None
    value = timing.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise BotaiError(ErrorCode.VALIDATION, "Ожидался объект ключ-значение")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise BotaiError(ErrorCode.VALIDATION, "Ключи хранилища: непустые строки")
        result[key[:256]] = str(item)[:32_768]
    return result


_MAX_JSON_CHARS = 100_000


def _shrink_js_result(value: Any) -> Any:
    """Bounded JSON-safe JS result: strings are truncated, big structures are stringified."""
    if isinstance(value, str):
        return value[:MAX_READ_CHARS]
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)[:MAX_READ_CHARS]
    if len(encoded) <= _MAX_JSON_CHARS:
        return value
    return encoded[:_MAX_JSON_CHARS]


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.hostname}"[:200]


def _validated_cookie(cookie: Mapping[str, Any]) -> dict[str, Any]:
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not name:
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: нужно имя")
    if not isinstance(value, str):
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: значение должно быть строкой")
    domain = cookie.get("domain")
    cookie_url = cookie.get("url")
    if not (isinstance(domain, str) and domain) and not (
        isinstance(cookie_url, str) and cookie_url
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: нужен domain или url")
    result: dict[str, Any] = {"name": name[:256], "value": value[:16_384]}
    for key in ("domain", "path", "url"):
        raw = cookie.get(key)
        if isinstance(raw, str):
            result[key] = raw[:2_048]
    expires = cookie.get("expires")
    if isinstance(expires, int | float) and not isinstance(expires, bool):
        result["expires"] = float(expires)
    for key in ("httpOnly", "secure"):
        raw = cookie.get(key)
        if isinstance(raw, bool):
            result[key] = raw
    same_site = cookie.get("sameSite")
    if same_site in ("Lax", "None", "Strict"):
        result["sameSite"] = same_site
    return result


MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024
MAX_STATE_COOKIES = 500
MAX_STATE_ORIGINS = 30
MAX_STATE_STORAGE_ENTRIES = 500


def _validated_storage_state(state: Mapping[str, Any]) -> StorageState:
    """Validate a Playwright storage-state shape without trusting its size or origins."""
    cookies_raw = state.get("cookies", [])
    origins_raw = state.get("origins", [])
    if not isinstance(cookies_raw, list) or not isinstance(origins_raw, list):
        raise BotaiError(
            ErrorCode.VALIDATION, "Storage state: cookies/origins должны быть списками"
        )
    if len(cookies_raw) > MAX_STATE_COOKIES:
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком много cookies")
    if len(origins_raw) > MAX_STATE_ORIGINS:
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком много origin'ов")
    cookies = [_validated_cookie(item) for item in cookies_raw if isinstance(item, Mapping)]
    origins: list[dict[str, Any]] = []
    total = 0
    for raw in origins_raw:
        if not isinstance(raw, Mapping):
            continue
        origin = raw.get("origin")
        localStorage = raw.get("localStorage", [])
        if not isinstance(origin, str) or not isinstance(localStorage, list):
            raise BotaiError(ErrorCode.VALIDATION, "Storage state: origin и localStorage")
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise BotaiError(ErrorCode.VALIDATION, "Storage state: недопустимый origin")
        entries: list[dict[str, str]] = []
        for item in localStorage[:MAX_STATE_STORAGE_ENTRIES]:
            if not isinstance(item, Mapping):
                continue
            name, value = item.get("name"), item.get("value")
            if isinstance(name, str) and name and isinstance(value, str):
                entry = {"name": name[:256], "value": value[:32_768]}
                total += len(entry["name"]) + len(entry["value"])
                entries.append(entry)
        origins.append({"origin": f"{parts.scheme}://{parts.netloc}", "localStorage": entries})
    if total > MAX_STORAGE_STATE_BYTES:
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком большой")
    normalized = cast(StorageState, {"cookies": cookies, "origins": origins})
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_STORAGE_STATE_BYTES:
        raise BotaiError(
            ErrorCode.VALIDATION,
            f"Storage state: слишком большой (лимит {MAX_STORAGE_STATE_BYTES} байт)",
        )
    return normalized


__all__ = [
    "BrowserControl",
    "ContextInfo",
    "ControlConfig",
    "DownloadInfo",
    "NetworkEntry",
    "PageInfo",
    "StorageImportResult",
    "TabInfo",
    "Target",
    "as_target",
    "mask_primary_secrets",
]
