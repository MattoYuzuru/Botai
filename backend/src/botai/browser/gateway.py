"""PlaywrightGateway: Chromium for university logins and learning (core.browser.BrowserGateway).

* One in-memory browser context per connection, all traffic through the EgressProxy with that
  connection's token; `--host-resolver-rules` leaves the browser without DNS, so a page cannot
  reach anything but the proxy even if a bypass rule slipped in.
* No stealth, no fingerprint changes: the stock Chromium identifies itself as it is.
* At most one live context per user (a newer one replaces the older), a global cap, a hard
  lifetime. Page content, typed values and network samples live in memory only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import SecretStr

from botai.browser.egress import LOGIN_TOKEN_TTL_S, Egress
from botai.browser.forms import CaptchaPage, PushWait, detect, has_credential_fields
from botai.browser.pagescan import SCAN_JS, build_snapshot
from botai.browser.probe import requires_authentication
from botai.browser.read_capabilities import ReadCapabilityRegistry
from botai.browser.read_views import ReadView, ReadViewRegistry
from botai.browser.session import (
    MAX_STORAGE_BYTES,
    MAX_STORAGE_ENTRIES,
    MAX_STORAGE_KEY_CHARS,
    MAX_STORAGE_ORIGINS,
    MAX_STORAGE_VALUE_CHARS,
    bearer_expired,
    bounded_browser_storage,
    jwt_expiry,
    storage_origin,
)
from botai.contracts.browser_plan import BrowserPlan
from botai.core.browser import (
    BrowserBusy,
    ObservedRequest,
    PageSnapshot,
    SessionBearer,
    SessionCookie,
    SessionOriginStorage,
    SessionState,
    auth_origin,
)
from botai.core.clock import Clock, SystemClock
from botai.core.diagnostics import diagnostic_context, diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode
from botai.core.read_capabilities import ReadActionResult, ReadCapability

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_MS = 3_000
# After a login step: cabinets keep analytics and long polls busy forever, so a step waits this
# long for quiet and then goes on with whatever the page shows. Silence is not proof of anything.
AUTH_SETTLE_MS = 8_000
READ_VIEW_QUIET_S = 0.2
MAX_LOG = 500
AUTH_EVALUATE_S = 0.5
# One step of the login (an e-mail, a password, a code) opens this window for its auth frame: the
# page's own scripts may then post to the login hosts of the connection. Never longer: a script that
# outlives the step is not part of it.
AUTH_WINDOW_S = 15.0
# POSTs a window serves once the step is sent (a click or Enter): a check of the login, the
# submission itself and a spare. What the page posts while the value is still being typed (an
# `input` handler's telemetry) has a budget of its own and cannot eat this one.
AUTH_WINDOW_POSTS = 4
AUTH_TYPING_POSTS = 16
# A document that is the answer to an approved submission may post once more on its own: an
# auto-submitted `form_post`/SAML message. It must carry only the fields of those protocols.
AUTH_FOLLOWUP_S = 10.0
_PROTOCOL_FIELDS = frozenset(
    {
        "samlresponse", "samlrequest", "relaystate", "sigalg", "signature",
        "id_token", "code", "state", "session_state", "access_token", "token_type",
        "expires_in", "scope", "iss", "error", "error_description",
        "wa", "wresult", "wctx", "wtrealm", "wreply",
    }
)  # fmt: skip
_GUARD_REASONS = frozenset(
    {"auth_host_not_allowed", "auth_window_exhausted", "auth_window_expired", "auth_scan_failed"}
)
_AUTH_SCAN_JS = (
    "() => { const result = (" + SCAN_JS + ")(); result.title = document.title; return result; }"
)
SAMPLE_CHARS = 64_000  # a whole timetable or marks page as JSON, so the learner can parse it
SAMPLE_BUDGET = 4_000_000  # characters of samples one browser context may hold
HOLD_MAX_S = 30 * 60

BearerFrom = Literal["none", "localStorage", "sessionStorage", "authorizationHeader"]

_TEXTUAL = re.compile(r"json|text/|xml|javascript|html", re.IGNORECASE)
_TOKEN_KEYS = re.compile(
    r'"(?:access_token|refresh_token|id_token|token|password|secret|client_secret)"\s*:',
    re.IGNORECASE,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_NAV_URL = re.compile(r"\bat (https?://[^\s]+)")
_OBSERVED_TYPES = frozenset({"xhr", "fetch", "document"})
_SETTLE_TYPES = _OBSERVED_TYPES | {"script"}
_EMPTY_DIAGNOSTIC_CONTEXT: dict[str, object] = {
    "job_id": None,
    "thread_id": None,
    "connection_id": None,
    "request_id": None,
    "message_id": None,
    "block_id": None,
    "job_kind": None,
}
_SESSION_STORAGE_JS = """() => {
  const items = [];
  let size = 0, complete = true;
  try {
    if (sessionStorage.length > 64) complete = false;
    for (let i = 0; i < sessionStorage.length && items.length < 64; i++) {
      const name = sessionStorage.key(i);
      if (!name || name.length > 256) { complete = false; continue; }
      const value = sessionStorage.getItem(name);
      if (value === null || value.length > 32768 || size + name.length + value.length > 262144) {
        complete = false; continue;
      }
      items.push({name, value});
      size += name.length + value.length;
    }
  } catch (_) { complete = false; }
  return {origin: location.origin, items, complete};
}"""
_RESTORE_SESSION_STORAGE_JS = """(() => {
  const origins = JSON.parse(__BOTAI_STORAGE_DATA__);
  if (!Object.prototype.hasOwnProperty.call(origins, location.origin)) return;
  try {
    for (const [key, value] of Object.entries(origins[location.origin])) {
      if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value);
    }
  } catch (_) {}
})();"""


def _browser_diagnostics(connection_id: str) -> dict[str, object]:
    # Playwright dispatch runs in its launcher's task context. The owner of this browser
    # supplies correlation; absent IDs explicitly suppress any stale dispatcher context.
    return {**_EMPTY_DIAGNOSTIC_CONTEXT, **diagnostic_context(), "connection_id": connection_id}


def _initiator_url(request: Request) -> str | None:
    """The document of the frame that issued the request; a worker has no frame."""
    try:
        url = cast("object", request.frame.url)
    except PlaywrightError:
        return None
    return url if isinstance(url, str) and url else None


def _method(method: str) -> str:
    return (
        method
        if method in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
        else "OTHER"
    )


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """What `export_session` takes from the browser (login-preset `capture`)."""

    cookies_for: tuple[str, ...] | None = None  # default: every allowed host
    bearer_from: BearerFrom = "authorizationHeader"
    bearer_key: str | None = None


@dataclass(slots=True, repr=False)
class _AuthWindow:
    """What one login step lets its auth frame do: post to `hosts` until `expires`.

    The window belongs to one document of one page. It carries no URL: a login script sets its
    form's action at submission time and checks a login before it posts, so the address of a
    submission cannot be known in advance. What can be known is who asked (this frame, right after
    the step), and where the connection is allowed to log in (`hosts`)."""

    page: Page
    epoch: int
    url: str
    hosts: frozenset[str]
    suffixes: tuple[str, ...]
    selectors: frozenset[str]
    expires: float
    posts: int = 0
    sent: bool = False  # the step's click or Enter has happened: `posts` counts from there

    def allows(self, host: str) -> bool:
        return host in self.hosts or any(host.endswith("." + suffix) for suffix in self.suffixes)


@dataclass(slots=True, repr=False)
class _Followup:
    """The one POST navigation the document that answered an approved submission may make."""

    page: Page
    epoch: int  # the epoch of that document: the one after the submission was made
    window: _AuthWindow
    expires: float


def _auth_ui_present(snap: PageSnapshot, selectors: frozenset[str] = frozenset()) -> bool:
    """Does the frame still ask for a login, a code, a captcha or a push approval? `selectors`
    are the fields the flow was filling: a learned step may use fields a guess would not call
    credentials. A hidden form asks for nothing."""
    if has_credential_fields(snap) or isinstance(detect(snap), CaptchaPage | PushWait):
        return True
    live = {
        field.selector
        for form in snap.forms
        for field in form.fields
        if field.input_type.lower() != "hidden" and getattr(field, "visible", True)
    }
    return bool(selectors) and selectors <= live


def _protocol_message(request: Request) -> bool:
    """Is this POST an authentication protocol message (SAML, OpenID `form_post`), not a write?
    A form-encoded body whose every field is one of the protocols' own. Only the names are read."""
    try:
        if (
            not request.headers.get("content-type", "")
            .lower()
            .startswith("application/x-www-form-urlencoded")
        ):
            return False
        body = request.post_data
        if not body:
            return False
        names = {name.lower() for name, _ in parse_qsl(body, keep_blank_values=True)}
    except (PlaywrightError, ValueError, UnicodeError):
        return False
    return bool(names) and names <= _PROTOCOL_FIELDS


def in_container() -> bool:
    """Docker and Podman mark themselves; a container is the isolation boundary there."""
    return Path("/.dockerenv").exists() or bool(os.environ.get("container"))  # noqa: SIM112


def sandbox_default() -> bool:
    """Chromium's own sandbox: on, except inside a container (its default seccomp profile forbids
    the user namespaces the sandbox needs) or when `BOTAI_BROWSER_SANDBOX=0|1` says otherwise."""
    explicit = os.environ.get("BOTAI_BROWSER_SANDBOX")
    if explicit is not None:
        return explicit != "0"
    return not in_container()


def scrub_sample(text: str) -> str | None:
    """A response sample safe to hand to the learner: nothing that looks like a credential."""
    if _TOKEN_KEYS.search(text):
        return None
    return _JWT.sub("[jwt]", text)[:SAMPLE_CHARS]


def guard_refusal_error(*, fallback_used: bool = False) -> BotaiError:
    """What the user is told when our own guard stopped a request of the login: not an outage."""
    return BotaiError(
        ErrorCode.LOGIN_GUARD_REFUSED,
        "Ботай остановил отправку формы входа: сайт вуза отправляет её способом, который мы не "
        "можем безопасно проверить. Это не значит, что сайт недоступен. "
        + (
            "Запасной экран уже открывали, и это не помогло: попробуйте позже."
            if fallback_used
            else "Повторите шаг или завершите вход на запасном экране."
        ),
    )


def map_navigation_error(
    exc: PlaywrightError, requested: str, *, guarded: bool = False
) -> BotaiError:
    """Classify a main-frame failure without exposing its URL parameters. `guarded`: the page
    runs behind the login guard, whose refusal reaches the navigation as BLOCKED_BY_CLIENT."""
    message = str(exc)
    if guarded and "ERR_BLOCKED_BY_CLIENT" in message:
        return guard_refusal_error()
    match = _NAV_URL.search(message)
    host = urlsplit(match.group(1) if match else requested).hostname or "?"
    if any(
        code in message
        for code in ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY", "ERR_NO_SUPPORTED_PROXIES")
    ):
        return BotaiError(
            ErrorCode.EGRESS_BLOCKED,
            f"Сайт вуза ведёт на {host}, а этот адрес не входит в разрешённые для подключения",
            extra={"host": host},
        )
    if "ERR_CERT" in message or "ERR_SSL" in message:
        return BotaiError(ErrorCode.SOURCE_UNAVAILABLE, f"Сертификат сайта {host} не принят")
    return BotaiError(ErrorCode.SOURCE_UNAVAILABLE, f"Сайт {host} не отвечает")


async def _read_only_request(
    route: Route, *, diagnostic_facts: dict[str, object] | None = None
) -> None:
    if route.request.method in {"GET", "HEAD", "OPTIONS"}:
        await route.fallback()
    else:
        diagnostic_event(
            "browser.read_only_blocked",
            **{
                **_EMPTY_DIAGNOSTIC_CONTEXT,
                **(diagnostic_facts or {}),
                "method": _method(route.request.method),
                **url_facts(route.request.url),
            },
        )
        await route.abort("blockedbyclient")


async def _read_only_socket(socket: WebSocketRoute) -> None:
    await socket.close(code=1008, reason="read-only browser")


def _host_matches(domain: str, hosts: Iterable[str]) -> bool:
    """Would a browser send a cookie with this `domain` attribute to one of `hosts`?

    A leading dot marks a domain cookie (subdomains included); without it the cookie is
    host-only."""
    bare = domain.lstrip(".").lower()
    if domain.startswith("."):
        return any(h == bare or h.endswith("." + bare) for h in hosts)
    return bare in hosts


class PlaywrightHandle:
    """A live page. Implements core.browser.BrowserHandle; the extras serve LoginFlow/screen."""

    def __init__(
        self,
        *,
        gateway: PlaywrightGateway,
        context: BrowserContext,
        page: Page,
        user_id: str,
        connection_id: str,
        allow_hosts: Sequence[str],
        origin: str,
        capture: CaptureConfig,
        clock: Clock,
        read_only: bool = False,
        injected_authorizations: dict[Request, str] | None = None,
        confirmed_entry_url: SecretStr | None = None,
        restored_bearers: Mapping[str, tuple[SecretStr, datetime | None]] | None = None,
        suffixes: Sequence[str] = (),
    ) -> None:
        self._gateway = gateway
        self.context = context
        self._page = page
        self.user_id = user_id
        self.connection_id = connection_id
        self._diagnostic_facts = _browser_diagnostics(connection_id)
        self._read_view_registry = ReadViewRegistry()
        self._read_capability_registry = ReadCapabilityRegistry(page, now=clock.now)
        self._retired_capability_tasks: set[asyncio.Task[None]] = set()
        self.allow_hosts = tuple(allow_hosts)
        self._suffixes = tuple(suffix.lower() for suffix in suffixes)
        self.origin = origin
        self._capture = capture
        self._clock = clock
        self.read_only = read_only
        self._confirmed_entry_url = confirmed_entry_url
        self._entry_guarded = False
        self._entry_verifying = 0
        self._auth_epochs: dict[Frame, int] = {}
        self._auth_windows: dict[Frame, _AuthWindow] = {}
        self._auth_followups: dict[Frame, _Followup] = {}
        self._steps_open = 0
        self._guard_refusals: list[tuple[str, str]] = []  # (reason, host) while a step is open
        self._refused_navigations: dict[Request, tuple[str, str]] = {}
        self._guard_refused_navigation = False
        self._log: list[ObservedRequest] = []
        self._entries: dict[Request, ObservedRequest] = {}
        self._request_versions: dict[Request, int] = {}
        self._injected_authorizations = (
            injected_authorizations if injected_authorizations is not None else {}
        )
        self._samples: set[asyncio.Task[None]] = set()
        self._auth_captures: set[asyncio.Task[None]] = set()
        self._inflight: set[Request] = set()
        self._last_activity = monotonic()
        self._activity_generation = 0
        self._sampled = 0
        self._failed_diagnostics = 0
        self._bearer: dict[str, str] = {}  # exact origin -> last bearer the browser sent
        self._bearer_versions: dict[str, tuple[int, int]] = {}
        self._bearer_captured_at: dict[str, datetime] = {}
        self._restored_bearers = dict(restored_bearers or {})
        self._request_serial = 0
        self._observation_bearers: set[str] = set()
        self._observation_bearer_bytes = 0
        self._secret_inventory_failed = False
        self._visited: dict[str, None] = {}
        self._blocked: list[str] = []  # one entry per refused tunnel, oldest first
        self._navigation_request: Request | None = None
        self._navigation_error: BotaiError | None = None
        self._close_callbacks: list[Callable[[], None]] = []
        self._page_callbacks: list[Callable[[], None]] = []
        self.closed = False
        self._reaper: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        context.on("page", self._on_page)
        page.on("framenavigated", self._revoke_auth_document)
        page.on("framedetached", self._forget_auth_document)
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_failed)
        context.on("requestfinished", self._on_finished)

    def _diagnostic(self, event: str, **facts: object) -> None:
        diagnostic_event(event, **{**facts, **self._diagnostic_facts})

    # -- observation -------------------------------------------------------------------------

    @property
    def page(self) -> Page:
        return self._page

    @property
    def url(self) -> str:
        return self._page.url

    def visited_hosts(self) -> list[str]:
        """Hosts the browser asked for, in first-seen order (allowed or not)."""
        return list(self._visited)

    def blocked_hosts(self) -> list[str]:
        """Hosts whose tunnel the proxy refused, first-seen order."""
        return list(dict.fromkeys(self._blocked))

    def navigation_failure(self) -> BotaiError | None:
        """Only the current main-page navigation, never a blocked image or an old request."""
        return self._navigation_error

    def _revoke_auth_document(self, frame: Frame) -> None:
        self._auth_epochs[frame] = self._auth_epochs.get(frame, 0) + 1
        if frame == frame.page.main_frame:
            self._auth_windows.clear()
        else:
            self._auth_windows.pop(frame, None)

    def _forget_auth_document(self, frame: Frame) -> None:
        self._auth_windows.pop(frame, None)
        self._auth_followups.pop(frame, None)
        self._auth_epochs.pop(frame, None)

    def take_guard_refusals(self) -> list[tuple[str, str]]:
        """What the guard refused to a window since the last call: `(reason, host)`, the host
        empty unless the reason is a host. The caller decides which of it matters."""
        refusals, self._guard_refusals = self._guard_refusals, []
        return refusals

    def _auth_refused(self, request: Request, reason: str) -> bool:
        host = (urlsplit(request.url).hostname or "").lower()
        navigation = request.is_navigation_request()
        self._diagnostic(
            "browser.auth_permit_refused",
            outcome="rejected",
            reason=reason,
            method=_method(request.method),
            navigation=navigation,
            **url_facts(request.url),
        )
        if navigation and len(self._refused_navigations) < 64:
            self._refused_navigations[request] = (reason, host)
        if self._steps_open and reason in _GUARD_REASONS and len(self._guard_refusals) < 32:
            self._guard_refusals.append((reason, host if reason == "auth_host_not_allowed" else ""))
        return False

    def _refuse_and_close(self, request: Request, frame: Frame, reason: str) -> bool:
        """The page no longer is what the window was opened for: nothing it posts later is the
        step's, and the window must not wait for a document submission to be used."""
        self._auth_windows.pop(frame, None)
        return self._auth_refused(request, reason)

    async def _entry_auth_allowed(self, request: Request) -> bool:
        """May this POST leave an auth frame behind the entry guard?

        Yes while the frame's window is open (a step just began, see `auth_step`), the host is one
        the connection logs in at, and the frame still shows an auth UI. Which URL, and whether the
        form says so, does not matter: login scripts choose both at submission time."""
        if self.read_only or self._entry_verifying:
            return self._auth_refused(request, "auth_read_only")
        try:
            frame = request.frame
            window = self._auth_windows.get(frame)
            if window is None:
                if request.is_navigation_request() and frame in self._auth_followups:
                    return self._followup_allowed(request, frame)
                return self._auth_refused(request, "auth_no_permit")
            if monotonic() >= window.expires:
                return self._refuse_and_close(request, frame, "auth_window_expired")
            if (
                frame.page != self._page
                or self._page != window.page
                or self._auth_epochs.get(frame, 0) != window.epoch
                or frame.url != window.url
            ):
                return self._refuse_and_close(request, frame, "auth_context_changed")
            if not window.allows((urlsplit(request.url).hostname or "").lower()):
                return self._auth_refused(request, "auth_host_not_allowed")
            if window.posts >= (AUTH_WINDOW_POSTS if window.sent else AUTH_TYPING_POSTS):
                return self._auth_refused(request, "auth_window_exhausted")
            window.posts += 1  # taken before any await: concurrent requests share the budget
            if request.is_navigation_request():
                # Never inspect a renderer suspended by this document POST. The submission ends
                # the step: the document it brings has no window of its own, only a follow-up.
                self._auth_windows.pop(frame, None)
                self._auth_followups[frame] = _Followup(
                    page=window.page,
                    epoch=window.epoch + 1,
                    window=window,
                    expires=monotonic() + AUTH_FOLLOWUP_S,
                )
            else:
                # Polling can remove the auth UI without changing a SPA's URL: a data page's
                # script must not inherit a window. No navigation waits inside interception.
                async with asyncio.timeout(AUTH_EVALUATE_S):
                    raw: Any = await frame.evaluate(_AUTH_SCAN_JS)
                snap = build_snapshot(window.url, str(raw.get("title", "")), raw)
                if (
                    self._auth_windows.get(frame) is not window
                    or self._page != window.page
                    or frame.page != window.page
                    or self._auth_epochs.get(frame, 0) != window.epoch
                    or frame.url != window.url
                ):
                    return self._refuse_and_close(request, frame, "auth_context_changed")
                if not _auth_ui_present(snap, window.selectors):
                    return self._refuse_and_close(request, frame, "auth_ui_missing")
            self._diagnostic(
                "browser.auth_permit",
                outcome="accepted",
                phase="response",
                method=_method(request.method),
                navigation=request.is_navigation_request(),
                post_count=window.posts,
                **url_facts(request.url),
            )
            return True
        except (PlaywrightError, TimeoutError):
            with contextlib.suppress(PlaywrightError):
                self._auth_windows.pop(request.frame, None)
            return self._auth_refused(request, "auth_scan_failed")

    def _followup_allowed(self, request: Request, frame: Frame) -> bool:
        """The document that answered an approved submission posts once on its own: an
        auto-submitted `form_post` or SAML message. Only the document that came right after the
        submission, only soon, only to a login host, only with the protocol's own fields."""
        follow = self._auth_followups.pop(frame)
        if monotonic() >= follow.expires:
            return self._auth_refused(request, "auth_window_expired")
        if (
            frame.page != self._page
            or self._page != follow.page
            or self._auth_epochs.get(frame, 0) != follow.epoch
        ):
            return self._auth_refused(request, "auth_context_changed")
        if not follow.window.allows((urlsplit(request.url).hostname or "").lower()):
            return self._auth_refused(request, "auth_host_not_allowed")
        if not _protocol_message(request):
            return self._auth_refused(request, "auth_followup_rejected")
        self._diagnostic(
            "browser.auth_permit",
            outcome="accepted",
            phase="response",
            method=_method(request.method),
            navigation=True,
            followup=True,
            **url_facts(request.url),
        )
        return True

    async def _scan_auth_window(
        self, frame: Frame, *, selectors: frozenset[str] = frozenset(), sent: bool = False
    ) -> _AuthWindow | None:
        """A window for what `frame` shows now, or None (and why, in the trace). Stores nothing."""
        page = self._page
        epoch = self._auth_epochs.get(frame, 0)
        url = frame.url
        try:
            async with asyncio.timeout(AUTH_EVALUATE_S):
                raw: Any = await frame.evaluate(_AUTH_SCAN_JS)
            snap = build_snapshot(url, str(raw.get("title", "")), raw)
            if (
                page != self._page
                or frame.page != page
                or frame.url != url
                or self._auth_epochs.get(frame, 0) != epoch
            ):
                self._diagnostic(
                    "browser.auth_permit",
                    outcome="rejected",
                    reason="auth_context_changed",
                    **url_facts(url),
                )
                return None
            if not _auth_ui_present(snap, selectors):
                self._diagnostic(
                    "browser.auth_permit",
                    outcome="rejected",
                    reason="auth_ui_missing",
                    **url_facts(url),
                )
                return None
        except (PlaywrightError, TimeoutError):
            self._diagnostic(
                "browser.auth_permit",
                outcome="rejected",
                reason="auth_scan_failed",
                **url_facts(url),
            )
            return None
        # The login hosts the connection already trusts (and, on a first login, the subdomains
        # its proxy grant follows the site to), and the frame's own: the user is told which one
        # they are typing into, and the page reached it through the same proxy grant.
        hosts = set(self.allow_hosts)
        if frame_host := (urlsplit(url).hostname or "").lower():
            hosts.add(frame_host)
        window = _AuthWindow(
            page=page,
            epoch=epoch,
            url=url,
            hosts=frozenset(hosts),
            suffixes=self._suffixes,
            selectors=selectors,
            expires=monotonic() + AUTH_WINDOW_S,
            sent=sent,
        )
        self._diagnostic(
            "browser.auth_permit",
            outcome="accepted",
            phase="request",
            window_ms=int(AUTH_WINDOW_S * 1000),
            allow_host_count=len(hosts),
            **url_facts(url),
        )
        return window

    async def _arm_auth_window(
        self, frame: Frame, *, selectors: frozenset[str] = frozenset()
    ) -> _AuthWindow | None:
        window = await self._scan_auth_window(frame, selectors=selectors)
        if window is not None:
            self._auth_windows[frame] = window
            self._auth_followups.pop(frame, None)  # a new step supersedes the last submission
        return window

    def _mark_sent(self) -> None:
        """The click or Enter that sends the step: what the page posts from now on is the step's
        own, and has the window's real budget."""
        window = self._auth_windows.get(self._page.main_frame)
        if window is not None and not window.sent:
            window.sent = True
            window.posts = 0

    @contextlib.asynccontextmanager
    async def read_only_entry(self) -> AsyncGenerator[None]:
        """Keep entry documents read-only, including scripts that run after navigation settles."""
        if not self._entry_guarded:

            async def guard(route: Route) -> None:
                if route.request.method in {"GET", "HEAD", "OPTIONS"} or (
                    route.request.method == "POST" and await self._entry_auth_allowed(route.request)
                ):
                    await route.fallback()
                else:
                    await _read_only_request(route, diagnostic_facts=self._diagnostic_facts)

            async def socket_guard(socket: WebSocketRoute) -> None:
                # A socket opened at login can survive a same-URL SPA transition.
                # Do not leave an unrestricted write channel on the data page.
                await _read_only_socket(socket)

            await self.context.route("**/*", guard)
            await self.context.route_web_socket("**/*", socket_guard)
            self._entry_guarded = True
        self._entry_verifying += 1
        self._auth_windows.clear()
        self._auth_followups.clear()
        try:
            yield
        finally:
            self._entry_verifying -= 1

    @contextlib.asynccontextmanager
    async def auth_step(self, *, fields: Sequence[str] = ()) -> AsyncGenerator[None]:
        """One step of the login: the user (or the password they stored) has just given the value.

        Opens the window of the main frame for the length of the step, so the page's own scripts
        may check the login and submit the form; every step opens its own. `fields` are the
        selectors being filled. On leaving, waits for the page to settle and reports a navigation
        our guard stopped as `login_guard_refused` (or, for a host, as `egress_blocked`, which the
        flow can put to the student), never as a site that does not answer."""
        page = self._page
        frame = page.main_frame
        previous_navigation = self._navigation_request
        window = (
            await self._arm_auth_window(frame, selectors=frozenset(fields))
            if self._entry_guarded
            else None
        )
        if self._entry_guarded and window is None:
            self._guard_refusals.append(("auth_scan_failed", ""))  # no window: nothing will post
        self._steps_open += 1
        deadline = monotonic() + AUTH_SETTLE_MS / 1000
        try:
            yield
            if self._entry_guarded:
                while True:
                    if (
                        self._page == page
                        and self._navigation_request is not None
                        and self._navigation_request is not previous_navigation
                    ):
                        failure = self.navigation_failure()
                        if failure is not None:
                            raise failure
                    if (
                        not self._inflight
                        and monotonic() - self._last_activity >= READ_VIEW_QUIET_S
                    ):
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break  # still busy (analytics, long polls): the flow reads the page as is
                    await asyncio.sleep(min(0.02, remaining))
        finally:
            self._steps_open -= 1
            if self._auth_windows.get(frame) is window:
                self._auth_windows.pop(frame, None)

    async def arm_human_auth(self) -> None:
        """Before a real keystroke or click on the screen, open a window on every recognized auth
        frame of the page. The human's input is the budget: each one opens windows of its own, and
        they replace the last ones at once, so there is no moment without a window in between."""
        if not self._entry_guarded or self._entry_verifying:
            return
        fresh: dict[Frame, _AuthWindow] = {}
        try:
            async with asyncio.timeout(1.0):
                for frame in self._page.frames[:32]:
                    window = await self._scan_auth_window(frame, sent=True)
                    if window is not None:
                        fresh[frame] = window
        except TimeoutError:
            self._auth_windows.clear()
            return
        self._auth_windows = fresh
        self._auth_followups.clear()

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def on_page_change(self, callback: Callable[[], None]) -> None:
        """Called when a popup or new tab becomes the current page."""
        self._page_callbacks.append(callback)

    def _on_page(self, page: Page) -> None:
        self._auth_windows.clear()
        self._auth_followups.clear()
        page.on("framenavigated", self._revoke_auth_document)
        page.on("framedetached", self._forget_auth_document)
        self._read_view_registry.invalidate()
        old = self._read_capability_registry
        old.invalidate()
        cleanup = asyncio.create_task(old.aclose())
        self._retired_capability_tasks.add(cleanup)
        cleanup.add_done_callback(self._retired_capability_tasks.discard)
        self._page = page
        self._read_capability_registry = ReadCapabilityRegistry(page, now=self._clock.now)
        self._navigation_request = None
        self._navigation_error = None
        for callback in list(self._page_callbacks):
            callback()

    def _on_request(self, request: Request) -> None:
        if request.resource_type in _SETTLE_TYPES:
            self._inflight.add(request)
            self._last_activity = monotonic()
            self._activity_generation += 1
        if request.is_navigation_request():
            # An early popup request may precede its Frame. It is not yet a known
            # main-page navigation and must not become a host-consent candidate.
            with contextlib.suppress(PlaywrightError):
                if request.frame == self._page.main_frame:
                    self._navigation_request = request
                    self._navigation_error = None
        host = urlsplit(request.url).hostname
        self._request_serial += 1
        serial = self._request_serial
        if host:
            self._visited.setdefault(host.lower(), None)
            self._remember_bearer(
                request.url,
                request.headers.get("authorization", ""),
                serial,
                injected=request in self._injected_authorizations,
            )
        if request.resource_type not in _OBSERVED_TYPES or len(self._log) >= MAX_LOG:
            return
        entry = ObservedRequest(
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
            request_content_type=request.headers.get("content-type"),
            evidence_id=f"r{serial}",
            initiator_url=_initiator_url(request),
        )
        self._log.append(entry)
        self._entries[request] = entry
        self._request_versions[request] = serial
        task = asyncio.get_running_loop().create_task(self._capture_auth(request, entry, serial))
        self._auth_captures.add(task)
        task.add_done_callback(self._auth_captures.discard)
        self._diagnostic(
            "browser.request",
            connection_id=self.connection_id,
            method=_method(request.method),
            resource_type=request.resource_type,
            read_only=self.read_only,
            **url_facts(request.url),
        )

    def _remember_bearer(
        self, url: str, authorization: str, serial: int, *, injected: bool
    ) -> None:
        origin = auth_origin(url)
        host = (urlsplit(url).hostname or "").lower()
        if origin is None or host not in self.allow_hosts:
            return
        if authorization:
            self._remember_secret(authorization)
            self._remember_secret(authorization.partition(" ")[2].strip())
        if not authorization.lower().startswith("bearer "):
            return
        token = authorization[7:].strip()
        if not token:
            return
        if len(token) > 32_768:
            self._secret_inventory_failed = True
            return
        # The application's own credential supersedes the saved fallback, even when a later
        # request omitted its header or asynchronous header capture completes out of order.
        version = (0 if injected else 1, serial)
        if version >= self._bearer_versions.get(origin, (-1, 0)):
            self._bearer[origin] = token
            self._bearer_versions[origin] = version
            self._bearer_captured_at[origin] = self._clock.now()

    def _remember_secret(self, token: str) -> None:
        if token and token not in self._observation_bearers:
            try:
                size = len(token.encode("utf-8"))
            except UnicodeError:
                self._secret_inventory_failed = True
                return
            if (
                len(self._observation_bearers) >= 4096
                or self._observation_bearer_bytes + size > 1_048_576
            ):
                self._secret_inventory_failed = True
            else:
                self._observation_bearers.add(token)
                self._observation_bearer_bytes += size

    async def _capture_auth(self, request: Request, entry: ObservedRequest, serial: int) -> None:
        try:
            async with asyncio.timeout(2.0):
                headers = await request.all_headers()
            authorization = self._injected_authorizations.get(
                request, headers.get("authorization", "")
            ).strip()
            entry.has_cookie = bool(headers.get("cookie", "").strip())
            for pair in headers.get("cookie", "").split(";"):
                self._remember_secret(pair.partition("=")[2].strip().strip('"'))
            entry.has_authorization = bool(authorization)
            entry.authorization_scheme = (
                "bearer"
                if authorization.lower().startswith("bearer ") and authorization[7:].strip()
                else "other"
                if authorization
                else "none"
            )
            self._remember_bearer(
                request.url,
                authorization,
                serial,
                injected=request in self._injected_authorizations,
            )
            entry.auth_proven = True
        except (PlaywrightError, TimeoutError):
            # Without a complete inventory, reflected headers may not be passed to the model.
            self._secret_inventory_failed = True

    def _on_response(self, response: Response) -> None:
        entry = self._entries.get(response.request)
        if entry is None:
            return
        entry.status = response.status
        injected = self._injected_authorizations.get(response.request)
        if injected is not None:
            # Playwright's original all_headers() omits executor route overrides. A completed
            # response ties the exact injected header to this one request, not to session presence.
            entry.has_authorization = True
            entry.authorization_scheme = "bearer"
            self._remember_bearer(
                response.request.url,
                injected,
                self._request_versions[response.request],
                injected=True,
            )
        content_type = response.headers.get("content-type")
        entry.response_content_type = content_type
        self._diagnostic(
            "browser.response",
            connection_id=self.connection_id,
            status=entry.status,
            resource_type=response.request.resource_type,
            json_response=bool(content_type and "json" in content_type.lower()),
            html_response=bool(content_type and "html" in content_type.lower()),
            **url_facts(response.url),
        )
        if (
            response.request.resource_type in ("xhr", "fetch")
            and content_type
            and _TEXTUAL.search(content_type)
        ):
            task = asyncio.get_running_loop().create_task(self._sample(response, entry))
            self._samples.add(task)
            task.add_done_callback(self._samples.discard)

    async def _sample(self, response: Response, entry: ObservedRequest) -> None:
        if self._sampled >= SAMPLE_BUDGET:
            self._diagnostic(
                "browser.sample",
                connection_id=self.connection_id,
                budget_exhausted=True,
                **url_facts(entry.url),
            )
            return
        with contextlib.suppress(PlaywrightError, UnicodeError):
            body = await response.body()
            sample = scrub_sample(body[: SAMPLE_CHARS * 4].decode("utf-8", "replace"))
            entry.response_sample = sample
            self._sampled += len(sample or "")
            self._diagnostic(
                "browser.sample",
                connection_id=self.connection_id,
                body_bytes=len(body),
                sample_chars=len(sample or ""),
                omitted_sensitive=sample is None,
                truncated=len(body) > SAMPLE_CHARS * 4
                or bool(sample and len(sample) == SAMPLE_CHARS),
                budget_exhausted=False,
                **url_facts(entry.url),
            )

    def _on_failed(self, request: Request) -> None:
        self._on_finished(request)
        failure = request.failure or ""
        if self._failed_diagnostics < MAX_LOG:
            self._failed_diagnostics += 1
            self._diagnostic(
                "browser.request_failed",
                connection_id=self.connection_id,
                proxy_failed="TUNNEL" in failure or "PROXY" in failure,
                blocked_by_client="BLOCKED_BY_CLIENT" in failure,
                navigation=request is self._navigation_request,
                **url_facts(request.url),
            )
        refused = self._refused_navigations.pop(request, None)
        if refused is not None and "BLOCKED_BY_CLIENT" in failure:
            self._guard_refused_navigation = True
        if request is self._navigation_request:
            self._navigation_error = (
                self._guard_navigation_error(refused)
                if refused is not None and "BLOCKED_BY_CLIENT" in failure
                else map_navigation_error(
                    PlaywrightError(failure), request.url, guarded=self._entry_guarded
                )
            )
        if "TUNNEL" in failure or "PROXY" in failure:
            host = urlsplit(request.url).hostname
            if host:
                self._blocked.append(host.lower())

    @staticmethod
    def _guard_navigation_error(refusal: tuple[str, str]) -> BotaiError:
        """Why our guard stopped a submission, as the flow can act on it: a host it may put to the
        student for approval, or a refusal it can only report."""
        reason, host = refusal
        if reason == "auth_host_not_allowed" and host:
            return BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                f"Сайт вуза отправляет данные входа на {host}, а этот адрес не входит "
                "в разрешённые для подключения",
                extra={"host": host, "post": True},
            )
        return guard_refusal_error()

    def _on_finished(self, request: Request) -> None:
        if request in self._inflight:
            self._inflight.discard(request)
            self._last_activity = monotonic()
            self._activity_generation += 1

    async def network_log(self) -> list[ObservedRequest]:
        pending = self._samples | self._auth_captures
        if pending:
            await asyncio.wait(list(pending), timeout=2.0)
        return [e.model_copy() for e in self._log]

    # -- actions -----------------------------------------------------------------------------

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username or host not in self.allow_hosts:
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                f"Адрес {host or '?'} не входит в разрешённые для этого подключения",
                extra={"host": host},
            )

    async def _settle(self, timeout_ms: int = SETTLE_MS) -> None:
        page = self._page
        with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)

    async def goto(self, url: str) -> PageSnapshot:
        self._read_view_registry.invalidate()
        self._read_capability_registry.reset()
        started = monotonic()
        self._diagnostic(
            "browser.navigation.start",
            connection_id=self.connection_id,
            read_only=self.read_only,
            **url_facts(url),
        )
        self._check_url(url)
        self._navigation_request = None
        self._navigation_error = None
        try:
            refused_before, self._guard_refused_navigation = self._guard_refused_navigation, False
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightError as exc:
                if (
                    not (self._entry_guarded and refused_before)
                    or "interrupted by another navigation" not in str(exc)
                    or isinstance(exc, PlaywrightTimeout)
                ):
                    raise
                # The error page of the submission our guard just refused commits late and cuts
                # ours short. Let it land, then go again.
                await self._settle(1_500)
                self._navigation_request = None
                self._navigation_error = None
                await self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            self._diagnostic(
                "browser.navigation.failed",
                connection_id=self.connection_id,
                timed_out=True,
                elapsed_ms=int((monotonic() - started) * 1000),
                **url_facts(url),
            )
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Сайт вуза не отвечает") from exc
        except PlaywrightError as exc:
            await asyncio.sleep(0.1)  # let the `requestfailed` event for the refused tunnel land
            self._diagnostic(
                "browser.navigation.failed",
                connection_id=self.connection_id,
                timed_out=False,
                elapsed_ms=int((monotonic() - started) * 1000),
                **url_facts(url),
            )
            raise self._navigation_error or map_navigation_error(
                exc, url, guarded=self._entry_guarded
            ) from exc
        await self._settle()
        snap = await self.snapshot()
        if self.read_only and (
            self._navigation_error is not None or snap.url.startswith("chrome-error:")
        ):
            raise self._navigation_error or BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Страница источника недоступна для чтения"
            )
        self._diagnostic(
            "browser.navigation.done",
            connection_id=self.connection_id,
            elapsed_ms=int((monotonic() - started) * 1000),
            redirected=snap.url != url,
            **url_facts(snap.url),
        )
        return snap

    async def snapshot(self) -> PageSnapshot:
        self._read_view_registry.invalidate()
        self._read_capability_registry.invalidate()
        last: PlaywrightError | None = None
        for _ in range(4):
            page = self._page
            try:
                raw: Any = await page.evaluate(SCAN_JS)
                snap = build_snapshot(page.url, await page.title(), raw)
                self._diagnostic(
                    "browser.snapshot",
                    connection_id=self.connection_id,
                    html_chars=len(snap.html),
                    text_chars=len(snap.text),
                    form_count=len(snap.forms),
                    captcha_suspected=snap.captcha_suspected,
                    auth_required=requires_authentication(snap),
                    credential_fields=any(
                        f.input_type == "password" for form in snap.forms for f in form.fields
                    ),
                    **url_facts(snap.url),
                )
                return snap
            except PlaywrightError as exc:  # navigation in flight destroys the execution context
                last = exc
                await asyncio.sleep(0.15)
                await self._settle(1_500)
        raise BotaiError(
            ErrorCode.SOURCE_UNAVAILABLE, "Страница вуза недоступна для чтения"
        ) from last

    async def fill(self, selector: str, value: str) -> None:
        self._read_capability_registry.invalidate()
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
        try:
            await self._page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            # `from None`: Playwright's call log can quote the value that was being typed
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Поле на странице вуза не найдено"
            ) from None

    async def read_capabilities(self) -> list[ReadCapability]:
        if not self.read_only or self.closed:
            return []
        return await self._read_capability_registry.discover()

    async def apply_read_capability(
        self, ref: str, *, value_ref: str | None = None
    ) -> ReadActionResult:
        if not self.read_only or self.closed:
            raise BotaiError(ErrorCode.CONFLICT, "Действие недоступно для чтения")
        self._read_view_registry.invalidate()
        return await self._read_capability_registry.apply(ref, value_ref=value_ref)

    async def replay_browser_plan(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]:
        if not self.read_only or self.closed:
            raise BotaiError(ErrorCode.CONFLICT, "План недоступен для чтения")
        self._read_view_registry.invalidate()
        return await self._read_capability_registry.replay(
            plan, values=values, items_selector=items_selector
        )

    async def read_views(self) -> list[ReadView]:
        if not self.read_only or self.closed:
            return []
        return await self._read_view_registry.discover(self._page)

    async def activate_read_view(self, ref: str) -> PageSnapshot:
        self._read_capability_registry.invalidate()
        if not self.read_only or self.closed:
            raise BotaiError(ErrorCode.CONFLICT, "Представление недоступно для чтения")
        panel_id = self._read_view_registry.panel_id(self._page, ref)
        # A SPA's earlier `networkidle` lifecycle may already be satisfied. Observe *new*
        # DOM mutations and in-flight read requests instead of reusing that old load state.
        # The observer contains only a timestamp, never page text or credential values.
        monitor = await self._page.evaluate_handle(
            """id => {
              let panel = document.getElementById(id);
              const state = {changedAt: performance.now(), changed: false};
              const presentation = new Set([
                'class', 'style', 'hidden', 'tabindex', 'aria-selected', 'aria-expanded',
                'aria-hidden', 'aria-busy', 'data-state', 'data-active', 'data-selected'
              ]);
              state.observer = new MutationObserver(records => {
                const current = document.getElementById(id);
                const contentChanged = current !== panel || records.some(record => {
                  const inside = (panel && panel.contains(record.target)) ||
                    (current && current.contains(record.target));
                  return inside && (record.type !== 'attributes' ||
                    !presentation.has(record.attributeName));
                });
                panel = current;
                if (contentChanged) {
                  state.changedAt = performance.now(); state.changed = true;
                }
              });
              state.observer.observe(document, {
                subtree: true, childList: true, attributes: true, characterData: true
              });
              return state;
            }""",
            panel_id,
        )
        try:
            network_before = self._activity_generation
            await self._read_view_registry.activate(self._page, ref)
            deadline = monotonic() + SETTLE_MS / 1000
            self._last_activity = monotonic()
            while monotonic() < deadline:
                quiet_ms: float = await monitor.evaluate(
                    "state => performance.now() - state.changedAt"
                )
                dom_changed: bool = await monitor.evaluate("state => state.changed")
                if (
                    (dom_changed or self._activity_generation > network_before)
                    and not self._inflight
                    and quiet_ms >= READ_VIEW_QUIET_S * 1000
                    and monotonic() - self._last_activity >= READ_VIEW_QUIET_S
                ):
                    return await self.snapshot()
                await asyncio.sleep(min(0.05, max(0, deadline - monotonic())))
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Представление источника не завершило загрузку"
            )
        except PlaywrightError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Представление страницы недоступно для чтения"
            ) from None
        finally:
            with contextlib.suppress(PlaywrightError):
                await monitor.evaluate("state => state.observer.disconnect()")
                await monitor.dispose()

    async def click(self, selector: str) -> PageSnapshot:
        self._read_capability_registry.invalidate()
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
        self._mark_sent()
        try:
            await self._page.click(selector, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Кнопка на странице вуза не найдена"
            ) from None
        await self._settle()
        return await self.snapshot()

    async def press(self, selector: str, key: str) -> PageSnapshot:
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
        self._mark_sent()
        try:
            await self._page.press(selector, key, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Поле на странице вуза не найдено"
            ) from None
        await self._settle()
        return await self.snapshot()

    # -- session -----------------------------------------------------------------------------

    def _cookie_hosts(self) -> tuple[str, ...]:
        return self._capture.cookies_for or self.allow_hosts

    async def _bearer_from_storage(self, store: str, key: str) -> str | None:
        for page in self.context.pages:
            host = (urlsplit(page.url).hostname or "").lower()
            if host not in self.allow_hosts:
                continue
            with contextlib.suppress(PlaywrightError):
                value: Any = await page.evaluate(
                    "([s, k]) => { try { return window[s].getItem(k); } catch (e) { return null; } }",
                    [store, key],
                )
                if isinstance(value, str) and value:
                    return value
        return None

    async def observation_secrets(self) -> tuple[SecretStr, ...]:
        """Complete in-memory inventory for prompt redaction, never a restorable session.

        Vault exports remain bounded and can omit unsupported entries. Observation instead
        refuses any incomplete inventory, and includes every tab store and bearer rotation.
        The caller applies its two-second deadline and the cumulative redaction byte bound.
        """
        try:
            if self._auth_captures:
                _, pending = await asyncio.wait(list(self._auth_captures), timeout=2.0)
                if pending:
                    self._secret_inventory_failed = True
            if self._secret_inventory_failed:
                raise ValueError("incomplete bearer inventory")
            values = [SecretStr(value) for value in self._observation_bearers]
            for cookie in await self.context.cookies():
                domain, value = cookie.get("domain"), cookie.get("value")
                if not isinstance(domain, str) or not isinstance(value, str):
                    raise ValueError("incomplete cookie inventory")
                if _host_matches(domain, self.allow_hosts):
                    values.append(SecretStr(value))
            origins: set[str] = set()
            storage_bytes = 0

            def record(rows: Any, origin: str) -> None:
                nonlocal storage_bytes
                if not isinstance(rows, list) or len(rows) > MAX_STORAGE_ENTRIES:
                    raise ValueError("incomplete storage inventory")
                if rows:
                    origins.add(origin)
                    if len(origins) > MAX_STORAGE_ORIGINS:
                        raise ValueError("storage origin budget")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("invalid storage inventory")
                    key, value = row.get("name"), row.get("value")
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > MAX_STORAGE_KEY_CHARS
                        or not isinstance(value, str)
                        or len(value) > MAX_STORAGE_VALUE_CHARS
                    ):
                        raise ValueError("unsupported storage entry")
                    storage_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
                    if storage_bytes > MAX_STORAGE_BYTES:
                        raise ValueError("storage byte budget")
                    values.append(SecretStr(value))

            state = await self.context.storage_state()
            for item in state.get("origins", []):
                origin = storage_origin(item["origin"], self.allow_hosts)
                if origin is not None:
                    record(item["localStorage"], origin)
            for page in self.context.pages:
                origin = storage_origin(page.url, self.allow_hosts)
                if origin is None:
                    continue
                raw: Any = await page.evaluate(_SESSION_STORAGE_JS)
                if (
                    not isinstance(raw, dict)
                    or raw.get("complete") is not True
                    or raw.get("origin") != origin
                ):
                    raise ValueError("incomplete tab storage inventory")
                # Session storage belongs to a tab, so two stores at one origin must both
                # be protected instead of overwriting one another in a SessionState map.
                record(raw.get("items"), origin)
            return tuple(values)
        except Exception:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Не удалось полностью защитить состояние браузера"
            ) from None

    async def export_session(self) -> SessionState:
        """Cookies of the capture hosts plus a bearer the browser itself sent (or the single
        explicitly named key). Bounded browser storage stays only in the encrypted session,
        separate from bearer discovery and from observations supplied to the model."""
        if self._auth_captures:
            await asyncio.wait(list(self._auth_captures), timeout=2.0)
        hosts = self._cookie_hosts()
        cookies: list[SessionCookie] = []
        for c in await self.context.cookies():
            name, value, domain = c.get("name"), c.get("value"), c.get("domain")
            if name is None or value is None or domain is None or not _host_matches(domain, hosts):
                continue
            expires = c.get("expires")
            cookies.append(
                SessionCookie(
                    name=name,
                    value=SecretStr(value),
                    domain=domain,
                    path=c.get("path", "/"),
                    expires=expires if expires is not None and expires > 0 else None,
                    http_only=bool(c.get("httpOnly", False)),
                    secure=bool(c.get("secure", True)),
                )
            )
        bearer: str | None = None
        bearer_scopes: list[SessionBearer] = []
        source = self._capture.bearer_from
        now = self._clock.now()
        if source == "authorizationHeader":
            primary = auth_origin(self.origin)
            bearer = self._bearer.get(primary) if primary is not None else None
            for origin, token in self._bearer.items():
                if (
                    auth_origin(origin) != origin
                    or (urlsplit(origin).hostname or "") not in self.allow_hosts
                ):
                    continue
                bearer_scopes.append(
                    SessionBearer(
                        origin=origin,
                        token=SecretStr(token),
                        captured_at=self._bearer_captured_at.get(origin, now),
                        expires_hint=self._bearer_expiry(origin, token),
                    )
                )
        elif source in ("localStorage", "sessionStorage") and self._capture.bearer_key:
            bearer = await self._bearer_from_storage(source, self._capture.bearer_key)
        expiries = [datetime.fromtimestamp(c.expires, UTC) for c in cookies if c.expires]
        hint: datetime | None = None
        if cookies and len(expiries) == len(cookies):
            hint = min(expiries)
        if bearer:
            exp = self._bearer_expiry(auth_origin(self.origin) or "", bearer)
            if exp is not None and (hint is None or exp < hint):
                hint = exp
        browser_storage = await self._export_browser_storage()
        self._diagnostic(
            "browser.export",
            connection_id=self.connection_id,
            cookie_count=len(cookies),
            bearer_present=bearer is not None,
            capture_from=source,
            expires_hint_present=hint is not None,
        )
        return SessionState(
            origin=self.origin,
            cookies=cookies,
            bearer=SecretStr(bearer) if bearer else None,
            bearer_scopes=bearer_scopes,
            captured_at=now,
            expires_hint=hint,
            browser_storage=browser_storage,
            entry_url=self._confirmed_entry_url,
        )

    def _bearer_expiry(self, origin: str, token: str) -> datetime | None:
        expiries = [jwt_expiry(token)]
        restored = self._restored_bearers.get(origin)
        if restored is not None and restored[0].get_secret_value() == token:
            expiries.append(restored[1])
        return min((expiry for expiry in expiries if expiry is not None), default=None)

    async def _export_browser_storage(self) -> list[SessionOriginStorage]:
        entries: dict[str, SessionOriginStorage] = {}
        try:
            state = await self.context.storage_state()
            for item in state.get("origins", []):
                origin = storage_origin(item["origin"], self.allow_hosts)
                if origin is None:
                    continue
                entries[origin] = SessionOriginStorage(
                    origin=origin,
                    local_storage={
                        value["name"]: SecretStr(value["value"])
                        for value in item["localStorage"][:64]
                    },
                )
            for page in self.context.pages:
                if storage_origin(page.url, self.allow_hosts) is None:
                    continue
                raw: Any = await page.evaluate(_SESSION_STORAGE_JS)
                if not isinstance(raw, dict) or not isinstance(raw.get("origin"), str):
                    continue
                origin = storage_origin(raw["origin"], self.allow_hosts)
                values = raw.get("items")
                if origin is None or not isinstance(values, list):
                    continue
                entry = entries.setdefault(origin, SessionOriginStorage(origin=origin))
                entry.session_storage = {
                    value["name"]: SecretStr(value["value"])
                    for value in values[:64]
                    if isinstance(value, dict)
                    and isinstance(value.get("name"), str)
                    and isinstance(value.get("value"), str)
                }
        except PlaywrightError:
            # Playwright can quote a storage value or init script in its error details.
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Не удалось сохранить состояние браузера"
            ) from None
        return bounded_browser_storage(entries.values(), self.allow_hosts)

    # -- lifetime ----------------------------------------------------------------------------

    async def close(self) -> None:
        if self._closing is None:
            self.closed = True
            if self._reaper is not None and self._reaper is not asyncio.current_task():
                self._reaper.cancel()
            for task in list(self._samples | self._auth_captures):
                task.cancel()
            self._closing = asyncio.create_task(self._finish_close())
        # A fetch deadline may stop waiting for cleanup. The context and its pool slot still
        # belong to this one bounded task; later callers and gateway shutdown wait for it too.
        await asyncio.shield(self._closing)

    async def _finish_close(self) -> None:
        try:
            with contextlib.suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(2):
                    await self._read_capability_registry.aclose()
                    await asyncio.gather(*self._retired_capability_tasks, return_exceptions=True)
            with contextlib.suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(10):
                    await self.context.close()
        finally:
            self._gateway.release(self)
            for callback in self._close_callbacks:
                with contextlib.suppress(Exception):
                    callback()
            self._close_callbacks.clear()

    def start_reaper(self, lifetime_s: float) -> None:
        async def reap() -> None:
            await asyncio.sleep(lifetime_s)
            log.info("browser context for connection %s reached its lifetime", self.connection_id)
            await self.close()

        self._reaper = asyncio.get_running_loop().create_task(reap())


class PlaywrightGateway:
    """Implements core.browser.BrowserGateway on Chromium."""

    def __init__(
        self,
        egress: Egress,
        *,
        headless: bool = True,
        sandbox: bool | None = None,
        ignore_https_errors: bool = False,
        max_contexts: int = 6,
        lifetime_s: float = HOLD_MAX_S,
        tmp_dir: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._egress = egress
        self._headless = headless
        self._sandbox = sandbox if sandbox is not None else sandbox_default()
        self._ignore_https_errors = ignore_https_errors  # tests only: fixture certificates
        self._slots = asyncio.Semaphore(max_contexts)
        self._lifetime_s = lifetime_s
        self._tmp_dir = tmp_dir or (
            Path(os.environ["BOTAI_BROWSER_TMPDIR"])
            if os.environ.get("BOTAI_BROWSER_TMPDIR")
            else None
        )
        self._clock = clock or SystemClock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()
        self._by_user: dict[str, PlaywrightHandle] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._handles: set[PlaywrightHandle] = set()
        self._background: set[asyncio.Task[None]] = set()

    # -- browser process ---------------------------------------------------------------------

    async def _ensure_browser(self, proxy_server: str) -> Browser:
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await self._start_playwright()
            # With a proxy the browser needs no DNS of its own: only the proxy host resolves.
            proxy_host = urlsplit(proxy_server).hostname or ""
            resolver_rules = f"MAP * ~NOTFOUND, EXCLUDE {proxy_host}"
            log.info("starting Chromium (sandbox %s)", "on" if self._sandbox else "off")
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                chromium_sandbox=self._sandbox,
                proxy={"server": "http://per-context"},
                args=[
                    f"--host-resolver-rules={resolver_rules}",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--disable-breakpad",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate,MediaRouter,OptimizationHints",
                ],
            )
            self._browser.on("disconnected", self._on_disconnected)
            return self._browser

    async def _start_playwright(self) -> Playwright:
        """Start the driver. Its temporary directory (Chromium's profile) goes to `tmp_dir`,
        which a deployment points at tmpfs."""
        if self._tmp_dir is None:
            return await async_playwright().start()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        saved = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(self._tmp_dir)
        try:
            return await async_playwright().start()
        finally:
            if saved is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved

    def _on_disconnected(self, _browser: Browser) -> None:
        """Chromium died or was closed: every context in it is gone."""
        loop = asyncio.get_running_loop()
        for handle in list(self._handles):
            task = loop.create_task(handle.close())
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        for handle in list(self._handles):
            await handle.close()
        async with self._launch_lock:
            if self._browser is not None:
                with contextlib.suppress(PlaywrightError):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(PlaywrightError):
                    await self._playwright.stop()
                self._playwright = None

    # -- contexts ----------------------------------------------------------------------------

    def active_for(self, user_id: str) -> PlaywrightHandle | None:
        handle = self._by_user.get(user_id)
        return handle if handle is not None and not handle.closed else None

    def release(self, handle: PlaywrightHandle) -> None:
        """Called by a handle when it closes: forget it and free its slot."""
        self._handles.discard(handle)
        if self._by_user.get(handle.user_id) is handle:
            del self._by_user[handle.user_id]
        self._slots.release()

    async def open(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
        origin: str | None = None,
        capture: CaptureConfig | None = None,
        suffixes: Sequence[str] = (),
        read_only: bool = False,
        background: bool = False,
    ) -> PlaywrightHandle:
        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
            return await self._open_for_user(
                user_id=user_id,
                connection_id=connection_id,
                allow_hosts=allow_hosts,
                session=session,
                origin=origin,
                capture=capture,
                suffixes=suffixes,
                read_only=read_only,
                background=background,
            )

    async def _open_for_user(
        self,
        *,
        user_id: str,
        connection_id: str,
        allow_hosts: Iterable[str],
        session: SessionState | None = None,
        origin: str | None = None,
        capture: CaptureConfig | None = None,
        suffixes: Sequence[str] = (),
        read_only: bool = False,
        background: bool = False,
    ) -> PlaywrightHandle:
        hosts = tuple(dict.fromkeys(h.lower() for h in allow_hosts))
        if not hosts and not suffixes:
            raise ValueError("a browser context needs at least one allowed host")
        previous = self._by_user.get(user_id)
        if background and previous is not None and not previous.closed:
            raise BrowserBusy()
        if previous is not None:
            await previous.close()  # one live context per user
        try:
            async with asyncio.timeout(20):
                await self._slots.acquire()
        except TimeoutError as exc:
            raise BrowserBusy("Браузер сейчас занят, попробуйте через минуту") from exc
        context: BrowserContext | None = None
        try:
            credentials = await self._egress.credentials(
                hosts, ttl_s=LOGIN_TOKEN_TTL_S, suffixes=suffixes
            )
            browser = await self._ensure_browser(credentials.server)
            context = await browser.new_context(
                proxy={
                    "server": credentials.server,
                    "username": "botai",
                    "password": credentials.token,
                    "bypass": "<-loopback>",
                },
                ignore_https_errors=self._ignore_https_errors,
                accept_downloads=False,
                service_workers="block",
                permissions=[],
                locale="ru-RU",
                viewport={"width": 1024, "height": 768},
            )
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            primary = origin or (session.origin if session is not None else f"https://{hosts[0]}")
            injected_authorizations: dict[Request, str] = {}
            if session is not None:
                injected_authorizations = await self._restore(
                    context, session, allow_hosts=hosts, clock=self._clock
                )
            if read_only:
                # Install after restore so this guard runs before bearer injection.
                route_diagnostics = _browser_diagnostics(connection_id)

                async def read_only_request(route: Route) -> None:
                    await _read_only_request(route, diagnostic_facts=route_diagnostics)

                await context.route("**/*", read_only_request)
                await context.route_web_socket("**/*", _read_only_socket)
            page = await context.new_page()
        except BaseException:
            if context is not None:  # a half-built context would live on with its cookies
                with contextlib.suppress(Exception):
                    await context.close()
            self._slots.release()
            raise
        restored_bearers: dict[str, tuple[SecretStr, datetime | None]] = {}
        if session is not None:
            for scope in session.bearer_scopes:
                expiry = min(
                    (
                        value
                        for value in (scope.expires_hint, session.expires_hint)
                        if value is not None
                    ),
                    default=None,
                )
                restored_bearers[scope.origin] = (scope.token, expiry)
            audience = auth_origin(session.origin)
            if session.bearer is not None and audience is not None:
                restored_bearers.setdefault(audience, (session.bearer, session.expires_hint))
        handle = PlaywrightHandle(
            gateway=self,
            context=context,
            page=page,
            user_id=user_id,
            connection_id=connection_id,
            allow_hosts=hosts,
            origin=primary,
            capture=capture or CaptureConfig(),
            clock=self._clock,
            read_only=read_only,
            injected_authorizations=injected_authorizations,
            restored_bearers=restored_bearers,
            suffixes=suffixes,
            confirmed_entry_url=(
                session.entry_url
                if session is not None and auth_origin(primary) == auth_origin(session.origin)
                else None
            ),
        )
        self._handles.add(handle)
        self._by_user[user_id] = handle
        handle.start_reaper(self._lifetime_s)
        return handle

    @staticmethod
    async def _restore(
        context: BrowserContext,
        session: SessionState,
        *,
        allow_hosts: Iterable[str] | None = None,
        now: datetime | None = None,
        clock: Clock | None = None,
    ) -> dict[Request, str]:
        injected_authorizations: dict[Request, str] = {}
        read_now = clock.now if clock is not None else SystemClock().now
        if clock is None and now is not None:
            fixed_now = now

            def read_now() -> datetime:
                return fixed_now

        now = read_now()
        if session.expires_hint is not None and session.expires_hint <= now:
            return injected_authorizations
        expired_scopes = [
            scope
            for scope in session.bearer_scopes
            if bearer_expired(scope.token, now=now, expires_hint=scope.expires_hint)
        ]
        expired_origins = {scope.origin for scope in expired_scopes}
        expired_tokens = {scope.token.get_secret_value() for scope in expired_scopes}
        primary = auth_origin(session.origin)
        if session.bearer is not None and bearer_expired(session.bearer, now=now):
            if primary is not None:
                expired_origins.add(primary)
            expired_tokens.add(session.bearer.get_secret_value())
        hosts = (
            tuple(allow_hosts)
            if allow_hosts is not None
            else ((urlsplit(session.origin).hostname or ""),)
        )
        browser_storage = [
            entry
            for entry in bounded_browser_storage(session.browser_storage, hosts)
            if entry.origin not in expired_origins
        ]
        diagnostic_event(
            "browser.restore",
            cookie_count=len(session.cookies),
            bearer_present=session.bearer is not None,
            storage_restored=bool(browser_storage),
        )
        if browser_storage:
            try:
                await context.set_storage_state(
                    {
                        "cookies": [],
                        "origins": [
                            {
                                "origin": entry.origin,
                                "localStorage": [
                                    {"name": key, "value": value.get_secret_value()}
                                    for key, value in entry.local_storage.items()
                                ],
                            }
                            for entry in browser_storage
                        ],
                    }
                )
                session_values = {
                    entry.origin: {
                        key: value.get_secret_value()
                        for key, value in entry.session_storage.items()
                    }
                    for entry in browser_storage
                    if entry.session_storage
                }
                if session_values:
                    # Fixed program; the serialized state is data, never executable source.
                    await context.add_init_script(
                        _RESTORE_SESSION_STORAGE_JS.replace(
                            "__BOTAI_STORAGE_DATA__", json.dumps(json.dumps(session_values))
                        )
                    )
            except PlaywrightError:
                raise BotaiError(
                    ErrorCode.SOURCE_UNAVAILABLE, "Не удалось восстановить состояние браузера"
                ) from None
        if session.cookies:
            await context.add_cookies(
                [
                    {
                        "name": c.name,
                        "value": c.value.get_secret_value(),
                        "domain": c.domain,
                        "path": c.path,
                        "expires": c.expires if c.expires else -1,
                        "httpOnly": c.http_only,
                        "secure": c.secure,
                    }
                    for c in session.cookies
                    if c.value.get_secret_value() not in expired_tokens
                ]
            )
        scoped = {
            scope.origin: scope.token.get_secret_value()
            for scope in session.bearer_scopes
            if (urlsplit(scope.origin).hostname or "") in hosts
            and scope.origin not in expired_origins
        }
        scoped_hints = {scope.origin: scope.expires_hint for scope in session.bearer_scopes}
        if (
            session.bearer is not None
            and primary is not None
            and primary not in expired_origins
            and (urlsplit(primary).hostname or "") in hosts
        ):
            scoped.setdefault(primary, session.bearer.get_secret_value())
        if scoped:

            async def add_authorization(route: Route) -> None:
                headers = route.request.headers
                # The application may have refreshed its token since restoration. Its own
                # credentials take precedence over the saved fallback, including other schemes.
                audience = auth_origin(route.request.url) or ""
                bearer = scoped.get(audience)
                at = read_now()
                expired = (session.expires_hint is not None and session.expires_hint <= at) or (
                    bearer is not None
                    and bearer_expired(
                        SecretStr(bearer), now=at, expires_hint=scoped_hints.get(audience)
                    )
                )
                if "authorization" in headers or bearer is None or expired:
                    await route.continue_()
                else:
                    authorization = f"Bearer {bearer}"
                    # This is execution metadata, kept private. Without a matching completed
                    # response it cannot be accepted as evidence by the factory compiler.
                    injected_authorizations[route.request] = authorization
                    await route.continue_(headers={**headers, "authorization": authorization})

            await context.route("**/*", add_authorization)
        return injected_authorizations
