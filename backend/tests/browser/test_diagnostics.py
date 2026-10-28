"""Diagnostic observations describe authentication failures without retaining source data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from playwright.async_api import Browser, BrowserContext, Page, Request, Response, Route
from pydantic import SecretStr

from botai.browser.egress import Egress, ProxyCredentials
from botai.browser.gateway import CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.browser.probe import probe_ok
from botai.core.browser import ObservedRequest, SessionCookie, SessionState
from botai.core.clock import SystemClock
from botai.core.diagnostics import diagnostic_scope
from botai.core.ids import new_id

MARKER = "private-fixture-credential-never-recorded"
PRIVATE_URL = f"https://fakeuni.test/students/{MARKER}?access_token={MARKER}"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []

    def record(event: str, **facts: object) -> None:
        events.append((event, facts))

    monkeypatch.setattr("botai.browser.gateway.diagnostic_event", record)
    monkeypatch.setattr("botai.browser.probe.diagnostic_event", record)
    return events


def handle(context: Mock, page: Mock, *, connection_id: str = "source") -> PlaywrightHandle:
    gateway = PlaywrightGateway(cast(Egress, Mock()), sandbox=False)
    return PlaywrightHandle(
        gateway=gateway,
        context=cast(BrowserContext, context),
        page=cast(Page, page),
        user_id="student",
        connection_id=connection_id,
        allow_hosts=("fakeuni.test",),
        origin="https://fakeuni.test",
        capture=CaptureConfig(),
        clock=SystemClock(),
    )


async def test_restore_reports_missing_browser_storage_without_recording_credentials(
    captured: list[tuple[str, dict[str, object]]],
) -> None:
    context = Mock(spec=BrowserContext)
    context.add_cookies = AsyncMock()
    context.route = AsyncMock()
    state = SessionState(
        origin="https://fakeuni.test",
        cookies=[SessionCookie(name="session", value=SecretStr(MARKER), domain="fakeuni.test")],
        bearer=SecretStr(MARKER),
        captured_at=datetime.now(UTC),
    )
    await PlaywrightGateway._restore(cast(BrowserContext, context), state)  # pyright: ignore[reportPrivateUsage]
    assert captured == [
        ("browser.restore", {"cookie_count": 1, "bearer_present": True, "storage_restored": False})
    ]
    assert MARKER not in repr(captured)
    context.add_cookies.assert_awaited_once()
    context.route.assert_awaited_once()


async def test_callbacks_keep_handle_owner_instead_of_dispatcher_context(
    captured: list[tuple[str, dict[str, object]]],
) -> None:
    job_a, thread_a, connection_a = new_id(), new_id(), new_id()
    job_b, thread_b, request_b = new_id(), new_id(), new_id()
    context = Mock(spec=BrowserContext)
    with diagnostic_scope(job_id=job_a, thread_id=thread_a, job_kind="connect"):
        browser = handle(context, Mock(spec=Page), connection_id=connection_a)
    callbacks = {call.args[0]: call.args[1] for call in context.on.call_args_list}
    request = Mock(spec=Request)
    request.is_navigation_request.return_value = False
    request.method = "GET"
    request.resource_type = "xhr"
    request.headers = {}
    request.url = PRIVATE_URL
    request.failure = "net::ERR_BLOCKED_BY_CLIENT"
    response = Mock(spec=Response)
    response.request = request
    response.status = 200
    response.headers = {"content-type": "application/octet-stream"}
    response.url = PRIVATE_URL
    response.body = AsyncMock(return_value=b"{}")
    with diagnostic_scope(job_id=job_b, thread_id=thread_b, request_id=request_b, job_kind="sync"):
        callbacks["request"](request)
        callbacks["response"](response)
        callbacks["requestfailed"](request)
        await browser._sample(  # pyright: ignore[reportPrivateUsage]
            cast(Response, response), ObservedRequest(method="GET", url=PRIVATE_URL)
        )
    assert {event for event, _ in captured} == {
        "browser.request",
        "browser.response",
        "browser.request_failed",
        "browser.sample",
    }
    for _, facts in captured:
        assert facts["job_id"] == job_a
        assert facts["thread_id"] == thread_a
        assert facts["connection_id"] == connection_a
        assert facts["job_kind"] == "connect"
        assert facts["request_id"] is None


async def test_read_only_route_keeps_owner_when_dispatched_from_another_job(
    captured: list[tuple[str, dict[str, object]]],
) -> None:
    job_a, thread_a, connection_a, job_b = new_id(), new_id(), new_id(), new_id()
    context = Mock(spec=BrowserContext)
    context.new_page = AsyncMock(return_value=Mock(spec=Page))
    context.route = AsyncMock()
    context.route_web_socket = AsyncMock()
    context.close = AsyncMock()
    chromium = Mock(spec=Browser)
    chromium.new_context = AsyncMock(return_value=context)
    egress = Mock(spec=Egress)
    egress.credentials = AsyncMock(return_value=ProxyCredentials("http://127.0.0.1:3128", MARKER))
    gateway = PlaywrightGateway(cast(Egress, egress), sandbox=False)
    gateway._ensure_browser = AsyncMock(return_value=chromium)  # pyright: ignore[reportPrivateUsage]
    with diagnostic_scope(job_id=job_a, thread_id=thread_a):
        browser = await gateway.open(
            user_id=new_id(),
            connection_id=connection_a,
            allow_hosts=("fakeuni.test",),
            read_only=True,
        )
    try:
        assert context.route.await_args is not None
        guard = cast(Callable[[Route], Awaitable[None]], context.route.await_args.args[1])
        route = Mock(spec=Route)
        route.request.method = "POST"
        route.request.url = PRIVATE_URL
        route.abort = AsyncMock()
        with diagnostic_scope(job_id=job_b, request_id=new_id()):
            await guard(cast(Route, route))
        event, facts = captured[-1]
        assert event == "browser.read_only_blocked"
        assert facts["job_id"] == job_a
        assert facts["thread_id"] == thread_a
        assert facts["connection_id"] == connection_a
        assert facts["request_id"] is None
        route.abort.assert_awaited_once_with("blockedbyclient")
    finally:
        await browser.close()


async def test_snapshot_retains_only_authentication_hint_and_structure(
    captured: list[tuple[str, dict[str, object]]],
) -> None:
    page = Mock(spec=Page)
    page.url = PRIVATE_URL
    page.title = AsyncMock(return_value=MARKER)
    page.evaluate = AsyncMock(
        return_value={
            "text": f"Вы не вошли в аккаунт. Для продолжения, пожалуйста войдите. {MARKER}",
            "html": f"<main><h2>Вы не вошли в аккаунт</h2><p>{MARKER}</p></main>",
        }
    )
    browser = handle(Mock(spec=BrowserContext), page)
    snap = await browser.snapshot()
    assert MARKER in snap.text  # Only the in-memory observation contains page text.
    name, facts = captured[-1]
    assert name == "browser.snapshot"
    assert facts["auth_required"] is True
    assert facts["form_count"] == 0
    assert MARKER not in repr(captured)
    assert PRIVATE_URL not in repr(captured)


async def test_response_sampling_and_failures_never_record_payloads_or_error_messages(
    captured: list[tuple[str, dict[str, object]]],
) -> None:
    browser = handle(Mock(spec=BrowserContext), Mock(spec=Page))
    response = Mock(spec=Response)
    response.body = AsyncMock(return_value=f'{{"access_token":"{MARKER}"}}'.encode())
    entry = ObservedRequest(method="GET", url=PRIVATE_URL)
    await browser._sample(cast(Response, response), entry)  # pyright: ignore[reportPrivateUsage]
    assert entry.response_sample is None
    assert captured[-1][1]["omitted_sensitive"] is True
    request = Mock(spec=Request)
    request.url = PRIVATE_URL
    request.failure = f"net::ERR_TUNNEL_CONNECTION_FAILED {MARKER}"
    browser._on_failed(cast(Request, request))  # pyright: ignore[reportPrivateUsage]
    assert captured[-1][1]["proxy_failed"] is True
    assert MARKER not in repr(captured)


async def test_probe_reports_status_without_source_profile_or_token(
    captured: list[tuple[str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "botai.browser.probe._status",
        AsyncMock(return_value=httpx.Response(200, json={"email": MARKER})),
    )
    state = SessionState(
        origin="https://fakeuni.test", bearer=SecretStr(MARKER), captured_at=datetime.now(UTC)
    )
    assert await probe_ok(
        state,
        {"url": PRIVATE_URL, "expectStatus": 200},
        ("fakeuni.test",),
        egress=cast(Egress, Mock()),
    )
    name, facts = captured[-1]
    assert name == "auth.probe"
    assert facts["status"] == 200
    assert facts["passed"] is True
    assert facts["bearer_present"] is True
    assert MARKER not in repr(captured)
