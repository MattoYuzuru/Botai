"""How the rest of the backend obtains a path through the EgressProxy.

* `Egress.credentials(hosts)` mints a short-lived token for one connection's allowlist. Only a
  browser context asks for `assets`: room for a few extra public hosts that its page assets come
  from. Tokens for `core.http` clients (API fetch, probes, search) never carry it.
* `Egress.arm_asset_host(token, host)` is how the browser guard spends one of those slots on a host
  it has just vouched for (a GET of a style sheet, script, font, image or media).
* With `EGRESS_PROXY_URL` set, the proxy is a separate process (`python -m botai.browser.proxy`)
  and the secret comes from `BOTAI_PROXY_SECRET`, the same value the proxy was started with.
* Without it, a proxy runs inside this process on a loopback port (development, tests, single-box
  installs). The browser and `core.http` clients still reach the network only through it.

Other modules (the sync Runner) call `proxy_url_for(...)` and hand the result to
`core.http.build_client(proxy_url=...)`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from botai.browser.proxy import (
    PROXY_USER,
    EgressProxy,
    derive_key,
    is_ip_literal,
    mint_token,
    normalize_host,
    proxy_url_with_token,
    verify_token,
)
from botai.core.diagnostics import diagnostic_event
from botai.settings import Settings

LOGIN_TOKEN_TTL_S = 35 * 60  # a little over the longest login session (events.md §3)


@dataclass(frozen=True, slots=True)
class ProxyCredentials:
    server: str  # http://host:port
    token: str

    @property
    def url(self) -> str:
        """`http://botai:<token>@host:port`, for httpx."""
        return proxy_url_with_token(self.server, self.token)


class Egress(Protocol):
    async def credentials(
        self,
        hosts: Sequence[str],
        *,
        ttl_s: float,
        suffixes: Sequence[str] = (),
        assets: int = 0,
    ) -> ProxyCredentials: ...

    async def arm_asset_host(self, token: str, host: str) -> bool:
        """Spend one of the token's asset slots on `host`; False when the proxy refuses."""
        ...


class ProxyEgress:
    """Mints tokens for an external proxy at `server_url`, or for `proxy` running in this process."""

    def __init__(
        self, secret: bytes, *, server_url: str | None = None, proxy: EgressProxy | None = None
    ) -> None:
        if (server_url is None) == (proxy is None):
            raise ValueError("give either server_url (external proxy) or proxy (in-process)")
        if server_url is not None:
            parts = urlsplit(server_url)
            if parts.scheme != "http" or not parts.hostname:
                # the proxy speaks plain HTTP CONNECT; Chromium would take `https://` as a TLS proxy
                raise ValueError("EGRESS_PROXY_URL must be http://host[:port]")
        self._secret = secret
        self._server_url = server_url
        self._proxy = proxy
        self._start_lock = asyncio.Lock()

    async def credentials(
        self,
        hosts: Sequence[str],
        *,
        ttl_s: float,
        suffixes: Sequence[str] = (),
        assets: int = 0,
    ) -> ProxyCredentials:
        token = mint_token(
            self._secret, hosts=hosts, ttl_s=ttl_s, suffixes=suffixes, max_asset_hosts=assets
        )
        grant = verify_token(self._secret, token)
        if grant is not None:
            diagnostic_event(
                "egress.grant",
                grant_hash=hashlib.sha256(grant.jti.encode()).hexdigest()[:16],
                allow_host_count=len(grant.hosts),
                **({"asset_host_cap": grant.max_asset_hosts} if grant.max_asset_hosts else {}),
            )
        if self._proxy is not None:
            async with self._start_lock:
                await self._proxy.start()
            return ProxyCredentials(self._proxy.url, token)
        assert self._server_url is not None
        return ProxyCredentials(self._server_url.rstrip("/"), token)

    async def arm_asset_host(self, token: str, host: str) -> bool:
        normalized = normalize_host(host)
        if normalized is None or is_ip_literal(normalized):
            return False
        if self._proxy is not None:
            return self._proxy.arm_asset_host(token, normalized) is None
        assert self._server_url is not None
        return await _arm_remote(self._server_url, token, normalized)

    async def aclose(self) -> None:
        if self._proxy is not None:
            await self._proxy.close()


async def _arm_remote(server_url: str, token: str, host: str, *, timeout_s: float = 2.0) -> bool:
    """`ARM host:443` against a proxy in another process: the answer is 204 or a refusal."""
    parts = urlsplit(server_url)
    if not parts.hostname:
        return False
    credentials = base64.b64encode(f"{PROXY_USER}:{token}".encode()).decode()
    request = (
        f"ARM {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
    )
    try:
        async with asyncio.timeout(timeout_s):
            # the port as Chromium takes it for a proxy `http://host`: 80
            reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 80)
            try:
                writer.write(request.encode("ascii"))
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
            finally:
                writer.close()
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return False
    status_line = head.split(b"\r\n", 1)[0].split(b" ")
    return len(status_line) >= 2 and status_line[1] == b"204"


_cached: ProxyEgress | None = None


def get_egress(settings: Settings) -> ProxyEgress:
    """The process-wide egress, built from settings on first use."""
    global _cached
    if _cached is None:
        master = os.environ.get("BOTAI_PROXY_SECRET", "")
        if settings.egress_proxy_url:
            if len(master) < 32:
                raise RuntimeError(
                    "EGRESS_PROXY_URL is set but BOTAI_PROXY_SECRET (>= 32 chars) is not"
                )
            _cached = ProxyEgress(
                derive_key(master.encode(), "proxy-token"), server_url=settings.egress_proxy_url
            )
        else:
            key = (
                derive_key(master.encode(), "proxy-token")
                if len(master) >= 32
                else secrets.token_bytes(32)
            )
            _cached = ProxyEgress(key, proxy=EgressProxy(key))
    return _cached


async def close_egress() -> None:
    global _cached
    if _cached is not None:
        await _cached.aclose()
        _cached = None


async def proxy_url_for(settings: Settings, hosts: Sequence[str], *, ttl_s: float = 10 * 60) -> str:
    """Proxy URL (with token) for an HTTP client that may talk to exactly `hosts`."""
    return (await get_egress(settings).credentials(hosts, ttl_s=ttl_s)).url
