"""Auth bindings replay current, origin-owned credentials without following them across sites."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from botai.core.browser import SessionBearer, SessionCookie, SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapter import SessionExpired
from botai.sources.fetch import SourceClient
from tests.sources.helpers import NOW, make_ctx

ORIGIN = "https://fakeuni.test"
MIXED = {"cookies": True, "bearer": "observedAuthorization"}


def session(generation: str = "first", **changes: Any) -> SessionState:
    return SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer=SecretStr("legacy-" + generation),
        cookies=[
            SessionCookie(
                name="sid", domain="fakeuni.test", value=SecretStr("cookie-" + generation)
            )
        ],
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN,
                token=SecretStr("scoped-" + generation),
                captured_at=NOW,
                expires_hint=NOW + timedelta(hours=1),
            )
        ],
        **changes,
    )


async def test_each_request_replays_exactly_its_proven_cookie_bearer_combination() -> None:
    requests: list[httpx.Request] = []

    def source(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        required = {
            "/mixed": ("sid=cookie-first", "Bearer scoped-first"),
            "/cookie": ("sid=cookie-first", None),
            "/bearer": (None, "Bearer scoped-first"),
            "/public": (None, None),
        }[request.url.path]
        assert (request.headers.get("cookie"), request.headers.get("authorization")) == required
        return httpx.Response(200, json={"ok": True})

    ctx = make_ctx(session=session(), transport=httpx.MockTransport(source))
    async with SourceClient(ctx, auth_type="session") as client:
        for path, binding in (
            ("/mixed", MIXED),
            ("/cookie", {"cookies": True, "bearer": "none"}),
            ("/bearer", {"cookies": False, "bearer": "observedAuthorization"}),
            ("/public", {"cookies": False, "bearer": "none"}),
        ):
            assert (
                await client.request("GET", ORIGIN + path, auth_binding=binding)
            ).status_code == 200
    assert len(requests) == 4


async def test_public_binding_can_read_with_an_expired_unused_session() -> None:
    seen: list[httpx.Request] = []

    def source(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200)

    state = session(expires_hint=NOW - timedelta(seconds=1))
    ctx = make_ctx(session=state, transport=httpx.MockTransport(source))
    async with SourceClient(ctx, auth_type="session") as client:
        response = await client.request(
            "GET", ORIGIN + "/public", auth_binding={"cookies": False, "bearer": "none"}
        )
        assert response.status_code == 200
        with pytest.raises(SessionExpired) as error:
            await client.request("GET", ORIGIN + "/private", auth_binding=MIXED)
        assert error.value.code == "auth_missing"
    assert len(seen) == 1


async def test_replacing_the_session_after_login_refresh_replaces_both_credentials() -> None:
    seen: list[tuple[str | None, str | None]] = []

    def source(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("cookie"), request.headers.get("authorization")))
        return httpx.Response(200)

    ctx = make_ctx(session=session(), transport=httpx.MockTransport(source))
    async with SourceClient(ctx, auth_type="session") as client:
        await client.request("GET", ORIGIN + "/calendar", auth_binding=MIXED)
        ctx.session = session("refreshed")
        await client.request("GET", ORIGIN + "/calendar", auth_binding=MIXED)
    assert seen == [
        ("sid=cookie-first", "Bearer scoped-first"),
        ("sid=cookie-refreshed", "Bearer scoped-refreshed"),
    ]


@pytest.mark.parametrize("mode", ["cookie", "bearer", "session"])
async def test_redirects_never_forward_credentials_even_when_returning_to_the_source(
    mode: str,
) -> None:
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        destination = {
            "/start": "https://id.fakeuni.test/hop",
            "/hop": ORIGIN + "/finish",
        }.get(request.url.path)
        return (
            httpx.Response(302, headers={"location": destination})
            if destination
            else httpx.Response(200)
        )

    ctx = make_ctx(
        session=session(),
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        transport=httpx.MockTransport(redirect),
    )
    async with SourceClient(ctx, auth_type=mode, header_name="X-Fake-Auth") as client:
        response = await client.request(
            "GET",
            ORIGIN + "/start",
            auth_binding=MIXED if mode == "session" else None,
            check_session=False,
        )
    assert response.status_code == 200
    assert len(seen) == 3
    assert any(name in seen[0].headers for name in ("cookie", "authorization", "x-fake-auth"))
    for request in seen[1:]:
        assert not any(
            name in request.headers for name in ("cookie", "authorization", "x-fake-auth")
        )


async def test_cross_origin_return_is_typed_as_a_login_redirect() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://id.fakeuni.test/hop"})
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": ORIGIN + "/finish"})
        return httpx.Response(200)

    ctx = make_ctx(
        session=session(),
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        transport=httpx.MockTransport(redirect),
    )
    async with SourceClient(ctx, auth_type="session") as client:
        with pytest.raises(SessionExpired) as error:
            await client.request("GET", ORIGIN + "/start", auth_binding=MIXED)
    assert error.value.code == "login_redirect"


@pytest.mark.parametrize("missing", ["legacy_only", "expired_bearer", "cookies"])
async def test_missing_proven_credentials_fail_before_network(missing: str) -> None:
    state = session()
    if missing == "legacy_only":
        state.bearer_scopes = []
    elif missing == "expired_bearer":
        state.bearer_scopes[0].expires_hint = NOW - timedelta(seconds=1)
    else:
        state.cookies = []
    calls = 0

    def unexpected(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    ctx = make_ctx(session=state, transport=httpx.MockTransport(unexpected))
    async with SourceClient(ctx, auth_type="session") as client:
        with pytest.raises(SessionExpired) as error:
            await client.request("GET", ORIGIN + "/calendar", auth_binding=MIXED)
    assert error.value.code == "auth_missing"
    assert calls == 0


@pytest.mark.parametrize("target", ["https://id.fakeuni.test", "https://fakeuni.test:444"])
async def test_a_binding_cannot_send_credentials_to_another_allowed_origin(target: str) -> None:
    calls = []
    ctx = make_ctx(
        session=session(),
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    async with SourceClient(ctx, auth_type="session") as client:
        with pytest.raises(SessionExpired) as error:
            await client.request("GET", target + "/calendar", auth_binding=MIXED)
    assert error.value.code == "auth_scope_mismatch"
    assert calls == []


@pytest.mark.parametrize(
    "binding", [None, {"cookies": True, "bearer": []}, {**MIXED, "token": "bad"}]
)
async def test_invalid_bindings_cannot_supply_credentials(binding: dict[str, Any] | None) -> None:
    ctx = make_ctx(session=session(), transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    async with SourceClient(ctx, auth_type="session") as client:
        with pytest.raises(BotaiError) as error:
            await client.request("GET", ORIGIN + "/calendar", auth_binding=binding)
    assert error.value.code == ErrorCode.RECIPE_REJECTED


@pytest.mark.parametrize("mode", ["cookie", "bearer", "none"])
async def test_legacy_auth_modes_remain_executable(mode: str) -> None:
    state = session()
    state.bearer_scopes = []

    def source(request: httpx.Request) -> httpx.Response:
        assert bool(request.headers.get("cookie")) is (mode == "cookie")
        assert request.headers.get("authorization") == (
            "Bearer legacy-first" if mode == "bearer" else None
        )
        return httpx.Response(200)

    ctx = make_ctx(session=state, transport=httpx.MockTransport(source))
    async with SourceClient(ctx, auth_type=mode) as client:
        await client.request("GET", ORIGIN + "/calendar")
