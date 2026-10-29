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
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

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
from botai.browser.read_views import ReadView, ReadViewRegistry
from botai.browser.session import (
    MAX_STORAGE_BYTES,
    MAX_STORAGE_ENTRIES,
    MAX_STORAGE_KEY_CHARS,
    MAX_STORAGE_ORIGINS,
    MAX_STORAGE_VALUE_CHARS,
    bounded_browser_storage,
    jwt_expiry,
    storage_origin,
)
from botai.core.browser import (
    ObservedRequest,
    PageSnapshot,
    SessionCookie,
    SessionOriginStorage,
    SessionState,
)
from botai.core.clock import Clock, SystemClock
from botai.core.diagnostics import diagnostic_context, diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_MS = 3_000
READ_VIEW_QUIET_S = 0.2
MAX_LOG = 500
AUTH_EVALUATE_S = 0.5
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


@dataclass(frozen=True, slots=True, repr=False)
class _AuthPermit:
    page: Page
    epoch: int
    url: str
    actions: frozenset[str]


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


def map_navigation_error(exc: PlaywrightError, requested: str) -> BotaiError:
    """Classify a main-frame failure without exposing its URL parameters."""
    message = str(exc)
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
    ) -> None:
        self._gateway = gateway
        self.context = context
        self._page = page
        self.user_id = user_id
        self.connection_id = connection_id
        self._diagnostic_facts = _browser_diagnostics(connection_id)
        self._read_view_registry = ReadViewRegistry()
        self.allow_hosts = tuple(allow_hosts)
        self.origin = origin
        self._capture = capture
        self._clock = clock
        self.read_only = read_only
        self._entry_guarded = False
        self._entry_verifying = 0
        self._auth_epochs: dict[Frame, int] = {}
        self._auth_permits: dict[Frame, _AuthPermit] = {}
        self._log: list[ObservedRequest] = []
        self._entries: dict[Request, ObservedRequest] = {}
        self._samples: set[asyncio.Task[None]] = set()
        self._inflight: set[Request] = set()
        self._last_activity = monotonic()
        self._activity_generation = 0
        self._sampled = 0
        self._failed_diagnostics = 0
        self._bearer: dict[str, str] = {}  # host -> last Authorization the browser sent
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
            self._auth_permits.clear()
        else:
            self._auth_permits.pop(frame, None)

    def _forget_auth_document(self, frame: Frame) -> None:
        self._auth_permits.pop(frame, None)
        self._auth_epochs.pop(frame, None)

    async def _entry_auth_allowed(self, request: Request) -> bool:
        if self.read_only or self._entry_verifying:
            return False
        try:
            frame = request.frame
            permit = self._auth_permits.pop(frame, None)
            if (
                permit is None
                or frame.page != self._page
                or self._page != permit.page
                or self._auth_epochs.get(frame, 0) != permit.epoch
                or frame.url != permit.url
            ):
                return False
            if request.is_navigation_request():
                # Never inspect a renderer suspended by this document POST.
                return request.url in permit.actions
            # GET polling can remove auth UI without changing a SPA's URL.
            # No snapshot/title/navigation waits or retries inside interception.
            async with asyncio.timeout(AUTH_EVALUATE_S):
                raw: Any = await frame.evaluate(_AUTH_SCAN_JS)
            snap = build_snapshot(permit.url, str(raw.get("title", "")), raw)
            return (
                self._page == permit.page == frame.page
                and self._auth_epochs.get(frame, 0) == permit.epoch
                and frame.url == permit.url
                and (
                    has_credential_fields(snap) or isinstance(detect(snap), CaptchaPage | PushWait)
                )
            )
        except (PlaywrightError, TimeoutError):
            return False

    async def _arm_auth_document(self, frame: Frame) -> _AuthPermit | None:
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
                or not (
                    has_credential_fields(snap) or isinstance(detect(snap), CaptchaPage | PushWait)
                )
            ):
                return None
        except (PlaywrightError, TimeoutError):
            return None
        actions = frozenset(
            urlsplit(urljoin(url, form.action or url))._replace(fragment="").geturl()
            for form in snap.forms
            if form.method.lower() == "post"
            and has_credential_fields(PageSnapshot(url=url, title=snap.title, forms=[form]))
        )
        grant = _AuthPermit(page, epoch, url, actions)
        self._auth_permits[frame] = grant
        return grant

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
        self._auth_permits.clear()
        try:
            yield
        finally:
            self._entry_verifying -= 1

    @contextlib.asynccontextmanager
    async def auth_document(self) -> AsyncGenerator[None]:
        """One native auth POST; never inspect a paused navigation from its route handler."""
        frame = self._page.main_frame
        grant = await self._arm_auth_document(frame) if self._entry_guarded else None
        deadline = monotonic() + SETTLE_MS / 1000
        try:
            yield
            if self._entry_guarded:
                while self._inflight or monotonic() - self._last_activity < READ_VIEW_QUIET_S:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise BotaiError(
                            ErrorCode.SOURCE_UNAVAILABLE, "Страница входа не завершила загрузку"
                        )
                    await asyncio.sleep(min(0.02, remaining))
        finally:
            permit = self._auth_permits.get(frame)
            if permit is grant:
                self._auth_permits.pop(frame, None)

    async def arm_human_auth(self) -> None:
        """Before a real screen input, grant only recognized auth frames of the selected page."""
        if not self._entry_guarded or self._entry_verifying:
            return
        self._auth_permits.clear()
        try:
            async with asyncio.timeout(1.0):
                for frame in self._page.frames[:32]:
                    await self._arm_auth_document(frame)
        except TimeoutError:
            self._auth_permits.clear()

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def on_page_change(self, callback: Callable[[], None]) -> None:
        """Called when a popup or new tab becomes the current page."""
        self._page_callbacks.append(callback)

    def _on_page(self, page: Page) -> None:
        self._auth_permits.clear()
        page.on("framenavigated", self._revoke_auth_document)
        page.on("framedetached", self._forget_auth_document)
        self._read_view_registry.invalidate()
        self._page = page
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
        if host:
            self._visited.setdefault(host.lower(), None)
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer ") and host.lower() in self.allow_hosts:
                token = auth[7:].strip()
                self._bearer[host.lower()] = token
                if token not in self._observation_bearers:
                    try:
                        size = len(token.encode("utf-8"))
                    except UnicodeError:
                        self._secret_inventory_failed = True
                    else:
                        if (
                            len(self._observation_bearers) >= 4096
                            or self._observation_bearer_bytes + size > 1_048_576
                        ):
                            self._secret_inventory_failed = True
                        else:
                            self._observation_bearers.add(token)
                            self._observation_bearer_bytes += size
        if request.resource_type not in _OBSERVED_TYPES or len(self._log) >= MAX_LOG:
            return
        entry = ObservedRequest(
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
            request_content_type=request.headers.get("content-type"),
        )
        self._log.append(entry)
        self._entries[request] = entry
        self._diagnostic(
            "browser.request",
            connection_id=self.connection_id,
            method=_method(request.method),
            resource_type=request.resource_type,
            read_only=self.read_only,
            **url_facts(request.url),
        )

    def _on_response(self, response: Response) -> None:
        entry = self._entries.get(response.request)
        if entry is None:
            return
        entry.status = response.status
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
        if request is self._navigation_request:
            self._navigation_error = map_navigation_error(PlaywrightError(failure), request.url)
        if "TUNNEL" in failure or "PROXY" in failure:
            host = urlsplit(request.url).hostname
            if host:
                self._blocked.append(host.lower())

    def _on_finished(self, request: Request) -> None:
        if request in self._inflight:
            self._inflight.discard(request)
            self._last_activity = monotonic()
            self._activity_generation += 1

    async def network_log(self) -> list[ObservedRequest]:
        if self._samples:
            await asyncio.wait(list(self._samples), timeout=2.0)
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
            raise self._navigation_error or map_navigation_error(exc, url) from exc
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
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
        try:
            await self._page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            # `from None`: Playwright's call log can quote the value that was being typed
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Поле на странице вуза не найдено"
            ) from None

    async def read_views(self) -> list[ReadView]:
        if not self.read_only or self.closed:
            return []
        return await self._read_view_registry.discover(self._page)

    async def activate_read_view(self, ref: str) -> PageSnapshot:
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
        if self.read_only:
            raise BotaiError(ErrorCode.CONFLICT, "Этот браузер открыт только для чтения")
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
        source = self._capture.bearer_from
        if source == "authorizationHeader":
            primary = (urlsplit(self.origin).hostname or "").lower()
            # the origin's own; failing that, the one token the browser sent anywhere (an API
            # host next to the page host). Two different tokens: no guess.
            sent = set(self._bearer.values())
            bearer = self._bearer.get(primary) or (next(iter(sent)) if len(sent) == 1 else None)
        elif source in ("localStorage", "sessionStorage") and self._capture.bearer_key:
            bearer = await self._bearer_from_storage(source, self._capture.bearer_key)
        now = self._clock.now()
        expiries = [datetime.fromtimestamp(c.expires, UTC) for c in cookies if c.expires]
        hint: datetime | None = None
        if cookies and len(expiries) == len(cookies):
            hint = min(expiries)
        if bearer:
            exp = jwt_expiry(bearer)
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
            captured_at=now,
            expires_hint=hint,
            browser_storage=browser_storage,
        )

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
            for task in list(self._samples):
                task.cancel()
            self._closing = asyncio.create_task(self._finish_close())
        # A fetch deadline may stop waiting for cleanup. The context and its pool slot still
        # belong to this one bounded task; later callers and gateway shutdown wait for it too.
        await asyncio.shield(self._closing)

    async def _finish_close(self) -> None:
        try:
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
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "Браузер занят другим действием; чтение продолжится позже",
            )
        if previous is not None:
            await previous.close()  # one live context per user
        try:
            async with asyncio.timeout(20):
                await self._slots.acquire()
        except TimeoutError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Браузер сейчас занят, попробуйте через минуту"
            ) from exc
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
            primary = origin or f"https://{hosts[0]}"
            if session is not None:
                await self._restore(context, session, allow_hosts=hosts)
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
        )
        self._handles.add(handle)
        self._by_user[user_id] = handle
        handle.start_reaper(self._lifetime_s)
        return handle

    @staticmethod
    async def _restore(
        context: BrowserContext, session: SessionState, *, allow_hosts: Iterable[str] | None = None
    ) -> None:
        hosts = (
            tuple(allow_hosts)
            if allow_hosts is not None
            else ((urlsplit(session.origin).hostname or ""),)
        )
        browser_storage = bounded_browser_storage(session.browser_storage, hosts)
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
                ]
            )
        if session.bearer is not None:
            bearer = session.bearer.get_secret_value()
            primary = urlsplit(session.origin)
            pattern = f"{primary.scheme}://{primary.hostname}/**"

            async def add_authorization(route: Route) -> None:
                headers = {**route.request.headers, "authorization": f"Bearer {bearer}"}
                await route.continue_(headers=headers)

            await context.route(pattern, add_authorization)
