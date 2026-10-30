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

from botai.core.browser import auth_origin
from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import build_client, host_allowed, request_with_retries
from botai.sources.adapter import ExpiredRules, SessionExpired, SyncContext
from botai.sources.session import auth_headers, cookie_jar, known_expired, scoped_bearer

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
        raise SessionExpired(
            f"HTTP {response.status_code}", code="auth_rejected", status=response.status_code
        )
    if not response.history:
        return
    landing = [r.url for r in response.history[1:]] + [response.url]
    initial_origin = auth_origin(str(response.history[0].url))
    if any(auth_origin(str(url)) != initial_origin for url in landing):
        raise SessionExpired("redirected to another origin", code="login_redirect")
    for url in landing:
        if any(url.path.startswith(prefix) for prefix in rules.redirect_path_prefixes):
            raise SessionExpired("redirected to the login page", code="login_redirect")


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
        follow_redirects: bool = True,
    ) -> None:
        self._ctx = ctx
        self._follow_redirects = follow_redirects
        self._requests = 0
        self._auth_type = auth_type
        self._use_session = use_session
        self._session_seen = ctx.session
        self._origin = auth_origin(ctx.origin)
        self._credential_headers = {"authorization", "cookie"}
        if header_name:
            self._credential_headers.add(header_name.lower())
        session = ctx.session if use_session else None
        if (
            auth_type in {"cookie", "bearer"}
            and session is not None
            and known_expired(session, ctx.now)
        ):
            raise SessionExpired("session is past its expiry hint", code="auth_missing")
        if auth_type == "session" and any(
            name.lower() in self._credential_headers for name in headers or {}
        ):
            raise BotaiError(ErrorCode.RECIPE_REJECTED, "Авторизация задаётся привязкой запроса")
        self._cookies = cookie_jar(session, ctx.now) if auth_type in {"cookie", "session"} else None
        self._headers = {
            **auth_headers(
                session, auth_type=auth_type, header_name=header_name, origin=ctx.origin
            ),
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
        self._client.follow_redirects = self._follow_redirects
        self._client.event_hooks["request"].append(self._scope_credentials)
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
        auth_binding: Mapping[str, Any] | None = None,
        max_bytes: int | None = None,
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
        # An API answer or a page is capped at MAX_BODY; a document passes the cap of an upload.
        limit = max_bytes or MAX_BODY
        kwargs: dict[str, Any] = {"policy": self._ctx.retry, "max_bytes": limit}
        if params:
            kwargs["params"] = dict(params)
        if data is not None:
            kwargs["data"] = dict(data)
        if json is not None:
            kwargs["json"] = json
        if self._client is None:
            raise RuntimeError("SourceClient must be used as `async with`")
        policy: dict[str, Any] = {"origin": self._origin, "crossed": False}
        if self._auth_type == "session":
            if self._origin is None or auth_origin(url) != self._origin:
                raise SessionExpired(
                    "request is outside its auth audience", code="auth_scope_mismatch"
                )
            cookies, bearer = _binding_modes(auth_binding)
            policy.update(cookies=cookies, bearer=bearer)
            state = self._ctx.session if self._use_session else None
            if (cookies or bearer) and (state is None or known_expired(state, self._ctx.now)):
                raise SessionExpired("session is unavailable", code="auth_missing")
            # A LoginFlow refresh replaces the session, without retaining the previous jar/token.
            if state is not self._session_seen:
                self._client.cookies = cookie_jar(state, self._ctx.now)
                self._session_seen = state
            request_headers: dict[str, str] = {}
            if bearer:
                token = scoped_bearer(state, self._ctx.origin, now=self._ctx.now)
                if token is None:
                    raise SessionExpired("no bearer for this auth audience", code="auth_missing")
                request_headers["Authorization"] = f"Bearer {token.get_secret_value()}"
            if cookies:
                probe = self._client.build_request(method, url, params=params)
                if not probe.headers.get("cookie"):
                    raise SessionExpired("no live cookies for this request", code="auth_missing")
            kwargs["headers"] = request_headers
        kwargs["extensions"] = {"botai_auth_policy": policy}
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
        if len(response.content) > limit:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Ответ источника слишком большой")
        return response

    async def _scope_credentials(self, request: httpx.Request) -> None:
        """Every redirect hop is checked, even when it returns to the original origin."""
        policy = request.extensions.get("botai_auth_policy")
        if not isinstance(policy, dict):
            policy = {"origin": self._origin, "crossed": False}
            request.extensions["botai_auth_policy"] = policy
        if auth_origin(str(request.url)) != policy["origin"]:
            policy["crossed"] = True
        if policy["crossed"]:
            for name in self._credential_headers:
                request.headers.pop(name, None)
        elif self._auth_type == "session":
            if not policy.get("cookies"):
                request.headers.pop("cookie", None)
            if not policy.get("bearer"):
                request.headers.pop("authorization", None)


def _binding_modes(binding: Mapping[str, Any] | None) -> tuple[bool, bool]:
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"cookies", "bearer"}
        or type(binding["cookies"]) is not bool
        or not isinstance(binding["bearer"], str)
        or binding["bearer"] not in {"none", "observedAuthorization"}
    ):
        raise BotaiError(ErrorCode.RECIPE_REJECTED, "Некорректная привязка авторизации запроса")
    return binding["cookies"], binding["bearer"] == "observedAuthorization"
