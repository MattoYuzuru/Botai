"""AssetGate: what a browser context may fetch from hosts outside its connection's allowlist.

An identity provider or a cabinet loads its style sheets, scripts, fonts and images from CDN and
static hosts that are not the login host. With exact-host allowlists such a page renders without CSS
and JS. The gate lets those *assets* through and nothing else (security.md §4.3):

* a request to a host outside `allow_hosts` (and outside the login-mode `suffixes`) passes only if it
  is a GET of a style sheet, script, font, image or media file, with no `Authorization` and no
  cookie of the source that the browser would send to that host;
* every other request (a document, XHR/fetch, a beacon, an event stream, a WebSocket, any non-GET)
  is refused here, and a refused document is reported like a proxy-refused one, so that the
  student is still asked about a login redirect;
* a passing request first *arms* its host at the EgressProxy (`Egress.arm_asset_host`). The proxy
  cannot see what a request is, so the browser vouches for the host and the proxy keeps the rest:
  public addresses only, DNS pinning, port 443, a cap of hosts per token. A host nobody armed,
  such as the target of a server redirect (Playwright's `route` does not see redirect hops), stays
  refused by the proxy.

Nothing here touches the session: the gate adds no cookie and no header, a host it admits never
becomes part of `allow_hosts`, and `PlaywrightHandle` leaves admitted hosts out of the hosts it
reports as visited.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Request, Route, WebSocketRoute
from playwright.async_api import Error as PlaywrightError

from botai.browser.proxy import is_ip_literal, normalize_host
from botai.core.diagnostics import diagnostic_event, url_facts

log = logging.getLogger(__name__)

ASSET_TYPES = frozenset({"stylesheet", "script", "font", "image", "media"})
# What `PlaywrightHandle` maps to an EGRESS_BLOCKED navigation error (an approval question).
NAVIGATION_REFUSED = "navigation_host_not_allowed"
MAX_REMEMBERED = 64  # refused documents / hosts kept for the handle to read
MAX_ARM_FAILURES = 8  # a proxy that does not answer ARM must not slow every asset of a page
_LOGGED_TYPES = frozenset(
    {"xhr", "fetch", "document", "stylesheet", "script", "font", "image", "media", "websocket"}
)
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})

Arm = Callable[[str], Awaitable[bool]]


def host_of(url: str) -> str:
    """Lower-case host name of a URL without trailing dot, or '' when it has none."""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def cookie_reaches(domain: str, host: str) -> bool:
    """Would a browser send a cookie with this `domain` attribute to `host`?

    A leading dot marks a domain cookie (subdomains included); without it the cookie is host-only."""
    bare = domain.lstrip(".").lower()
    if domain.startswith("."):
        return host == bare or host.endswith("." + bare)
    return host == bare


def _foreign_pattern(
    schemes: str, allow: Iterable[str], suffixes: Iterable[str]
) -> re.Pattern[str]:
    """URLs of the given schemes whose host is none of the connection's. A pattern, not a
    callable, so that Playwright hands the page's requests to the allowed hosts by without a round
    trip to Python, and so that WebSockets to the allowed hosts are left untouched."""
    names = [re.escape(h) for h in sorted(allow)]
    names += [r"[^/:@?#]+\." + re.escape(s) for s in suffixes]
    return re.compile(
        rf"^(?:{schemes})://(?!(?:{'|'.join(names)})(?::\d*)?(?:[/?#]|$))", re.IGNORECASE
    )


def _tail(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def asset_refusal(method: str, resource_type: str, headers: Mapping[str, str]) -> str | None:
    """Why a request to a foreign host is not a page asset, or None when it may be one."""
    if method != "GET":
        return "asset_method"
    if resource_type not in ASSET_TYPES:
        return "asset_type"
    if "authorization" in {name.lower() for name in headers}:
        return "asset_credentials"
    return None


class AssetGate:
    def __init__(
        self,
        *,
        context: BrowserContext,
        allow_hosts: Sequence[str],
        suffixes: Sequence[str],
        cap: int,
        arm: Arm,
        diagnostic_facts: Mapping[str, object],
    ) -> None:
        self._context = context
        self._allow = frozenset(h.lower() for h in allow_hosts)
        self._suffixes = tuple(s.lower() for s in suffixes)
        self.http_pattern = _foreign_pattern("https?", self._allow, self._suffixes)
        self.socket_pattern = _foreign_pattern("wss?", self._allow, self._suffixes)
        self._cap = cap
        self._arm = arm
        self._facts = dict(diagnostic_facts)
        self._lock = asyncio.Lock()
        self._arm_failures = 0
        self._full = False  # the cap is spent: no more new hosts, nothing to ask the proxy
        self.hosts: dict[str, None] = {}  # admitted asset hosts, first-admitted order
        self.refused: list[str] = []  # hosts of refused requests (bounded)
        self.refused_navigations: dict[Request, tuple[str, str]] = {}
        self.counts: Counter[str] = Counter()
        self.allowed_requests = 0

    # -- which requests are the gate's business: `http_pattern` and `socket_pattern` ------------

    def local(self, host: str) -> bool:
        """A host the connection logs in at (or, on a first login, a subdomain it may follow)."""
        return host in self._allow or any(host.endswith("." + s) for s in self._suffixes)

    # -- the decision ------------------------------------------------------------------------

    async def route(self, route: Route) -> None:
        request = route.request
        host = host_of(request.url)
        if self.local(host):  # an odd spelling of the URL (userinfo, trailing dot) of our own host
            await route.fallback()
            return
        reason = asset_refusal(request.method, request.resource_type, request.headers)
        if reason is None:
            reason = await self._credential_refusal(host)
        if reason is None:
            reason = await self._admit(host, request)
        if reason is not None:
            self._refuse(request, host, reason)
            with contextlib.suppress(PlaywrightError):
                await route.abort("blockedbyclient")
            return
        self.allowed_requests += 1
        with contextlib.suppress(PlaywrightError):
            await route.fallback()  # on to the rest of the chain (bearer injection)

    def take_refused_navigation(self, request: Request) -> tuple[str, str] | None:
        return self.refused_navigations.pop(request, None)

    async def close_socket(self, socket: WebSocketRoute) -> None:
        host = host_of(socket.url)
        if self.local(host):
            with contextlib.suppress(PlaywrightError):
                socket.connect_to_server()
            return
        self.counts["asset_type"] += 1
        self._remember_refused(host)
        self._event(
            "browser.asset_refused",
            reason="asset_type",
            resource_type="websocket",
            navigation=False,
            **url_facts(socket.url),
        )
        with contextlib.suppress(PlaywrightError):
            await socket.close(code=1008, reason="asset host")

    async def _credential_refusal(self, host: str) -> str | None:
        """The browser sends cookies by domain. A sibling of a login host (same registered domain)
        may be inside the Domain= of a cookie of the source: then the request would carry the
        session to a host that is not the connection's, and it is refused."""
        if not any(_tail(host) == _tail(allowed) for allowed in self._allow):
            return None  # no cookie of the source can reach a host with another registered domain
        try:
            cookies = await self._context.cookies()
        except PlaywrightError:
            return "asset_unavailable"
        for cookie in cookies:
            domain = cookie.get("domain")
            if (
                isinstance(domain, str)
                and cookie_reaches(domain, host)
                and any(cookie_reaches(domain, allowed) for allowed in self._allow)
            ):
                return "asset_credentials"
        return None

    async def _admit(self, host: str, request: Request) -> str | None:
        if host in self.hosts:
            return None
        normalized = normalize_host(host)
        if normalized is None or is_ip_literal(normalized):
            return "asset_unavailable"
        async with self._lock:
            if host in self.hosts:
                return None
            if self._full or len(self.hosts) >= self._cap:
                self._full = True
                return "asset_cap"
            if self._arm_failures >= MAX_ARM_FAILURES:
                return "asset_unavailable"
            try:
                armed = await self._arm(normalized)
            except Exception:
                log.exception("arming an asset host failed")
                armed = False
            if not armed:
                self._arm_failures += 1
                return "asset_unavailable"
            self.hosts[host] = None
        self._event(
            "browser.asset_host_allowed",
            resource_type=self._type_code(request.resource_type),
            asset_host_count=len(self.hosts),
            asset_host_cap=self._cap,
            **url_facts(request.url),
        )
        return None

    # -- bookkeeping -------------------------------------------------------------------------

    def _refuse(self, request: Request, host: str, reason: str) -> None:
        self.counts[reason] += 1
        self._remember_refused(host)
        navigation = request.is_navigation_request()
        if navigation and len(self.refused_navigations) < MAX_REMEMBERED:
            self.refused_navigations[request] = (NAVIGATION_REFUSED, host)
        method = request.method if request.method in _METHODS else "OTHER"
        self._event(
            "browser.asset_refused",
            reason=reason,
            method=method,
            resource_type=self._type_code(request.resource_type),
            navigation=navigation,
            **url_facts(request.url),
        )

    def _remember_refused(self, host: str) -> None:
        if host and len(self.refused) < MAX_REMEMBERED:
            self.refused.append(host)

    @staticmethod
    def _type_code(resource_type: str) -> str:
        return resource_type if resource_type in _LOGGED_TYPES else "other"

    def _event(self, name: str, **facts: object) -> None:
        diagnostic_event(name, **{**facts, **self._facts})

    def summary(self) -> None:
        """One event when the context closes: how much of the page came from asset hosts."""
        if self.hosts or self.counts:
            self._event(
                "browser.assets",
                asset_host_count=len(self.hosts),
                asset_host_cap=self._cap,
                request_count=self.allowed_requests,
                rejected_count=sum(self.counts.values()),
            )
