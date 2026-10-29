"""Audience-preserving session storage, restoration and executor-captured auth facts."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from playwright.async_api import BrowserContext, Request, Route
from pydantic import SecretStr, ValidationError

from botai.browser import probe
from botai.browser.egress import Egress, ProxyCredentials
from botai.browser.gateway import PlaywrightGateway
from botai.browser.session import decode_session, encode_session, request_headers
from botai.core.browser import SessionBearer, SessionCookie, SessionOriginStorage, SessionState
from botai.core.ids import new_id
from botai.learn.redact import Redactor
from botai.sources.session import dump_session, scoped_bearer
from tests.fakeuni.login import SPA_JWT
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
API_ORIGIN = "https://api.fakeuni.test"


def stored() -> SessionState:
    return SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer=SecretStr("legacy-primary-fixture"),
        bearer_scopes=[
            SessionBearer(origin=origin, token=SecretStr(token), captured_at=NOW)
            for origin, token in ((ORIGIN, "primary-fixture"), (API_ORIGIN, "api-fixture"))
        ],
        browser_storage=[
            SessionOriginStorage(
                origin=ORIGIN, local_storage={"neutral": SecretStr("storage-fixture")}
            )
        ],
    )


@pytest.mark.parametrize("encode", [encode_session, dump_session])
def test_scoped_tokens_are_masked_and_round_trip_only_through_vault_encoding(
    encode: Callable[[SessionState], str],
) -> None:
    state = stored()
    masked = repr(state) + state.model_dump_json()
    for scope in state.bearer_scopes:
        assert scope.token.get_secret_value() not in masked
    plain = encode(state)
    restored = decode_session(plain)
    assert restored == state
    assert "primary-fixture" in plain
    assert "api-fixture" in plain
    assert "storage-fixture" in plain


def test_legacy_sessions_are_readable_but_do_not_prove_new_audience_bindings() -> None:
    state = decode_session(
        json.dumps(
            {"origin": ORIGIN, "captured_at": NOW.isoformat(), "bearer": "old-session-fixture"}
        )
    )
    assert state.bearer_scopes == []
    assert scoped_bearer(state, ORIGIN) is None
    assert scoped_bearer(state, ORIGIN, allow_legacy=True) == SecretStr("old-session-fixture")
    assert scoped_bearer(state, API_ORIGIN, allow_legacy=True) is None
    assert request_headers(state, url=ORIGIN + "/me") == {
        "authorization": "Bearer old-session-fixture"
    }
    assert request_headers(state, url=API_ORIGIN + "/me") == {}
    assert request_headers(state, url=ORIGIN + ":444/me") == {}


def test_scoped_token_values_are_protected_when_reflected_in_neutral_response_fields() -> None:
    redactor = Redactor()
    state = stored()
    redactor.protect_session(state)
    for scope in state.bearer_scopes:
        marker = scope.token.get_secret_value()
        assert marker not in redactor.json_text({"neutral": marker}, 1000)
        assert marker not in redactor.html("<main>" + marker + "</main>")
        assert marker not in redactor.url(ORIGIN + "/data?neutral=" + marker, ORIGIN)


@pytest.mark.parametrize(
    "origin",
    ["http://fakeuni.test", ORIGIN + "/api", ORIGIN + "?token=private", "https://u:p@fakeuni.test"],
)
def test_bearer_scope_must_be_a_plain_https_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        SessionBearer(origin=origin, token=SecretStr("fixture"), captured_at=NOW)


def test_one_session_cannot_have_ambiguous_tokens_for_the_same_origin() -> None:
    state = stored()
    with pytest.raises(ValidationError):
        SessionState(origin=ORIGIN, captured_at=NOW, bearer_scopes=[state.bearer_scopes[0]] * 2)


async def test_browser_restore_uses_exact_audiences_and_keeps_application_authorization() -> None:
    context = Mock(spec=BrowserContext)
    context.route = AsyncMock()
    await PlaywrightGateway._restore(
        cast(BrowserContext, context),
        stored(),
        allow_hosts=("fakeuni.test", "api.fakeuni.test", "id.fakeuni.test"),
    )
    assert context.route.await_args is not None
    handler = cast(Callable[[Route], Awaitable[None]], context.route.await_args.args[1])
    for url, own, expected in (
        (ORIGIN + "/me", None, "Bearer primary-fixture"),
        (API_ORIGIN + "/me", None, "Bearer api-fixture"),
        ("https://id.fakeuni.test/me", None, None),
        (ORIGIN + ":444/me", None, None),
        (ORIGIN + "/me", "Basic app-owned-fixture", "Basic app-owned-fixture"),
    ):
        request = Mock(spec=Request)
        request.url = url
        request.headers = {"authorization": own} if own else {}
        route = Mock(spec=Route)
        route.request = request
        route.continue_ = AsyncMock()
        await handler(cast(Route, route))
        assert route.continue_.await_args is not None
        headers = route.continue_.await_args.kwargs.get("headers", request.headers)
        assert headers.get("authorization") == expected


@pytest.mark.browser
async def test_browser_captures_cookie_and_authorization_facts_without_header_values(
    gateway: PlaywrightGateway,
) -> None:
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        cookies=[
            SessionCookie(
                name="evidence", value=SecretStr("private-cookie-fixture"), domain="fakeuni.test"
            )
        ],
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        session=state,
    )
    try:
        await handle.goto(ORIGIN + "/spa")
        await handle.page.wait_for_function(
            "document.getElementById('out').textContent.includes('ok')"
        )
        request = next(
            row for row in await handle.network_log() if row.url.endswith("/spa/api/ping")
        )
        assert request.status == 200
        assert request.auth_proven
        assert request.has_cookie
        assert request.has_authorization
        assert request.authorization_scheme == "bearer"
        assert request.evidence_id
        assert SPA_JWT not in request.model_dump_json()
        assert "private-cookie-fixture" not in request.model_dump_json()
        exported = await handle.export_session()
        assert [(scope.origin, scope.token) for scope in exported.bearer_scopes] == [
            (ORIGIN, SecretStr(SPA_JWT))
        ]
    finally:
        await handle.close()


@pytest.mark.browser
async def test_restored_scoped_bearer_still_proves_the_actual_request_and_survives_export(
    gateway: PlaywrightGateway,
) -> None:
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer_scopes=[SessionBearer(origin=ORIGIN, token=SecretStr(SPA_JWT), captured_at=NOW)],
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        session=state,
    )
    try:
        page = await handle.goto(ORIGIN + "/spa/api/ping")
        assert '"ok"' in page.text
        request = next(
            row for row in await handle.network_log() if row.url.endswith("/spa/api/ping")
        )
        assert request.status == 200
        assert request.auth_proven
        assert request.has_authorization
        assert request.authorization_scheme == "bearer"
        exported = await handle.export_session()
        assert [(scope.origin, scope.token) for scope in exported.bearer_scopes] == [
            (ORIGIN, SecretStr(SPA_JWT))
        ]
    finally:
        await handle.close()


async def test_probe_redirects_strip_credentials_for_the_remainder_of_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        destination = {"/start": API_ORIGIN + "/hop", "/hop": ORIGIN + "/finish"}.get(
            request.url.path
        )
        return (
            httpx.Response(302, headers={"location": destination})
            if destination
            else httpx.Response(200)
        )

    original = probe.build_client

    def client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["proxy_url"] = None
        kwargs["transport"] = httpx.MockTransport(redirect)
        return original(**kwargs)

    monkeypatch.setattr(probe, "build_client", client)
    egress = Mock(spec=Egress)
    egress.credentials = AsyncMock(return_value=ProxyCredentials("http://127.0.0.1:1", "fixture"))
    state = stored()
    state.cookies = [
        SessionCookie(name="sid", value=SecretStr("cookie-fixture"), domain="fakeuni.test")
    ]
    result = await probe._status(
        cast(Egress, egress),
        ("fakeuni.test", "api.fakeuni.test"),
        ORIGIN + "/start",
        state=state,
        follow=True,
    )
    assert result.status_code == 200
    assert len(seen) == 3
    assert seen[0].headers["authorization"] == "Bearer primary-fixture"
    assert seen[0].headers["cookie"] == "sid=cookie-fixture"
    for request in seen[1:]:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
