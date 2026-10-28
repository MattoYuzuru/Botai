"""PlaywrightGateway against the synthetic university with real Chromium (marker `browser`)."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import Mock, PropertyMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Route
from pydantic import SecretStr

from botai.browser.gateway import SAMPLE_CHARS, CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.browser.proxy import EgressProxy
from botai.browser.session import decode_session, encode_session
from botai.core.browser import SessionCookie, SessionOriginStorage, SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from tests.fakeuni import data
from tests.fakeuni.login import SPA_JWT, SPA_JWT_EXPIRES

pytestmark = pytest.mark.browser

HOSTS = ("fakeuni.test", "id.fakeuni.test")
Opener = Callable[..., Awaitable[PlaywrightHandle]]


@pytest.fixture
async def open_browser(gateway: PlaywrightGateway) -> AsyncIterator[Opener]:
    opened: list[PlaywrightHandle] = []

    async def _open(hosts: tuple[str, ...] = HOSTS, **kwargs: Any) -> PlaywrightHandle:
        kwargs.setdefault("user_id", new_id())
        handle = await gateway.open(connection_id=new_id(), allow_hosts=hosts, **kwargs)
        opened.append(handle)
        return handle

    yield _open
    for handle in opened:
        await handle.close()


async def log_in(handle: PlaywrightHandle) -> None:
    await handle.goto("https://fakeuni.test/auth/login")
    await handle.fill("#username", data.USERNAME)
    await handle.fill("#password", data.PASSWORD)
    await handle.click("#kc-login")
    await handle.fill("#otp", data.OTP)
    await handle.click("#kc-otp-submit")


# ------------------------------------------------------------------------------------ snapshot


async def test_snapshot_describes_the_login_form(open_browser: Opener) -> None:
    handle = await open_browser()
    snap = await handle.goto("https://fakeuni.test/auth/login")
    assert snap.url == "https://fakeuni.test/auth/login"
    assert snap.title == "Вход — Фейкуни"
    assert "Пароль" in snap.text
    assert snap.captcha_suspected is False
    (form,) = snap.forms
    assert (form.selector, form.method, form.action) == ("#kc-form-login", "post", "/auth/login")
    assert form.submit_selector == "#kc-login"
    username, password = form.fields
    assert (username.selector, username.input_type, username.label) == (
        "#username",
        "email",
        "Почта",
    )
    assert username.autocomplete == "username"
    assert (password.selector, password.input_type, password.label) == (
        "#password",
        "password",
        "Пароль",
    )
    assert password.autocomplete == "current-password"


async def test_otp_step_exposes_its_hints(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/auth/login")
    await handle.fill("#username", data.USERNAME)
    await handle.fill("#password", data.PASSWORD)
    snap = await handle.click("#kc-login")
    assert snap.url.endswith("/auth/otp")
    (otp,) = snap.forms[0].fields
    assert otp.autocomplete == "one-time-code"
    assert getattr(otp, "inputmode", None) == "numeric"
    assert otp.label == "Код из SMS"


async def test_snapshot_never_contains_typed_values_or_scripts(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/auth/login")
    await handle.fill("#password", data.PASSWORD)
    await handle.page.evaluate(
        "document.getElementById('password').setAttribute('value', 'attr-' + document.getElementById('password').value)"
    )
    snap = await handle.snapshot()
    assert data.PASSWORD not in snap.html
    assert data.PASSWORD not in snap.text
    await log_in(handle)
    app = await handle.snapshot()
    assert "<script" not in app.html
    assert "fetch(" not in app.html


async def test_captcha_page_is_flagged(open_browser: Opener) -> None:
    handle = await open_browser()
    assert (await handle.goto("https://fakeuni.test/captcha/login")).captcha_suspected is True
    assert (await handle.goto("https://fakeuni.test/auth/login")).captcha_suspected is False


# --------------------------------------------------------------------------------------- login


async def test_full_login_and_session_export(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/auth/login")
    await handle.fill("#username", data.USERNAME)
    await handle.fill("#password", "wrong password")
    wrong = await handle.click("#kc-login")
    assert "Неверный логин или пароль" in wrong.text
    await log_in(handle)
    state = await handle.export_session()
    assert state.origin == "https://fakeuni.test"
    cookie = next(c for c in state.cookies if c.name == "FAKEUNI_SESSION")
    assert cookie.http_only is True
    assert cookie.secure is True
    assert cookie.domain.lstrip(".") == "fakeuni.test"
    assert state.captured_at.tzinfo is not None
    assert state.bearer is None
    # secrets stay masked in reprs and logs
    assert cookie.value.get_secret_value() not in repr(state)
    assert cookie.value.get_secret_value() in encode_session(state)
    assert decode_session(encode_session(state)).cookies[0].name == state.cookies[0].name


async def test_network_log_records_xhr_with_bounded_samples(open_browser: Opener) -> None:
    handle = await open_browser()
    await log_in(handle)
    log = await handle.network_log()
    api = {
        r.url.split("fakeuni.test")[1].split("?")[0]: r for r in log if r.resource_type == "fetch"
    }
    me = api["/api/v2/me"]
    assert (me.method, me.status) == ("GET", 200)
    assert me.response_content_type is not None
    assert me.response_content_type.startswith("application/json")
    assert me.response_sample is not None
    assert "Иванова Анна" in me.response_sample
    assert any(r.resource_type == "document" for r in log)
    assert all(len(r.response_sample or "") <= SAMPLE_CHARS for r in log)


async def test_restoring_a_session_skips_the_login(open_browser: Opener) -> None:
    first = await open_browser()
    await log_in(first)
    state = await first.export_session()
    await first.close()
    second = await open_browser(session=decode_session(encode_session(state)))
    snap = await second.goto("https://fakeuni.test/api/v2/me")
    assert "Иванова Анна" in snap.text
    anonymous = await open_browser()
    assert "Иванова Анна" not in (await anonymous.goto("https://fakeuni.test/api/v2/me")).text


async def test_origin_storage_is_restored_before_application_scripts(open_browser: Opener) -> None:
    first = await open_browser()
    await first.goto("https://fakeuni.test/auth/login")
    marker = "storage-authentication-fixture"
    await first.page.evaluate(
        "value => { localStorage.setItem('access_token', value);"
        "localStorage.setItem('auth_ready', 'true');"
        "sessionStorage.setItem('transient_token', value); }",
        marker,
    )
    state = decode_session(encode_session(await first.export_session()))
    assert state.browser_storage
    assert marker not in repr(state)
    await first.close()
    second = await open_browser(session=state, read_only=True)

    async def application(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=(
                "<body><p id='out'></p><script>"
                "const ready = localStorage.getItem('access_token') === 'storage-authentication-fixture'"
                " && localStorage.getItem('auth_ready') === 'true'"
                " && sessionStorage.getItem('transient_token') === 'storage-authentication-fixture';"
                "document.getElementById('out').textContent = ready ? 'ready' : 'missing';"
                "</script></body>"
            ),
        )

    await second.context.route("https://fakeuni.test/storage-application", application)
    snap = await second.goto("https://fakeuni.test/storage-application")
    assert "ready" in snap.text
    assert "missing" not in snap.text


async def test_restoration_does_not_extend_storage_to_another_origin(open_browser: Opener) -> None:
    marker = "origin-private-storage-fixture"
    state = SessionState(
        origin="https://fakeuni.test",
        captured_at=datetime.now(UTC),
        browser_storage=[
            SessionOriginStorage(
                origin="https://fakeuni.test",
                local_storage={"token": SecretStr(marker)},
                session_storage={"token": SecretStr(marker)},
            ),
            SessionOriginStorage(
                origin="https://evil.test", local_storage={"token": SecretStr(marker)}
            ),
        ],
    )
    browser = await open_browser(session=state)

    async def other_origin(route: Route) -> None:
        await route.fulfill(
            content_type="text/html",
            body=(
                "<body><script>document.body.textContent = "
                "localStorage.getItem('token') === null && sessionStorage.getItem('token') === null"
                " ? 'isolated' : 'leaked';</script></body>"
            ),
        )

    await browser.context.route("https://id.fakeuni.test/storage-application", other_origin)
    snap = await browser.goto("https://id.fakeuni.test/storage-application")
    assert "isolated" in snap.text
    restored = await browser.context.storage_state()
    assert all(entry["origin"] != "https://evil.test" for entry in restored["origins"])


# ----------------------------------------------------------------------------------- bearer


async def test_bearer_is_taken_from_the_header_the_browser_sent(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/spa")
    await handle.page.wait_for_function("document.getElementById('out').textContent.includes('ok')")
    state = await handle.export_session()
    assert state.bearer is not None
    assert state.bearer.get_secret_value() == SPA_JWT
    assert state.expires_hint == datetime.fromtimestamp(SPA_JWT_EXPIRES, UTC)


async def test_a_whole_json_answer_is_sampled_so_the_learner_can_parse_it(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/spa/big?n=400")
    await handle.page.wait_for_function("document.title.startsWith('done')")
    big = next(r for r in await handle.network_log() if "/spa/api/big" in r.url)
    assert (big.resource_type, big.status) == ("fetch", 200)
    assert big.response_sample is not None
    assert len(big.response_sample) > 20_000  # far more than a one-line preview
    items = json.loads(big.response_sample)  # complete: parseable, not cut mid-item
    assert len(items) == 400
    assert items[7]["title"] == "Занятие 7"


async def test_samples_are_capped_and_html_answers_are_sampled_too(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/spa/big?n=6000")  # about 600 KB of JSON
    await handle.page.wait_for_function("document.title.startsWith('done')")
    await handle.page.evaluate("fetch('/screen/lab2').then((r) => r.text())")
    log = await handle.network_log()
    huge = next(r for r in log if "/spa/api/big" in r.url)
    assert huge.response_sample is not None
    assert len(huge.response_sample) == SAMPLE_CHARS  # cut at the cap, never more
    html = next(r for r in log if r.url.endswith("/screen/lab2") and r.resource_type == "fetch")
    assert html.response_content_type is not None
    assert html.response_content_type.startswith("text/html")
    assert html.response_sample is not None
    assert "Second page" in html.response_sample


async def test_token_endpoints_are_not_sampled(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto("https://fakeuni.test/spa")
    await handle.page.wait_for_function("document.getElementById('out').textContent.includes('ok')")
    log = await handle.network_log()
    ping = next(r for r in log if r.url.endswith("/spa/api/ping"))
    token = next(r for r in log if r.url.endswith("/spa/api/token"))
    assert ping.response_sample is not None
    assert token.response_sample is None
    assert all(SPA_JWT not in (r.response_sample or "") for r in log)


async def test_storage_bearer_only_with_an_explicit_key(open_browser: Opener) -> None:
    named = await open_browser(
        capture=CaptureConfig(bearer_from="sessionStorage", bearer_key="tok")
    )
    await named.goto("https://fakeuni.test/spa")
    assert (await named.export_session()).bearer == SecretStr(SPA_JWT)
    unnamed = await open_browser(capture=CaptureConfig(bearer_from="sessionStorage"))
    await unnamed.goto("https://fakeuni.test/spa")
    assert (await unnamed.export_session()).bearer is None
    none = await open_browser(capture=CaptureConfig(bearer_from="none"))
    await none.goto("https://fakeuni.test/spa")
    assert (await none.export_session()).bearer is None


async def test_restored_bearer_is_sent_to_the_session_origin_only(open_browser: Opener) -> None:
    state = SessionState(
        origin="https://fakeuni.test", bearer=SecretStr(SPA_JWT), captured_at=datetime.now(UTC)
    )
    handle = await open_browser(session=state)
    assert '"ok"' in (await handle.goto("https://fakeuni.test/spa/api/ping")).text


async def test_cookies_outside_the_capture_hosts_are_not_exported(open_browser: Opener) -> None:
    handle = await open_browser(capture=CaptureConfig(cookies_for=("id.fakeuni.test",)))
    await log_in(handle)
    assert (await handle.export_session()).cookies == []


# ---------------------------------------------------------------------------- egress is a wall


@pytest.mark.parametrize(
    ("path", "host"),
    [
        ("/redirect/evil", "evil.localhost"),
        ("/redirect/ip", "127.0.0.1"),
        ("/redirect/metadata", "169.254.169.254"),
    ],
)
async def test_redirect_to_a_foreign_host_is_refused(
    open_browser: Opener, egress: EgressProxy, path: str, host: str
) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    with pytest.raises(BotaiError) as exc:
        await handle.goto(f"https://fakeuni.test{path}")
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra["host"] == host
    assert host in handle.blocked_hosts()
    assert "?" not in exc.value.detail  # no query strings in user-facing text


async def test_navigation_to_an_unlisted_host_never_reaches_the_browser(
    open_browser: Opener,
) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    for url in (
        "https://id.fakeuni.test/auth/login",
        "https://127.0.0.1:8443/",
        "http://fakeuni.test/auth/login",
        "https://user:pw@fakeuni.test/",
    ):
        with pytest.raises(BotaiError) as exc:
            await handle.goto(url)
        assert exc.value.code == ErrorCode.EGRESS_BLOCKED, url


@pytest.mark.parametrize("tag", ["img", "iframe"])
async def test_background_denial_cannot_become_a_login_host_approval(
    open_browser: Opener, tag: str
) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    await handle.goto("https://fakeuni.test/auth/login")
    background = "https://evil.localhost/blocked-background"
    async with handle.page.expect_event(
        "requestfailed", predicate=lambda request: request.url == background
    ):
        await handle.page.evaluate(
            "([tag, url]) => { const node = document.createElement(tag); "
            "node.src = url; document.body.append(node); }",
            [tag, background],
        )
    assert "evil.localhost" in handle.blocked_hosts()
    assert handle.navigation_failure() is None

    # A subsequent failure of the allowed main page has its own cause. An old blocked
    # tracker/frame must never cause the user to be asked to allow that unrelated host.
    broken = "https://fakeuni.test/broken-navigation"
    await handle.page.route(broken, lambda route: route.abort("connectionrefused"))
    with pytest.raises(BotaiError) as error:
        await handle.goto(broken)
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    failure = handle.navigation_failure()
    assert failure is not None
    assert failure.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "evil.localhost" not in error.value.detail

    await handle.goto("https://fakeuni.test/auth/login")
    assert handle.navigation_failure() is None


async def test_navigation_before_its_frame_does_not_escape_the_event_callback(
    open_browser: Opener,
) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    request = Mock(spec=Request)
    request.is_navigation_request.return_value = True
    type(request).frame = PropertyMock(side_effect=PlaywrightError("Frame is not available yet"))
    request.url = "https://id.fakeuni.test/early-popup"
    request.method = "GET"
    request.headers = {}
    request.resource_type = "document"
    request.failure = "net::ERR_TUNNEL_CONNECTION_FAILED"

    handle._on_request(cast(Request, request))
    handle._on_failed(cast(Request, request))
    assert "id.fakeuni.test" in handle.visited_hosts()
    assert handle.navigation_failure() is None


async def test_page_scripts_cannot_bypass_the_proxy(
    open_browser: Opener, egress: EgressProxy, fakeuni_port: int
) -> None:
    """The fixture server really listens on 127.0.0.1:<port>; a direct connection would succeed."""
    handle = await open_browser(hosts=("fakeuni.test",))
    await handle.goto("https://fakeuni.test/auth/login")
    before = egress.stats["deny:ip_literal"]
    result = await handle.page.evaluate(
        "(port) => fetch('https://127.0.0.1:' + port + '/auth/login')"
        ".then((r) => 'reached ' + r.status).catch(() => 'blocked')",
        fakeuni_port,
    )
    assert result == "blocked"
    assert egress.stats["deny:ip_literal"] > before


async def test_a_typed_value_never_rides_along_in_an_error(open_browser: Opener) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    await handle.page.set_content('<input id="r" readonly>')
    secret = "".join(
        ["SECRET", "-VALUE-7"]
    )  # (not a literal: it would show in the test's own frame)
    with pytest.raises(BotaiError) as exc:
        await handle.fill("#r", secret)  # the field exists but cannot be edited
    shown = "".join(traceback.format_exception(exc.value)) + repr(exc.value) + exc.value.detail
    assert secret not in shown


async def test_a_context_is_not_left_behind_when_opening_fails(
    gateway: PlaywrightGateway,
) -> None:
    await gateway.open(user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"])
    browser = gateway._browser
    assert browser is not None
    before = len(browser.contexts)
    broken = SessionState(
        origin="https://fakeuni.test",
        cookies=[SessionCookie(name="a", value=SecretStr("b"), domain="")],
        captured_at=datetime.now(UTC),
    )
    with pytest.raises(PlaywrightError):
        await gateway.open(
            user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"], session=broken
        )
    assert len(browser.contexts) == before


async def test_bearer_comes_from_the_origin_or_the_only_token_sent(open_browser: Opener) -> None:
    handle = await open_browser(origin="https://fakeuni.test")
    handle._bearer = {"api.fakeuni.test": "T1"}
    assert (await handle.export_session()).bearer == SecretStr("T1")  # one token, an API host
    handle._bearer = {"api.fakeuni.test": "T1", "id.fakeuni.test": "T2"}
    assert (await handle.export_session()).bearer is None  # two candidates: no guess
    handle._bearer = {"api.fakeuni.test": "T1", "fakeuni.test": "T0"}
    assert (await handle.export_session()).bearer == SecretStr("T0")  # the origin's own wins


async def test_downloads_are_cancelled(open_browser: Opener) -> None:
    handle = await open_browser(hosts=("fakeuni.test",))
    with pytest.raises(BotaiError):
        await handle.goto("https://fakeuni.test/download")


# ----------------------------------------------------------------------------------- lifetime


async def test_one_live_context_per_user(open_browser: Opener, gateway: PlaywrightGateway) -> None:
    user = new_id()
    first = await open_browser(user_id=user)
    second = await open_browser(user_id=user)
    assert first.closed is True
    assert second.closed is False
    assert gateway.active_for(user) is second


async def test_close_is_idempotent_and_runs_callbacks(open_browser: Opener) -> None:
    handle = await open_browser()
    calls: list[int] = []
    handle.on_close(lambda: calls.append(1))
    await handle.close()
    await handle.close()
    assert calls == [1]


async def test_no_secrets_in_logs(open_browser: Opener, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        handle = await open_browser()
        await log_in(handle)
        await handle.export_session()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert data.PASSWORD not in text
    assert data.OTP not in text


@pytest.mark.parametrize("read_only", [False, True])
async def test_readonly_renderer_blocks_writes_and_websockets_at_the_server(
    open_browser: Opener, read_only: bool
) -> None:
    from tests.fakeuni import login

    login.READ_ONLY_WRITES = login.READ_ONLY_SOCKETS = 0
    handle = await open_browser(read_only=read_only, background=read_only)
    await handle.goto("https://fakeuni.test/readonly/page")
    await handle.page.wait_for_function(
        "document.body.dataset.read === 'ok' && document.body.dataset.write && document.body.dataset.socket === 'closed'"
    )
    assert (0 if read_only else 1) == login.READ_ONLY_WRITES
    assert (0 if read_only else 1) == login.READ_ONLY_SOCKETS
    assert await handle.page.get_attribute("body", "data-write") == (
        "blocked" if read_only else "sent"
    )
    if read_only:
        with pytest.raises(BotaiError):
            await handle.fill("h1", "must-not-be-typed")
        with pytest.raises(BotaiError):
            await handle.click("h1")
        with pytest.raises(BotaiError):
            await handle.press("h1", "Enter")


async def test_background_renderer_preserves_a_foreground_login(open_browser: Opener) -> None:
    user = new_id()
    foreground = await open_browser(user_id=user)
    await foreground.goto("https://fakeuni.test/auth/login")
    await foreground.fill("#username", "unfinished-human-input")
    with pytest.raises(BotaiError) as exc:
        await open_browser(user_id=user, read_only=True, background=True)
    assert exc.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert not foreground.closed
    assert await foreground.page.input_value("#username") == "unfinished-human-input"


async def test_foreground_open_wins_a_race_with_background_rendering(
    open_browser: Opener, gateway: PlaywrightGateway
) -> None:
    import asyncio

    user = new_id()
    results = await asyncio.gather(
        open_browser(user_id=user, read_only=True, background=True),
        open_browser(user_id=user),
        return_exceptions=True,
    )
    foreground = results[1]
    assert isinstance(foreground, PlaywrightHandle)
    assert not foreground.closed
    assert gateway.active_for(user) is foreground
    background = results[0]
    assert isinstance(background, BotaiError) or (
        isinstance(background, PlaywrightHandle) and background.closed
    )
