"""The single way adapters talk to a source: allowlisted client, retries, session, expiry signs.

Every request goes through `core.http` (`build_client` with the connection's allowlist and the
EgressProxy URL, `request_with_retries` with LLD §5 timeouts), so no adapter can reach a host the
connection was not granted.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import build_client, host_allowed, request_with_retries
from botai.sources.adapter import ExpiredRules, SessionExpired, SyncContext
from botai.sources.session import auth_headers, cookie_jar, known_expired

MAX_BODY = 10 * 1024 * 1024
MAX_REQUESTS = 120  # per fetch: a spec cannot turn a sync into a flood (pagination, bootstrap)


def origin_host(origin: str) -> str:
    host = urlsplit(origin).hostname
    if not host:
        raise BotaiError(ErrorCode.VALIDATION, "Некорректный адрес источника")
    return host.lower()


def check_expired(response: httpx.Response, rules: ExpiredRules) -> None:
    """Raise SessionExpired on 401/403 or a redirect to a login page (preset rules, other host)."""
    if response.status_code in (401, 403):
        raise SessionExpired(f"HTTP {response.status_code}")
    if not response.history:
        return
    landing = [r.url for r in response.history[1:]] + [response.url]
    if response.url.host != response.history[0].url.host:
        raise SessionExpired("redirected to another host")
    for url in landing:
        if any(url.path.startswith(prefix) for prefix in rules.redirect_path_prefixes):
            raise SessionExpired("redirected to the login page")


class SourceClient:
    """Async context manager around one allowlisted httpx client for a single fetch."""

    def __init__(
        self,
        ctx: SyncContext,
        *,
        auth_type: str = "none",
        header_name: str | None = None,
        use_session: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._ctx = ctx
        self._requests = 0
        self._auth_type = auth_type
        session = ctx.session if use_session else None
        if auth_type != "none" and session is not None and known_expired(session, ctx.now):
            raise SessionExpired("session is past its expiry hint")
        self._cookies = cookie_jar(session, ctx.now) if auth_type == "cookie" else None
        self._headers = {
            **auth_headers(session, auth_type=auth_type, header_name=header_name),
            **(headers or {}),
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        # The production EgressProxy only accepts a token bound to this connection's allowlist
        # (security.md §4); a bare proxy URL is refused with 407. Tests pass a mock transport.
        proxy = self._ctx.proxy_url
        if proxy and self._ctx.transport is None:
            from botai.browser.egress import proxy_url_for
            from botai.settings import get_settings

            proxy = await proxy_url_for(get_settings(), list(self._ctx.allow_hosts))
        self._client = build_client(
            allow_hosts=self._ctx.allow_hosts,
            proxy_url=proxy,
            cookies=self._cookies,
            headers=self._headers,
            transport=self._ctx.transport,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        json: Any = None,
        check_session: bool = True,
    ) -> httpx.Response:
        if not host_allowed(url, self._ctx.allow_hosts):
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                "Адрес не входит в разрешённые для этого подключения",
                extra={"host": urlsplit(url).hostname or ""},
            )
        self._requests += 1
        if self._requests > MAX_REQUESTS:
            raise BotaiError(
                ErrorCode.SOURCE_RATE_LIMITED, "Слишком много запросов за одну синхронизацию"
            )
        if self._ctx.before_request is not None:
            await self._ctx.before_request()
        kwargs: dict[str, Any] = {"policy": self._ctx.retry}
        if params:
            kwargs["params"] = dict(params)
        if data is not None:
            kwargs["data"] = dict(data)
        if json is not None:
            kwargs["json"] = json
        if self._client is None:
            raise RuntimeError("SourceClient must be used as `async with`")
        response = await request_with_retries(self._client, method, url, **kwargs)
        auth_required = False
        try:
            if check_session:
                check_expired(response, self._ctx.expired)
        except SessionExpired:
            auth_required = True
            raise
        finally:
            diagnostic_event(
                "auth.source_response",
                method=method,
                status=response.status_code,
                auth_type=self._auth_type,
                transport="http",
                auth_required=auth_required,
                redirected=bool(response.history),
                outcome="rejected" if auth_required else "ok",
                **url_facts(str(response.request.url)),
            )
        if len(response.content) > MAX_BODY:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Ответ источника слишком большой")
        return response
