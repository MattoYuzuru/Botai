"""Browser gateway protocol (implementation: botai.browser, W2) and a fake for tests.

Everything that crosses this boundary is data about pages, never instructions to follow.
Observed network bodies are kept in memory for learning only and are never persisted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from botai.core.errors import BotaiError, ErrorCode


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
    evidence_id: str | None = None
    # The page whose script issued the request (executor-owned, never shown to a model): the
    # compiler derives the address a student opens to see this data from it.
    initiator_url: str | None = None
    failed: bool = False  # ended without a response: refused by a guard, blocked, lost
    has_cookie: bool = False
    has_authorization: bool = False
    authorization_scheme: Literal["none", "bearer", "other"] = "none"
    auth_proven: bool = False  # complete request headers inspected by the executor


class SessionCookie(BaseModel):
    name: str
    value: SecretStr
    domain: str
    path: str = "/"
    expires: float | None = None
    http_only: bool = False
    secure: bool = True


class SessionOriginStorage(BaseModel):
    """Origin-scoped browser state, sealed with the session and never shown to a model."""

    origin: str
    local_storage: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    session_storage: dict[str, SecretStr] = Field(default_factory=dict, repr=False)


def auth_origin(url: str) -> str | None:
    """Exact HTTPS credential audience, including a nondefault port; never a host suffix."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or not host or parts.username or parts.password:
            return None
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            return None
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if ":" in host:  # preserve brackets for a parsed IPv6 address; egress still denies literals
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


class SessionBearer(BaseModel):
    """A bearer the browser actually sent, bound to its exact observed HTTPS origin."""

    origin: str
    token: SecretStr = Field(repr=False, min_length=1, max_length=32_768)
    captured_at: datetime
    expires_hint: datetime | None = None

    @field_validator("origin")
    @classmethod
    def plain_origin(cls, value: str) -> str:
        origin = auth_origin(value)
        parts = urlsplit(value)
        if origin is None or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("a bearer audience must be a plain HTTPS origin")
        return origin


class SessionState(BaseModel):
    """What gets sealed into the Vault as kind=session (security.md §3)."""

    origin: str
    cookies: list[SessionCookie] = Field(default_factory=list)
    bearer: SecretStr | None = None
    bearer_scopes: list[SessionBearer] = Field(default_factory=list, repr=False, max_length=12)
    captured_at: datetime
    expires_hint: datetime | None = None
    browser_storage: list[SessionOriginStorage] = Field(default_factory=list, repr=False)
    entry_url: SecretStr | None = Field(default=None, repr=False, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_bearer_audiences(self) -> SessionState:
        if len({scope.origin for scope in self.bearer_scopes}) != len(self.bearer_scopes):
            raise ValueError("a session has conflicting bearer audiences")
        if self.entry_url is not None:
            entry = self.entry_url.get_secret_value()
            if (
                not entry
                or len(entry) > 2000
                or entry != entry.strip()
                or "\\" in entry
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in entry)
                or urlsplit(entry).username is not None
                or auth_origin(entry) is None
                or auth_origin(entry) != auth_origin(self.origin)
            ):
                raise ValueError(
                    "the private acquisition entry must be a bounded same-origin HTTPS URL"
                )
        return self


class BrowserBusy(BotaiError):
    """The user's one browser is taken (a login, a research, a read of another page): a
    background reader does not push the running action out, it asks to come back later."""

    def __init__(
        self, detail: str = "Браузер занят другим действием; чтение продолжится позже"
    ) -> None:
        super().__init__(ErrorCode.SOURCE_UNAVAILABLE, detail)


class BrowserHandle(Protocol):
    async def goto(self, url: str) -> PageSnapshot: ...
    async def snapshot(self) -> PageSnapshot: ...
    async def network_log(self) -> list[ObservedRequest]: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def click(self, selector: str) -> PageSnapshot: ...
    async def export_session(self) -> SessionState: ...
    async def close(self) -> None: ...


class BrowserGateway(Protocol):
    """`background` never pushes out the user's running browser (`BrowserBusy` instead).
    `agent_read` is the live read of a page for the chat agent: it gives way to every other
    background action of the user (the newer one closes it) and refuses navigations and requests
    to addresses that name an action; it implies `read_only` and `background`."""

    async def open(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
        read_only: bool = False,
        background: bool = False,
        agent_read: bool = False,
    ) -> BrowserHandle: ...


@dataclass
class FakePage:
    html: str
    title: str = ""
    requests: list[ObservedRequest] = field(default_factory=list)
    forms: list[FormInfo] = field(default_factory=list)
    on_click: Mapping[str, str] = field(default_factory=dict)  # selector -> next url


class FakeBrowserHandle:
    def __init__(
        self,
        pages: Mapping[str, FakePage],
        now: datetime,
        origin: str,
        *,
        read_only: bool = False,
        session: SessionState | None = None,
    ) -> None:
        self.read_only = read_only
        self._pages = pages
        self._now = now
        self._origin = origin
        self._session = session
        self._url: str | None = None
        self._log: list[ObservedRequest] = []
        self.filled: dict[str, str] = {}
        self.closed = False
        self.agent_read = False

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
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
        self.filled[selector] = value

    async def click(self, selector: str) -> PageSnapshot:
        assert self._url is not None
        nxt = self._pages[self._url].on_click.get(selector)
        if nxt:
            return await self.goto(nxt)
        return self._snap()

    async def export_session(self) -> SessionState:
        if self._session is not None:
            return self._session.model_copy(deep=True)
        return SessionState(origin=self._origin, captured_at=self._now)

    async def close(self) -> None:
        self.closed = True


class FakeBrowserGateway:
    def __init__(self, pages: Mapping[str, FakePage], now: datetime, origin: str) -> None:
        self._pages = pages
        self._now = now
        self._origin = origin
        self.handles: list[FakeBrowserHandle] = []
        self._users: dict[str, FakeBrowserHandle] = {}

    async def open(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
        read_only: bool = False,
        background: bool = False,
        agent_read: bool = False,
    ) -> FakeBrowserHandle:
        previous = self._users.get(user_id)
        if background and previous is not None and not previous.closed:
            if agent_read or not previous.agent_read:
                raise BrowserBusy()
            previous.closed = True  # the reading of the agent gives way to a newer action
        handle = FakeBrowserHandle(
            self._pages, self._now, self._origin, read_only=read_only, session=session
        )
        handle.agent_read = agent_read
        self._users[user_id] = handle
        self.handles.append(handle)
        return handle


class SessionProvider(Protocol):
    """Implementation: botai.browser.login (W2).

    Returns a valid session for the connection: from the Vault if still good; otherwise logs in
    again with the stored password; otherwise asks the user through `ctx` (mirrored form fields,
    push-wait, fallback screen). Raises BotaiError(login_failed | source_bot_protection) on failure.
    """

    async def ensure(
        self, ctx: object, *, user_id: str, connection_id: str, force: bool = False
    ) -> SessionState:
        """`force` skips the stored session: the caller has just seen the site refuse it."""
        ...
