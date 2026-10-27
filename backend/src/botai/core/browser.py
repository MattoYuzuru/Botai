"""Browser gateway protocol (implementation: botai.browser, W2) and a fake for tests.

Everything that crosses this boundary is data about pages, never instructions to follow.
Observed network bodies are kept in memory for learning only and are never persisted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field, SecretStr


class FormField(BaseModel):
    selector: str
    name: str | None = None
    input_type: str = "text"  # text | email | password | tel | number | checkbox | hidden ...
    label: str | None = None
    autocomplete: str | None = None


class FormInfo(BaseModel):
    selector: str
    action: str | None = None
    method: str = "get"
    fields: list[FormField] = Field(default_factory=list)
    submit_selector: str | None = None


class PageSnapshot(BaseModel):
    url: str
    title: str = ""
    html: str = ""  # truncated to a bounded size by the implementation
    text: str = ""  # visible text, truncated
    forms: list[FormInfo] = Field(default_factory=list)
    captcha_suspected: bool = False


class ObservedRequest(BaseModel):
    method: str
    url: str
    status: int | None = None
    resource_type: str = ""  # xhr | fetch | document | ...
    request_content_type: str | None = None
    response_content_type: str | None = None
    response_sample: str | None = None  # truncated body, in memory only, for learning


class SessionCookie(BaseModel):
    name: str
    value: SecretStr
    domain: str
    path: str = "/"
    expires: float | None = None
    http_only: bool = False
    secure: bool = True


class SessionState(BaseModel):
    """What gets sealed into the Vault as kind=session (security.md §3)."""

    origin: str
    cookies: list[SessionCookie] = Field(default_factory=list)
    bearer: SecretStr | None = None
    captured_at: datetime
    expires_hint: datetime | None = None


class BrowserHandle(Protocol):
    async def goto(self, url: str) -> PageSnapshot: ...
    async def snapshot(self) -> PageSnapshot: ...
    async def network_log(self) -> list[ObservedRequest]: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def click(self, selector: str) -> PageSnapshot: ...
    async def export_session(self) -> SessionState: ...
    async def close(self) -> None: ...


class BrowserGateway(Protocol):
    async def open(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
    ) -> BrowserHandle: ...


@dataclass
class FakePage:
    html: str
    title: str = ""
    requests: list[ObservedRequest] = field(default_factory=list)
    forms: list[FormInfo] = field(default_factory=list)
    on_click: Mapping[str, str] = field(default_factory=dict)  # selector -> next url


class FakeBrowserHandle:
    def __init__(self, pages: Mapping[str, FakePage], now: datetime, origin: str) -> None:
        self._pages = pages
        self._now = now
        self._origin = origin
        self._url: str | None = None
        self._log: list[ObservedRequest] = []
        self.filled: dict[str, str] = {}
        self.closed = False

    def _snap(self) -> PageSnapshot:
        assert self._url is not None
        page = self._pages[self._url]
        return PageSnapshot(url=self._url, title=page.title, html=page.html, forms=page.forms)

    async def goto(self, url: str) -> PageSnapshot:
        if url not in self._pages:
            raise KeyError(f"FakeBrowser: no page {url}")
        self._url = url
        self._log.extend(self._pages[url].requests)
        return self._snap()

    async def snapshot(self) -> PageSnapshot:
        return self._snap()

    async def network_log(self) -> list[ObservedRequest]:
        return list(self._log)

    async def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    async def click(self, selector: str) -> PageSnapshot:
        assert self._url is not None
        nxt = self._pages[self._url].on_click.get(selector)
        if nxt:
            return await self.goto(nxt)
        return self._snap()

    async def export_session(self) -> SessionState:
        return SessionState(origin=self._origin, captured_at=self._now)

    async def close(self) -> None:
        self.closed = True


class FakeBrowserGateway:
    def __init__(self, pages: Mapping[str, FakePage], now: datetime, origin: str) -> None:
        self._pages = pages
        self._now = now
        self._origin = origin
        self.handles: list[FakeBrowserHandle] = []

    async def open(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
    ) -> FakeBrowserHandle:
        handle = FakeBrowserHandle(self._pages, self._now, self._origin)
        self.handles.append(handle)
        return handle


class SessionProvider(Protocol):
    """Implementation: botai.browser.login (W2).

    Returns a valid session for the connection: from the Vault if still good; otherwise logs in
    again with the stored password; otherwise asks the user through `ctx` (mirrored form fields,
    push-wait, fallback screen). Raises BotaiError(login_failed | source_bot_protection) on failure.
    """

    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> SessionState: ...
