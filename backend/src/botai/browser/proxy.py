"""EgressProxy: a filtering CONNECT proxy, the only network path of the university browser.

This is the security contour of security.md §4, not Playwright route interception (which does not
see server redirects). Every tunnel is authorised by a signed, short-lived token that carries the
host allowlist of one connection. The proxy refuses, before any DNS lookup where possible:

* methods other than CONNECT and ports other than 443;
* hosts outside the token's allowlist (exact names; an optional domain-suffix rule exists for the
  login mode only and is capped; and a small capped allowance for page-asset hosts that the
  browser's own guard vouches for with `ARM`, below);
* IP literals in any spelling;
* names resolving to private, loopback, link-local, CGNAT, multicast, reserved or transition
  (NAT64, 6to4, Teredo) addresses, and DNS rebinding (the address is resolved once, checked, and
  the very same address is connected to);
* more tunnels than the token's budget.

`ARM host:443` (same Basic credentials as CONNECT) admits one more public host for the page assets
of a browser context, within the token's `max_asset_hosts`. The proxy cannot tell a style sheet from
a document, so it does not decide what an asset is: the browser guard does, and arms a host only for
a GET of a style sheet, script, font, image or media. A host that nobody armed (a server redirect of
a document to a new site, which the browser guard never sees) stays refused, as before. Tokens of
API clients carry no allowance, so `ARM` is refused for them.

Only the host, the decision, the reason and the byte counts are logged: never a token, a path or a
body. The module imports nothing heavy so `python -m botai.browser.proxy` starts fast.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.logging import configure_logging

log = logging.getLogger("botai.browser.proxy")

ALLOWED_PORT = 443
TOKEN_VERSION = 1
MAX_HOSTS_PER_TOKEN = 12
# Extra public hosts one browser context may load page assets from (style sheets, scripts, fonts,
# images, media of an identity provider or a static host). Real login pages use 1 to 5 such hosts
# (Yandex ID: a static host and an analytics host; Google and Microsoft: 3 to 5 CDN names); 16 is
# three times the largest seen and small enough that a hostile page cannot reach far with it.
# `MAX_ASSET_HOSTS` is the ceiling any token may carry.
ASSET_HOSTS = 16
MAX_ASSET_HOSTS = 32
MAX_HEAD_BYTES = 16 * 1024
PROXY_USER = "botai"

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LOG_UNSAFE = re.compile(r"[^A-Za-z0-9.\-:\[\]]")
_NUMERIC_LABEL = re.compile(r"(\d+|0x[0-9a-f]*)")

Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]  # (pinned ip, port)


# --------------------------------------------------------------------------------------------
# Hosts and addresses
# --------------------------------------------------------------------------------------------


def normalize_host(raw: str) -> str | None:
    """Lower-case ASCII DNS name without trailing dot, or None when it is not a plain name."""
    host = raw.strip().lower().rstrip(".")
    if not host or len(host) > 253 or not host.isascii():
        return None
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return None
    return host


def is_ip_literal(host: str) -> bool:
    """True for IP literals in any spelling, including inet_aton shorthands (127.1, 0x7f.1).

    A real host name never ends in a purely numeric (or 0x…) label, so such names are refused too.
    """
    if not host:
        return True
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        return True
    if ":" in host or "[" in host:
        return True
    return bool(_NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]))


_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/8",  # unspecified, loopback, IPv4-compatible (::127.0.0.1) and SIIT forms
        "64:ff9b::/96",  # NAT64 well-known prefix: embeds an IPv4 address
        "64:ff9b:1::/48",  # NAT64 local-use
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
        "100::/64",  # discard-only
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",  # site-local, deprecated
        "ff00::/8",  # multicast
        "2001:10::/28",  # ORCHID
        "2001:20::/28",  # ORCHIDv2
        "5f00::/16",  # SRv6 segment identifiers
    )
)
_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, cloud metadata
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


def address_block_reason(address: str) -> str | None:
    """Why an address must not be connected to, or None when it is a public unicast address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "invalid_address"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return address_block_reason(str(ip.ipv4_mapped))
        if any(ip in net for net in _BLOCKED_V6):
            return "private_address"
    elif any(ip in net for net in _BLOCKED_V4):
        return "private_address"
    if not ip.is_global:
        return "private_address"
    return None


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.append(address)
    return seen


async def tcp_connector(
    address: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port)


# --------------------------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """What one connection token allows."""

    jti: str
    hosts: frozenset[str]
    suffixes: tuple[str, ...]
    expires_at: float
    max_connects: int
    max_dynamic_hosts: int
    max_asset_hosts: int = 0


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, body: str) -> str:
    return _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def derive_key(master: bytes, purpose: str) -> bytes:
    """Domain-separated subkey, so one master secret can serve proxy tokens and screen tickets."""
    return hmac.new(master, b"botai/" + purpose.encode(), hashlib.sha256).digest()


def mint_token(
    secret: bytes,
    *,
    hosts: Iterable[str],
    ttl_s: float,
    suffixes: Iterable[str] = (),
    max_connects: int = 300,
    max_dynamic_hosts: int = 10,
    max_asset_hosts: int = 0,
    now: float | None = None,
) -> str:
    """Create a connection token. `hosts` are exact names; `suffixes` (login mode only) admit
    subdomains of a domain, at most `max_dynamic_hosts` distinct hosts per token.
    `max_asset_hosts` (browser contexts only, never an API client) lets the holder `ARM` that many
    more public hosts for page assets."""
    clean: list[str] = []
    for raw in hosts:
        host = normalize_host(raw)
        if host is None or is_ip_literal(host):
            raise ValueError(f"not an allowable host name: {raw!r}")
        clean.append(host)
    domains: list[str] = []
    for raw in suffixes:
        domain = normalize_host(raw)
        if domain is None or is_ip_literal(domain) or "." not in domain:
            raise ValueError(f"not an allowable domain suffix: {raw!r}")
        domains.append(domain)
    if not clean and not domains:
        raise ValueError("a token needs at least one allowed host")
    if (
        not 0 < ttl_s <= 24 * 3600
        or not 1 <= max_connects <= 10_000
        or not 0 <= max_dynamic_hosts <= 50
        or not 0 <= max_asset_hosts <= MAX_ASSET_HOSTS
    ):
        raise ValueError("token lifetime or budget out of range")
    if len(set(clean)) > MAX_HOSTS_PER_TOKEN:
        raise ValueError(f"at most {MAX_HOSTS_PER_TOKEN} hosts per token")
    issued = time.time() if now is None else now
    payload = {
        "v": TOKEN_VERSION,
        "j": secrets.token_hex(6),
        "x": int(issued + ttl_s),
        "h": sorted(set(clean)),
        "s": sorted(set(domains)),
        "m": max_connects,
        "d": max_dynamic_hosts,
        "a": max_asset_hosts,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(secret, body)}"


def verify_token(secret: bytes, token: str, *, now: float | None = None) -> Grant | None:
    """The grant carried by a genuine, unexpired token; None for anything else."""
    if not token.isascii():
        return None
    body, dot, signature = token.partition(".")
    if not dot or not body or not signature or "." in signature:
        return None
    if not hmac.compare_digest(signature, _sign(secret, body)):
        return None
    try:
        payload = json.loads(_unb64(body))
        if payload["v"] != TOKEN_VERSION:
            return None
        grant = Grant(
            jti=str(payload["j"]),
            hosts=frozenset(str(h) for h in payload["h"]),
            suffixes=tuple(str(s) for s in payload["s"]),
            expires_at=float(payload["x"]),
            max_connects=int(payload["m"]),
            max_dynamic_hosts=int(payload["d"]),
            max_asset_hosts=int(payload.get("a", 0)),
        )
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if (time.time() if now is None else now) >= grant.expires_at:
        return None
    return grant


def proxy_url_with_token(server_url: str, token: str) -> str:
    """`http://botai:<token>@host:port`, the form httpx and Playwright accept."""
    scheme, _, rest = server_url.partition("://")
    return f"{scheme}://{PROXY_USER}:{quote(token, safe='')}@{rest}"


# --------------------------------------------------------------------------------------------
# The proxy
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Usage:
    expires_at: float
    connects: int = 0
    active: int = 0
    dynamic_hosts: set[str] = field(default_factory=lambda: set[str]())
    asset_hosts: set[str] = field(default_factory=lambda: set[str]())  # armed by the browser guard


@dataclass(frozen=True, slots=True)
class Denial:
    status: int
    reason: str


_STATUS_TEXT = {
    200: "Connection Established",
    204: "No Content",
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    407: "Proxy Authentication Required",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    504: "Gateway Timeout",
}


def _response(status: int, extra: Sequence[tuple[str, str]] = ()) -> bytes:
    lines = [f"HTTP/1.1 {status} {_STATUS_TEXT[status]}"]
    if status != 200:
        lines += ["Connection: close", "Content-Length: 0"]
    lines += [f"{k}: {v}" for k, v in extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class EgressProxy:
    """Asyncio CONNECT proxy. One instance serves many connections, each with its own token."""

    def __init__(
        self,
        secret: bytes,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        resolver: Resolver = system_resolver,
        connector: Connector = tcp_connector,
        test_hosts: Mapping[str, tuple[str, int]] | None = None,
        relays: Mapping[str, tuple[str, int]] | None = None,
        connect_timeout_s: float = 10.0,
        dns_timeout_s: float = 5.0,
        head_timeout_s: float = 10.0,
        idle_timeout_s: float = 120.0,
        max_tunnels: int = 512,
        max_tunnels_per_token: int = 32,
    ) -> None:
        self._secret = secret
        self._bind = (host, port)
        self._resolver = resolver
        self._connector = connector
        self._test_hosts: dict[str, tuple[str, int]] = {}
        for name, (ip, target_port) in (test_hosts or {}).items():
            normalized = normalize_host(name)
            if normalized is None or not ipaddress.ip_address(ip).is_loopback:
                raise ValueError("test hosts may only map names to loopback addresses")
            self._test_hosts[normalized] = (ip, target_port)
        # An operator's route for a host the server's own country cannot reach: the granted host
        # is reached through a relay inside the stack (an SSH forward to that host only). The relay
        # name resolves to a private address on purpose; it is configuration, never a request.
        self._relays: dict[str, tuple[str, int]] = {}
        for name, (relay, relay_port) in (relays or {}).items():
            normalized = normalize_host(name)
            if normalized is None or not relay or not 0 < relay_port < 65536:
                raise ValueError("a relay maps a host name to RELAY:PORT")
            self._relays[normalized] = (relay, relay_port)
        self._connect_timeout = connect_timeout_s
        self._dns_timeout = dns_timeout_s
        self._head_timeout = head_timeout_s
        self._idle_timeout = idle_timeout_s
        self._max_tunnels = max_tunnels
        self._max_per_token = max_tunnels_per_token
        self._usage: dict[str, _Usage] = {}
        self._revoked: dict[str, float] = {}
        self._reserved = 0  # tunnels reserved or open, all tokens together
        self._tunnels: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None
        self.stats: Counter[str] = Counter()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_client, self._bind[0], self._bind[1], limit=MAX_HEAD_BYTES
        )

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("proxy is not started")
        sockname = self._server.sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def mint(
        self,
        *,
        hosts: Iterable[str],
        ttl_s: float,
        suffixes: Iterable[str] = (),
        max_connects: int = 300,
        max_dynamic_hosts: int = 10,
        max_asset_hosts: int = 0,
    ) -> str:
        return mint_token(
            self._secret,
            hosts=hosts,
            ttl_s=ttl_s,
            suffixes=suffixes,
            max_connects=max_connects,
            max_dynamic_hosts=max_dynamic_hosts,
            max_asset_hosts=max_asset_hosts,
        )

    def arm_asset_host(self, token: str, host: str) -> Denial | None:
        """In-process twin of the `ARM` request: the same checks, for a proxy in this process."""
        grant = self._grant_from_token(token)
        if grant is None:
            return Denial(407, "auth_failed")
        normalized = normalize_host(host)
        if normalized is None:
            return Denial(400, "bad_request")
        if is_ip_literal(normalized):
            return Denial(403, "ip_literal")
        return self._arm(grant, normalized)

    def url_for(self, token: str) -> str:
        return proxy_url_with_token(self.url, token)

    def revoke(self, token: str) -> None:
        """Refuse this token from now on (until it would have expired anyway). For an in-process
        proxy: a context that is closed has no further use for its token."""
        grant = verify_token(self._secret, token)
        if grant is not None:
            self._revoked[grant.jti] = grant.expires_at

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # no new connections
        for writer in list(self._tunnels):
            writer.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    # -- policy ------------------------------------------------------------------------------

    def _usage_for(self, grant: Grant) -> _Usage:
        now = time.time()
        if len(self._usage) > 256:
            for key in [k for k, u in self._usage.items() if u.expires_at < now and u.active == 0]:
                del self._usage[key]
        usage = self._usage.get(grant.jti)
        if usage is None:
            usage = self._usage[grant.jti] = _Usage(expires_at=grant.expires_at)
        return usage

    def _reserve(self, grant: Grant, usage: _Usage, host: str) -> Denial | None:
        """Check the allowlist and the budgets and, in the same synchronous step, take the slot:
        parallel CONNECTs must not all pass the same check before any of them is counted."""
        armed = host in usage.asset_hosts
        dynamic = host not in grant.hosts and host not in usage.dynamic_hosts and not armed
        if dynamic:
            if not any(host.endswith("." + suffix) for suffix in grant.suffixes):
                return Denial(403, "not_allowlisted")
            if len(usage.dynamic_hosts) >= grant.max_dynamic_hosts:
                return Denial(403, "too_many_hosts")
        if (
            usage.connects >= grant.max_connects
            or usage.active >= self._max_per_token
            or self._reserved >= self._max_tunnels
        ):
            return Denial(429, "budget_exceeded")
        if dynamic:
            usage.dynamic_hosts.add(host)  # counted even if the name later fails to resolve
        usage.connects += 1
        usage.active += 1
        self._reserved += 1
        return None

    def _arm(self, grant: Grant, host: str) -> Denial | None:
        """Admit `host` as a page-asset host of this token. Public-only and DNS pinning are still
        decided per CONNECT; this only spends one of the token's asset slots (counted even if the
        name later fails to resolve, like a dynamic host)."""
        if grant.max_asset_hosts <= 0:
            return Denial(403, "asset_not_allowed")
        usage = self._usage_for(grant)
        if (
            host in grant.hosts
            or host in usage.asset_hosts
            or any(host.endswith("." + suffix) for suffix in grant.suffixes)
        ):
            return None  # already reachable: nothing to spend
        if len(usage.asset_hosts) >= grant.max_asset_hosts:
            return Denial(403, "too_many_hosts")
        usage.asset_hosts.add(host)
        return None

    def check_target(self, grant: Grant, authority: str) -> tuple[str, Denial | None]:
        """Pure part of the decision: returns the normalised host and a denial, if any."""
        host_part, _, port_part = authority.rpartition(":")
        if not host_part or not (
            port_part.isascii() and port_part.isdecimal() and len(port_part) <= 5
        ):
            return authority, Denial(400, "bad_request")
        lowered = host_part.lower().rstrip(".")
        if is_ip_literal(lowered):
            return lowered, Denial(403, "ip_literal")
        host = normalize_host(lowered)
        if host is None:
            return lowered, Denial(400, "bad_request")
        if int(port_part) != ALLOWED_PORT:
            return host, Denial(403, "port_not_allowed")
        return host, None

    async def _pin_address(self, host: str) -> tuple[tuple[str, int] | None, Denial | None]:
        """Resolve once, check every answer, return the address to connect to."""
        mapped = self._test_hosts.get(host)
        if mapped is not None:
            return mapped, None
        relay = self._relays.get(host)
        if relay is not None:
            relay_host, relay_port = relay
            try:
                async with asyncio.timeout(self._dns_timeout):
                    found = await self._resolver(relay_host, relay_port)
            except (OSError, TimeoutError):
                return None, Denial(502, "relay_unavailable")
            if not found:
                return None, Denial(502, "relay_unavailable")
            return (found[0], relay_port), None
        try:
            async with asyncio.timeout(self._dns_timeout):
                addresses = await self._resolver(host, ALLOWED_PORT)
        except (OSError, TimeoutError):
            return None, Denial(502, "dns_failure")
        if not addresses:
            return None, Denial(502, "dns_failure")
        for address in addresses:
            reason = address_block_reason(address)
            if reason is not None:
                return None, Denial(403, reason)
        return (addresses[0], ALLOWED_PORT), None

    # -- connection handling -----------------------------------------------------------------

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.get_running_loop().create_task(self._serve(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    def _grant_from_header(self, value: str | None) -> Grant | None:
        if not value:
            return None
        scheme, _, credentials = value.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        _user, sep, token = decoded.partition(":")
        if not sep:
            return None
        return self._grant_from_token(token)

    def _grant_from_token(self, token: str) -> Grant | None:
        grant = verify_token(self._secret, token)
        if grant is None:
            return None
        now = time.time()
        for jti in [j for j, expires in self._revoked.items() if expires < now]:
            del self._revoked[jti]
        return None if grant.jti in self._revoked else grant

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[str, dict[str, list[str]]]:
        async with asyncio.timeout(self._head_timeout):
            raw = await reader.readuntil(b"\r\n\r\n")
        text = raw.decode("latin-1")
        request_line, *header_lines = text.split("\r\n")
        headers: dict[str, list[str]] = {}
        for line in header_lines:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep:
                raise ValueError("malformed header")
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        return request_line, headers

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host_for_log = "-"
        decision = "deny"
        reason = "internal"
        status = 500
        up = down = 0
        usage: _Usage | None = None
        grant: Grant | None = None
        diagnostic_host: str | None = None
        try:
            try:
                request_line, headers = await self._read_head(reader)
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                TimeoutError,
                ValueError,
                ConnectionError,
            ):
                reason, status = "bad_request", 400
                await self._reply(writer, status)
                return
            parts = request_line.split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                reason, status = "bad_request", 400
                await self._reply(writer, status)
                return
            method, authority = parts[0].upper(), parts[1]
            auth_values = headers.get("proxy-authorization", [])
            grant = self._grant_from_header(auth_values[0]) if len(auth_values) == 1 else None
            if grant is None:
                reason, status = "auth_failed", 407
                await self._reply(
                    writer, status, [("Proxy-Authenticate", 'Basic realm="botai-egress"')]
                )
                return
            if method == "ARM":
                host, denial = self.check_target(grant, authority)
                diagnostic_host = host
                host_for_log = _LOG_UNSAFE.sub("?", host)[:100]
                if denial is None:
                    denial = self._arm(grant, host)
                if denial is not None:
                    reason, status = denial.reason, denial.status
                    await self._reply(
                        writer, status, [("X-Botai-Egress", f"denied; reason={reason}")]
                    )
                    return
                decision, reason, status = "allow", "asset_armed", 204
                await self._reply(writer, status)
                return
            if method != "CONNECT":
                reason, status = "method_not_allowed", 405
                await self._reply(writer, status, [("Allow", "CONNECT")])
                return
            host, denial = self.check_target(grant, authority)
            diagnostic_host = host
            host_for_log = _LOG_UNSAFE.sub("?", host)[:100]
            usage = self._usage_for(grant)
            if denial is None:
                denial = self._reserve(grant, usage, host)
            if denial is not None:
                reason, status = denial.reason, denial.status
                await self._reply(writer, status, [("X-Botai-Egress", f"denied; reason={reason}")])
                return
            tunnelled = False
            try:  # from here the slot is ours and goes back whatever happens
                target, denial = await self._pin_address(host)
                if denial is not None or target is None:
                    reason, status = (denial.reason, denial.status) if denial else ("internal", 500)
                    await self._reply(
                        writer, status, [("X-Botai-Egress", f"denied; reason={reason}")]
                    )
                    return
                try:
                    async with asyncio.timeout(self._connect_timeout):
                        up_reader, up_writer = await self._connector(*target)
                except TimeoutError:
                    reason, status = "connect_timeout", 504
                    await self._reply(writer, status)
                    return
                except OSError:
                    reason, status = "connect_failed", 502
                    await self._reply(writer, status)
                    return
                self._tunnels.update((writer, up_writer))
                try:
                    writer.write(_response(200))
                    await writer.drain()
                    decision, reason, status = "allow", "ok", 200
                    tunnelled = True
                    up, down = await self._pump(reader, writer, up_reader, up_writer)
                finally:
                    self._tunnels.discard(writer)
                    self._tunnels.discard(up_writer)
                    _close(up_writer)
            finally:
                usage.active -= 1
                self._reserved -= 1
                if not tunnelled:
                    usage.connects -= 1  # nothing was tunnelled: the connect budget is not spent
        except ConnectionError:
            if decision != "allow":
                reason = "connection_lost"
        except Exception:
            # a hostile or broken request must cost one connection, never a traceback per request
            log.exception("egress proxy: unexpected error while serving a request")
            if decision != "allow":
                reason, status = "internal", 500
                await self._reply(writer, status)
        finally:
            self.stats[f"{decision}:{reason}"] += 1
            facts: dict[str, object] = {}
            if grant is not None:
                facts["grant_hash"] = hashlib.sha256(grant.jti.encode()).hexdigest()[:16]
            if diagnostic_host is not None:
                host_hash = url_facts("https://" + diagnostic_host).get("host_hash")
                if host_hash is not None:
                    facts["host_hash"] = host_hash
            diagnostic_event(
                "egress.decision",
                # An embedded server may inherit the starter job's context. A tunnel belongs
                # to its verified grant, never to that old task; the reader joins the digest.
                job_id=None,
                thread_id=None,
                connection_id=None,
                request_id=None,
                message_id=None,
                block_id=None,
                job_kind=None,
                decision=decision,
                reason=reason,
                status=status,
                up_bytes=up,
                down_bytes=down,
                **facts,
            )
            log.info(
                "egress host=%s decision=%s reason=%s status=%d up=%d down=%d",
                host_for_log,
                decision,
                reason,
                status,
                up,
                down,
            )
            _close(writer)

    async def _reply(
        self, writer: asyncio.StreamWriter, status: int, extra: Sequence[tuple[str, str]] = ()
    ) -> None:
        with contextlib.suppress(ConnectionError):
            writer.write(_response(status, extra))
            await writer.drain()

    async def _pump(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        counters = [0, 0]
        activity = [time.monotonic()]  # one clock per tunnel: either direction keeps it alive

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, index: int) -> None:
            try:
                while True:
                    try:
                        async with asyncio.timeout(self._idle_timeout):
                            chunk = await src.read(65536)
                    except TimeoutError:
                        if time.monotonic() - activity[0] < self._idle_timeout:
                            continue
                        raise
                    if not chunk:
                        break
                    activity[0] = time.monotonic()
                    counters[index] += len(chunk)
                    dst.write(chunk)
                    async with asyncio.timeout(self._idle_timeout):  # a peer that never reads
                        await dst.drain()
            except (ConnectionError, TimeoutError, OSError):
                _close(dst)
                return
            with contextlib.suppress(OSError, RuntimeError):
                if dst.can_write_eof():
                    dst.write_eof()

        await asyncio.gather(copy(client_reader, up_writer, 0), copy(up_reader, client_writer, 1))
        return counters[0], counters[1]


def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()


# --------------------------------------------------------------------------------------------
# Command line: python -m botai.browser.proxy
# --------------------------------------------------------------------------------------------


def _parse_test_host(value: str) -> tuple[str, tuple[str, int]]:
    name, _, target = value.partition("=")
    ip, _, port = target.rpartition(":")
    if not name or not ip or not port.isdigit():
        raise argparse.ArgumentTypeError("expected NAME=IP:PORT")
    return name, (ip, int(port))


def _parse_relay(value: str) -> tuple[str, tuple[str, int]]:
    name, _, target = value.partition("=")
    relay, _, port = target.rpartition(":")
    if not name or not relay or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST=RELAY:PORT")
    return name, (relay, int(port))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m botai.browser.proxy", description=__doc__)
    parser.add_argument(
        "--listen", default="127.0.0.1:3128", help="HOST:PORT (loopback by default)"
    )
    parser.add_argument(
        "--test-host",
        action="append",
        default=[],
        type=_parse_test_host,
        metavar="NAME=IP:PORT",
        help="map NAME:443 to a loopback address (tests and local fixtures only)",
    )
    parser.add_argument(
        "--relay",
        action="append",
        default=[],
        type=_parse_relay,
        metavar="HOST=RELAY:PORT",
        help="reach a granted HOST:443 through an operator's relay (also BOTAI_EGRESS_RELAY)",
    )
    args = parser.parse_args(argv)
    configure_logging()
    relays = dict(args.relay)
    for item in filter(None, os.environ.get("BOTAI_EGRESS_RELAY", "").split(",")):
        name, target = _parse_relay(item.strip())
        relays[name] = target
    master = os.environ.get("BOTAI_PROXY_SECRET", "")
    if len(master) < 32:
        print("BOTAI_PROXY_SECRET must be set to at least 32 characters", file=sys.stderr)
        return 2
    if args.test_host and os.environ.get("BOTAI_ENV", "") not in ("dev", "test"):
        print("--test-host needs BOTAI_ENV=dev or BOTAI_ENV=test", file=sys.stderr)
        return 2
    host, _, port = args.listen.rpartition(":")
    proxy = EgressProxy(
        derive_key(master.encode(), "proxy-token"),
        host=host or "127.0.0.1",
        port=int(port),
        test_hosts=dict(args.test_host),
        relays=relays,
    )

    async def run() -> None:
        await proxy.start()
        log.info("egress proxy listening on %s:%d", *proxy.address)
        try:
            await asyncio.Event().wait()
        finally:
            await proxy.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
