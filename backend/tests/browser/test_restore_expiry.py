"""A retained private entry cannot restore known-expired auth into an actual read context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import BrowserContext, Request, Route
from pydantic import SecretStr

from botai.browser.gateway import PlaywrightGateway
from botai.core.browser import SessionBearer, SessionCookie, SessionOriginStorage, SessionState
from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from tests.fakeuni.login import SPA_JWT
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
OTHER = "https://id.fakeuni.test"
EXPIRED = "expired-restore-fixture"
LIVE = "live-other-audience-fixture"


@pytest.mark.browser
@pytest.mark.parametrize("expiry", ["global", "scope"])
async def test_expired_auth_is_absent_from_real_requests_including_application_storage(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch, expiry: str
) -> None:
    monkeypatch.setattr(gateway, "_clock", FrozenClock(NOW))
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        entry_url=SecretStr(ORIGIN + "/expiry-page"),
        expires_hint=NOW - timedelta(seconds=1) if expiry == "global" else None,
        bearer=SecretStr(EXPIRED),  # expired primary scope must prohibit the scalar fallback
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN,
                token=SecretStr(EXPIRED),
                captured_at=NOW,
                expires_hint=NOW - timedelta(seconds=1),
            ),
            SessionBearer(
                origin=OTHER,
                token=SecretStr(LIVE),
                captured_at=NOW,
                expires_hint=NOW + timedelta(hours=1),
            ),
        ],
        cookies=[
            SessionCookie(name="old", domain="fakeuni.test", value=SecretStr(EXPIRED)),
            SessionCookie(
                name="live", domain="fakeuni.test", value=SecretStr("live-cookie-fixture")
            ),
        ],
        browser_storage=[
            SessionOriginStorage(
                origin=ORIGIN,
                local_storage={"opaque": SecretStr(EXPIRED)},
                session_storage={"other": SecretStr(EXPIRED)},
            )
        ],
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        session=state,
        read_only=True,
        background=True,
    )
    seen: list[tuple[str, dict[str, str]]] = []

    async def fixture(route: Route) -> None:
        seen.append((route.request.url, await route.request.all_headers()))
        body = (
            """<main>Reader fixture<script>
        const saved=localStorage.getItem('opaque')||sessionStorage.getItem('other');
        fetch('/expiry-data', {headers:saved?{Authorization:'Bearer '+saved}:{}})
          .then(()=>document.body.dataset.ready='yes');
        </script></main>"""
            if route.request.url.endswith("/expiry-page")
            else "<main>Data</main>"
        )
        await route.fulfill(content_type="text/html; charset=utf-8", body=body)

    await handle.context.route("**/expiry-*", fixture)
    try:
        await handle.goto(ORIGIN + "/expiry-page")
        await handle.page.wait_for_function("document.body.dataset.ready==='yes'")
        assert len(seen) == 2
        for _, headers in seen:
            assert "authorization" not in headers
            assert EXPIRED not in headers.get("cookie", "")
            assert ("live=live-cookie-fixture" in headers.get("cookie", "")) == (expiry == "scope")
        assert await handle.page.evaluate("Object.keys(localStorage).length") == 0
        assert await handle.page.evaluate("Object.keys(sessionStorage).length") == 0
        assert (await handle.export_session()).entry_url == state.entry_url
    finally:
        await handle.close()


async def test_expired_primary_scope_never_falls_back_to_legacy_scalar_but_live_audience_remains() -> (
    None
):
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer=SecretStr(EXPIRED),
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN,
                token=SecretStr(EXPIRED),
                captured_at=NOW,
                expires_hint=NOW - timedelta(seconds=1),
            ),
            SessionBearer(
                origin=OTHER,
                token=SecretStr(LIVE),
                captured_at=NOW,
                expires_hint=NOW + timedelta(hours=1),
            ),
        ],
    )
    context = Mock(spec=BrowserContext)
    context.route = AsyncMock()
    await PlaywrightGateway._restore(  # pyright: ignore[reportPrivateUsage]
        cast(BrowserContext, context),
        state,
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        now=NOW,
    )
    assert context.route.await_args is not None
    handler = cast(Callable[[Route], Awaitable[None]], context.route.await_args.args[1])
    for origin, expected in ((ORIGIN, None), (OTHER, "Bearer " + LIVE)):
        request = Mock(spec=Request)
        request.url = origin + "/data"
        request.headers = {}
        route = Mock(spec=Route)
        route.request = request
        route.continue_ = AsyncMock()
        await handler(cast(Route, route))
        assert route.continue_.await_args is not None
        headers = route.continue_.await_args.kwargs.get("headers", {})
        assert headers.get("authorization") == expected


@pytest.mark.browser
@pytest.mark.parametrize("token", ["opaque-live-fixture", SPA_JWT], ids=["opaque", "jwt"])
async def test_identical_export_preserves_earliest_known_expiry_but_rotation_does_not_inherit_it(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    clock = FrozenClock(NOW)
    monkeypatch.setattr(gateway, "_clock", clock)
    expiry = NOW + timedelta(minutes=5)
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN, token=SecretStr(token), captured_at=NOW, expires_hint=expiry
            )
        ],
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        session=state,
        read_only=True,
        background=True,
    )
    try:
        await handle.goto(ORIGIN + "/readonly/read")
        identical = await handle.export_session()
        assert identical.bearer_scopes[0].token == SecretStr(token)
        assert identical.bearer_scopes[0].expires_hint == expiry
        assert identical.expires_hint == expiry
        await handle.page.evaluate("""async () => {
          await (await fetch('/readonly/read?rotation=yes', {
            headers:{Authorization:'Bearer rotated-opaque-fixture'}
          })).text();
        }""")
        rotated = await handle.export_session()
        assert rotated.bearer_scopes[0].token == SecretStr("rotated-opaque-fixture")
        assert rotated.bearer_scopes[0].expires_hint is None
        assert rotated.expires_hint is None
    finally:
        await handle.close()


@pytest.mark.browser
@pytest.mark.parametrize("expiry", ["global", "scope"])
async def test_open_context_does_not_inject_fallback_after_clock_crosses_expiry(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch, expiry: str
) -> None:
    clock = FrozenClock(NOW)
    monkeypatch.setattr(gateway, "_clock", clock)
    deadline = NOW + timedelta(seconds=1)
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        expires_hint=deadline if expiry == "global" else None,
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN,
                token=SecretStr("short-lived-fixture"),
                captured_at=NOW,
                expires_hint=deadline if expiry == "scope" else None,
            )
        ],
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        session=state,
        read_only=True,
        background=True,
    )
    try:
        await handle.goto(ORIGIN + "/readonly/read")
        first = (await handle.network_log())[-1]
        assert first.status == 200
        assert first.has_authorization
        clock.advance(timedelta(seconds=2))
        await handle.page.evaluate(
            "async()=>{await (await fetch('/readonly/read?after=expiry')).text();}"
        )
        last = next(
            row for row in reversed(await handle.network_log()) if "after=expiry" in row.url
        )
        assert last.status == 200
        assert not last.has_authorization
        await handle.page.evaluate("""async()=>{
          await (await fetch('/readonly/read?owned=app', {headers:{Authorization:'Bearer own-new-fixture'}})).text();
        }""")
        owned = next(row for row in reversed(await handle.network_log()) if "owned=app" in row.url)
        assert owned.status == 200
        assert owned.has_authorization
        assert (await handle.export_session()).bearer_scopes[0].token == SecretStr(
            "own-new-fixture"
        )
    finally:
        await handle.close()
