"""The browser half of a playbook: one session, a step plan, and the network it observed.

A browser playbook is a first-class way to read a source (not a fallback): a plan navigates,
logs in with the session the source already has, clicks, types, waits, reads or evaluates JS, and
the JSON answers the page's own scripts receive are recorded as data. The runner then parses those
observed answers with the same declarative reads as an HTTP playbook — so a client-side page whose
DOM contains no data can still feed the domain, and the discovered API endpoints are kept for the
next run.

This module owns only the seam: a small `BrowserSession` protocol (real implementation over
`botai.browser.control.BrowserControl`, test doubles allowed), target parsing for the step
vocabulary of `contracts/playbook.schema.json`, and extraction of observed JSON responses. It
never decides where the browser runs; the wiring injects a `BrowserFactory`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.async_api import StorageState

from botai.browser.control import BrowserControl, ControlConfig, as_target
from botai.core.errors import BotaiError, ErrorCode

# Actions the runner executes. The contract also names `upload`: it has no wired source of files
# yet, so the runner rejects it structurally instead of silently skipping it.
BROWSER_ACTIONS = (
    "goto",
    "click",
    "type",
    "press",
    "select",
    "wait",
    "eval",
    "read",
    "download",
)
READ_MODES = ("text", "dom", "links", "tables", "aria")
MAX_OBSERVED_JSON = 200


@dataclass(frozen=True, slots=True)
class ObservedResponse:
    """One answer the page received: status, type and (when captured) the body."""

    method: str
    url: str
    status: int
    content_type: str
    body: str

    def is_json(self) -> bool:
        """Whether this answer is a JSON document, by content type or by body shape."""
        if any(hint in self.content_type.lower() for hint in ("json", "javascript")):
            return True
        head = self.body.lstrip()[:1]
        return head in ("{", "[")

    def parse(self) -> Any:
        """The decoded JSON body; raises ValueError for a non-JSON body."""
        return json.loads(self.body)


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """The final page of a plan: its address and its live DOM."""

    url: str
    html: str


@dataclass(slots=True)
class BrowserOutcome:
    """What a plan produced: page snapshots, observed JSON responses and report-safe notes."""

    pages: list[PageSnapshot] = field(default_factory=list)
    responses: list[ObservedResponse] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def endpoints(self) -> list[dict[str, Any]]:
        """Secret-free endpoint records for `source_state.endpoints` (query strings dropped)."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for response in self.responses:
            url = endpoint_url(response.url)
            key = (response.method.upper(), url)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": f"observed:{len(out) + 1}",
                    "method": response.method.upper(),
                    "url": url,
                    "status": response.status,
                    "contentType": response.content_type,
                    "observed": True,
                }
            )
        return out


def endpoint_url(url: str) -> str:
    """An endpoint without its query: a session token in a query must not reach the state row."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else parts.path


def cookie_pairs(storage_state: Mapping[str, Any]) -> dict[str, str]:
    """Name -> value cookies of a Playwright storage state (for an HTTP request jar)."""
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return {}
    out: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if isinstance(name, str) and isinstance(value, str) and name:
            out[name] = value
    return out


class BrowserSession(Protocol):
    """The slice of a full-control browser a playbook uses; wiring supplies the real one."""

    def url(self) -> str: ...

    async def navigate(self, url: str) -> str: ...

    async def click(self, target: Mapping[str, Any]) -> None: ...

    async def type(self, target: Mapping[str, Any], text: str) -> None: ...

    async def press(self, target: Mapping[str, Any] | None, key: str) -> None: ...

    async def select(self, target: Mapping[str, Any], value: str) -> None: ...

    async def wait(self, kind: str, value: str | None, timeout_s: float | None) -> None: ...

    async def read(
        self, mode: str, target: Mapping[str, Any] | None, max_chars: int | None
    ) -> str: ...

    async def eval_js(self, expression: str) -> Any: ...

    async def import_storage_state(self, state: Mapping[str, Any]) -> None: ...

    async def download(self, target: Mapping[str, Any]) -> tuple[bytes, str, str | None]: ...

    def network_log(self) -> Sequence[Any]: ...

    async def page_html(self, max_chars: int | None = None) -> str: ...

    async def close(self) -> None: ...


BrowserFactory = Callable[[Mapping[str, Any] | None], AbstractAsyncContextManager[BrowserSession]]


def target_of(raw: str) -> dict[str, Any]:
    """Turn a plan target string into a `browser.control.Target` mapping.

    Prefixes follow the control vocabulary: `css=`, `text=`, `role=`, `testid=`, `label=`,
    `xpath=`; a bare string is a CSS/Playwright selector (the common case).
    """
    value = raw.strip()
    if not value:
        raise BotaiError(ErrorCode.VALIDATION, "Цель шага пуста")
    for prefix, field_name in (("testid=", "test_id"), ("label=", "label")):
        if value.casefold().startswith(prefix):
            rest = value[len(prefix) :].strip()
            if not rest:
                raise BotaiError(ErrorCode.VALIDATION, f"Цель шага пуста после {prefix}")
            return {field_name: rest}
    # `css=`, `text=`, `xpath=`, `role=[...]` are Playwright selector prefixes: pass them through.
    return {"selector": value}


class PlaywrightSession:
    """`BrowserSession` over the real `BrowserControl` (full-control Chromium)."""

    def __init__(self, control: BrowserControl) -> None:
        self._control = control

    def url(self) -> str:
        return self._control.url

    async def navigate(self, url: str) -> str:
        info = await self._control.navigate(url, wait_until="domcontentloaded")
        return info.url

    async def click(self, target: Mapping[str, Any]) -> None:
        await self._control.click(as_target(target))

    async def type(self, target: Mapping[str, Any], text: str) -> None:
        await self._control.type(as_target(target), text)

    async def press(self, target: Mapping[str, Any] | None, key: str) -> None:
        await self._control.press(as_target(target) if target is not None else None, key)

    async def select(self, target: Mapping[str, Any], value: str) -> None:
        await self._control.select(as_target(target), value=value)

    async def wait(self, kind: str, value: str | None, timeout_s: float | None) -> None:
        await self._control.wait(kind, value, timeout_s=timeout_s)

    async def read(self, mode: str, target: Mapping[str, Any] | None, max_chars: int | None) -> str:
        return await self._control.read(
            mode, as_target(target) if target is not None else None, max_chars=max_chars
        )

    async def eval_js(self, expression: str) -> Any:
        return await self._control.eval_js(expression)

    async def import_storage_state(self, state: Mapping[str, Any]) -> None:
        await self._control.import_storage_state(StorageState(**state))

    async def download(self, target: Mapping[str, Any]) -> tuple[bytes, str, str | None]:
        info = await self._control.download(as_target(target))
        data = await asyncio.to_thread(Path(info.path).read_bytes)
        return data, info.suggested_filename, info.url

    def network_log(self) -> Sequence[Any]:
        return self._control.network_log()

    async def page_html(self, max_chars: int | None = None) -> str:
        return await self._control.read_dom(max_chars=max_chars)

    async def close(self) -> None:
        await self._control.close()


@asynccontextmanager
async def playwright_browser(
    storage_state: Mapping[str, Any] | None,
    *,
    headless: bool = True,
    ignore_https_errors: bool = False,
) -> AsyncGenerator[BrowserSession]:
    """Launch a fresh full-control Chromium with the source's session state applied."""
    control = await BrowserControl.launch(
        config=ControlConfig(
            headless=headless,
            ignore_https_errors=ignore_https_errors,
            capture_network_bodies=True,
        ),
        storage_state=StorageState(**storage_state) if storage_state else None,
    )
    session = PlaywrightSession(control)
    try:
        yield session
    finally:
        await control.close()


def playwright_factory(
    *, headless: bool = True, ignore_https_errors: bool = False
) -> BrowserFactory:
    """A `BrowserFactory` launching a real Chromium per browser playbook run."""

    def factory(
        storage_state: Mapping[str, Any] | None,
    ) -> AbstractAsyncContextManager[BrowserSession]:
        return playwright_browser(
            storage_state, headless=headless, ignore_https_errors=ignore_https_errors
        )

    return factory


def observed_responses(entries: Sequence[Any]) -> list[ObservedResponse]:
    """JSON responses from a browser network log, newest last, duplicates collapsed by URL.

    `NetworkEntry` objects are accepted structurally: a test double only needs the attributes the
    control's log provides. Bodies are present only when the session captured them.
    """
    out: list[ObservedResponse] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        method = str(getattr(entry, "method", "") or "").upper()
        url = str(getattr(entry, "url", "") or "")
        if not method or not url.startswith(("http://", "https://")):
            continue
        if getattr(entry, "failed", False):
            continue
        status = getattr(entry, "status", None)
        if not isinstance(status, int) or status < 200 or status >= 300:
            continue
        headers = getattr(entry, "response_headers", {}) or {}
        content_type = ""
        for name, value in headers.items():
            if str(name).casefold() == "content-type":
                content_type = str(value)
                break
        body = getattr(entry, "response_body", None)
        if not isinstance(body, str) or not body:
            continue
        response = ObservedResponse(method, url, status, content_type, body)
        if not response.is_json():
            continue
        key = (method, endpoint_url(url))
        if key in seen:
            continue
        seen.add(key)
        out.append(response)
        if len(out) >= MAX_OBSERVED_JSON:
            break
    return out
