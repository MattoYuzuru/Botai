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
import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import SecretStr

from botai.browser.egress import LOGIN_TOKEN_TTL_S, Egress
from botai.browser.pagescan import SCAN_JS, build_snapshot
from botai.browser.session import jwt_expiry
from botai.core.browser import ObservedRequest, PageSnapshot, SessionCookie, SessionState
from botai.core.clock import Clock, SystemClock
from botai.core.errors import BotaiError, ErrorCode

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_MS = 3_000
MAX_LOG = 500
SAMPLE_CHARS = 4_000
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


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """What `export_session` takes from the browser (login-preset `capture`)."""

    cookies_for: tuple[str, ...] | None = None  # default: every allowed host
    bearer_from: BearerFrom = "authorizationHeader"
    bearer_key: str | None = None


def scrub_sample(text: str) -> str | None:
    """A response sample safe to hand to the learner: nothing that looks like a credential."""
    if _TOKEN_KEYS.search(text):
        return None
    return _JWT.sub("[jwt]", text)[:SAMPLE_CHARS]


def map_navigation_error(
    exc: PlaywrightError, requested: str, refused_host: str | None = None
) -> BotaiError:
    """`refused_host` is the host the proxy turned away during this navigation, if any: after a
    redirect it differs from the URL Playwright reports."""
    message = str(exc)
    match = _NAV_URL.search(message)
    host = refused_host or urlsplit(match.group(1) if match else requested).hostname or "?"
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
    ) -> None:
        self._gateway = gateway
        self.context = context
        self._page = page
        self.user_id = user_id
        self.connection_id = connection_id
        self.allow_hosts = tuple(allow_hosts)
        self.origin = origin
        self._capture = capture
        self._clock = clock
        self._log: list[ObservedRequest] = []
        self._entries: dict[Request, ObservedRequest] = {}
        self._samples: set[asyncio.Task[None]] = set()
        self._bearer: dict[str, str] = {}  # host -> last Authorization the browser sent
        self._visited: dict[str, None] = {}
        self._blocked: list[str] = []  # one entry per refused tunnel, oldest first
        self._close_callbacks: list[Callable[[], None]] = []
        self.closed = False
        self._reaper: asyncio.Task[None] | None = None
        context.on("page", self._on_page)
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_failed)

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

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def _on_page(self, page: Page) -> None:
        self._page = page

    def _on_request(self, request: Request) -> None:
        host = urlsplit(request.url).hostname
        if host:
            self._visited.setdefault(host.lower(), None)
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer ") and host.lower() in self.allow_hosts:
                self._bearer[host.lower()] = auth[7:].strip()
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

    def _on_response(self, response: Response) -> None:
        entry = self._entries.get(response.request)
        if entry is None:
            return
        entry.status = response.status
        content_type = response.headers.get("content-type")
        entry.response_content_type = content_type
        if (
            response.request.resource_type in ("xhr", "fetch")
            and content_type
            and _TEXTUAL.search(content_type)
        ):
            task = asyncio.get_running_loop().create_task(self._sample(response, entry))
            self._samples.add(task)
            task.add_done_callback(self._samples.discard)

    async def _sample(self, response: Response, entry: ObservedRequest) -> None:
        with contextlib.suppress(PlaywrightError, UnicodeError):
            body = await response.body()
            entry.response_sample = scrub_sample(
                body[: SAMPLE_CHARS * 4].decode("utf-8", "replace")
            )

    def _on_failed(self, request: Request) -> None:
        failure = request.failure or ""
        if "TUNNEL" in failure or "PROXY" in failure:
            host = urlsplit(request.url).hostname
            if host:
                self._blocked.append(host.lower())

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
        self._check_url(url)
        refused_before = len(self._blocked)
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Сайт вуза не отвечает") from exc
        except PlaywrightError as exc:
            await asyncio.sleep(0.1)  # let the `requestfailed` event for the refused tunnel land
            refused = self._blocked[refused_before:]
            raise map_navigation_error(exc, url, refused[-1] if refused else None) from exc
        await self._settle()
        return await self.snapshot()

    async def snapshot(self) -> PageSnapshot:
        last: PlaywrightError | None = None
        for _ in range(4):
            page = self._page
            try:
                raw: Any = await page.evaluate(SCAN_JS)
                return build_snapshot(page.url, await page.title(), raw)
            except PlaywrightError as exc:  # navigation in flight destroys the execution context
                last = exc
                await asyncio.sleep(0.15)
                await self._settle(1_500)
        raise BotaiError(
            ErrorCode.SOURCE_UNAVAILABLE, "Страница вуза недоступна для чтения"
        ) from last

    async def fill(self, selector: str, value: str) -> None:
        try:
            await self._page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            # the message names the selector, never the value
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Поле на странице вуза не найдено"
            ) from exc

    async def click(self, selector: str) -> PageSnapshot:
        try:
            await self._page.click(selector, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Кнопка на странице вуза не найдена"
            ) from exc
        await self._settle()
        return await self.snapshot()

    async def press(self, selector: str, key: str) -> PageSnapshot:
        try:
            await self._page.press(selector, key, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Поле на странице вуза не найдено"
            ) from exc
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

    async def export_session(self) -> SessionState:
        """Cookies of the capture hosts plus a bearer the browser itself sent (or the single
        explicitly named storage key). Arbitrary tokens in storage are never collected."""
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
            bearer = self._bearer.get(primary) or next(iter(reversed(self._bearer.values())), None)
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
        return SessionState(
            origin=self.origin,
            cookies=cookies,
            bearer=SecretStr(bearer) if bearer else None,
            captured_at=now,
            expires_hint=hint,
        )

    # -- lifetime ----------------------------------------------------------------------------

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._reaper is not None and self._reaper is not asyncio.current_task():
            self._reaper.cancel()
        for task in list(self._samples):
            task.cancel()
        with contextlib.suppress(PlaywrightError, TimeoutError):
            async with asyncio.timeout(10):
                await self.context.close()
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
        # Chromium's own sandbox is on unless the deployment says it runs in a container
        self._sandbox = (
            sandbox if sandbox is not None else os.environ.get("BOTAI_BROWSER_SANDBOX", "1") != "0"
        )
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
        self._handles: set[PlaywrightHandle] = set()
        self._background: set[asyncio.Task[None]] = set()

    # -- browser process ---------------------------------------------------------------------

    async def _ensure_browser(self, proxy_server: str) -> Browser:
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            # With a proxy the browser needs no DNS of its own: only the proxy host resolves.
            proxy_host = urlsplit(proxy_server).hostname or ""
            resolver_rules = f"MAP * ~NOTFOUND, EXCLUDE {proxy_host}"
            env: dict[str, str | float | bool] | None = None
            if self._tmp_dir is not None:
                self._tmp_dir.mkdir(parents=True, exist_ok=True)
                env = {**os.environ, "TMPDIR": str(self._tmp_dir)}
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                chromium_sandbox=self._sandbox,
                proxy={"server": "http://per-context"},
                env=env,
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
    ) -> PlaywrightHandle:
        hosts = tuple(dict.fromkeys(h.lower() for h in allow_hosts))
        if not hosts and not suffixes:
            raise ValueError("a browser context needs at least one allowed host")
        previous = self._by_user.get(user_id)
        if previous is not None:
            await previous.close()  # one live context per user
        try:
            async with asyncio.timeout(20):
                await self._slots.acquire()
        except TimeoutError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Браузер сейчас занят, попробуйте через минуту"
            ) from exc
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
                await self._restore(context, session)
            page = await context.new_page()
        except BaseException:
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
        )
        self._handles.add(handle)
        self._by_user[user_id] = handle
        handle.start_reaper(self._lifetime_s)
        return handle

    @staticmethod
    async def _restore(context: BrowserContext, session: SessionState) -> None:
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
