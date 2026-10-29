"""A cookie-only login cannot teach future students to discard observed SPA bearer tokens."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from botai.browser import login as login_module
from botai.browser.egress import Egress
from botai.browser.gateway import CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.browser.presets import ConnectionInfo, InMemoryLoginStore, Recorder, build_preset
from botai.core.browser import (
    ObservedRequest,
    PageSnapshot,
    SessionBearer,
    SessionCookie,
    SessionState,
)
from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.vault import InMemoryVault
from tests.fakeuni.login import SPA_JWT
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
API_ORIGIN = "https://api.fakeuni.test"


def preset(state: SessionState) -> dict[str, Any]:
    return build_preset(
        origin=ORIGIN,
        start_url=ORIGIN + "/auth/login",
        hosts=("fakeuni.test", "api.fakeuni.test"),
        recorder=Recorder(),
        session=state,
        probe=None,
        final_url=ORIGIN + "/app",
        now=NOW,
    )


def cookie_only() -> SessionState:
    return SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        cookies=[
            SessionCookie(
                name="sid", value=SecretStr("private-cookie-fixture"), domain="fakeuni.test"
            )
        ],
    )


def test_generated_cookie_only_preset_keeps_observed_header_capture_available() -> None:
    learned = preset(cookie_only())
    assert learned["capture"]["bearerFrom"] == "authorizationHeader"
    assert "private-cookie-fixture" not in json.dumps(learned)
    assert "bearerKey" not in learned["capture"]


def test_api_only_scoped_bearer_never_becomes_a_value_or_a_storage_key_in_the_preset() -> None:
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer_scopes=[
            SessionBearer(
                origin=API_ORIGIN, token=SecretStr("private-api-fixture"), captured_at=NOW
            )
        ],
    )
    learned = preset(state)
    assert state.bearer is None
    assert learned["capture"]["bearerFrom"] == "authorizationHeader"
    assert "private-api-fixture" not in json.dumps(learned)
    assert "bearerKey" not in learned["capture"]


@pytest.mark.browser
@pytest.mark.parametrize("explicit_none", [False, True])
async def test_cookie_only_preset_captures_later_browser_bearer_unless_explicitly_disabled(
    gateway: PlaywrightGateway, explicit_none: bool
) -> None:
    state = cookie_only()
    learned = preset(state)
    capture = CaptureConfig(
        bearer_from="none" if explicit_none else learned["capture"]["bearerFrom"]
    )
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        session=state,
        capture=capture,
    )
    try:
        await handle.goto(ORIGIN + "/spa")
        await handle.page.wait_for_function(
            "document.getElementById('out').textContent.includes('ok')"
        )
        refreshed = await handle.export_session()
        if explicit_none:
            assert refreshed.bearer is None
            assert refreshed.bearer_scopes == []
        else:
            assert refreshed.bearer == SecretStr(SPA_JWT)
            assert [(scope.origin, scope.token) for scope in refreshed.bearer_scopes] == [
                (ORIGIN, SecretStr(SPA_JWT))
            ]
    finally:
        await handle.close()


async def test_login_finishes_with_an_api_scoped_bearer_after_pending_capture_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        bearer_scopes=[
            SessionBearer(
                origin=API_ORIGIN, token=SecretStr("private-api-fixture"), captured_at=NOW
            )
        ],
    )
    capture_finished = False

    async def network() -> list[ObservedRequest]:
        nonlocal capture_finished
        capture_finished = True
        return []

    async def export() -> SessionState:
        assert capture_finished, "export must include the pending browser network credentials"
        return state

    handle = Mock(spec=PlaywrightHandle)
    handle.allow_hosts = ("fakeuni.test", "api.fakeuni.test")
    handle.network_log = AsyncMock(side_effect=network)
    handle.export_session = AsyncMock(side_effect=export)
    flow = login_module.LoginFlow(
        gateway=cast(PlaywrightGateway, Mock()),
        vault=InMemoryVault(),
        store=InMemoryLoginStore(),
        egress=cast(Egress, Mock()),
        clock=FrozenClock(NOW),
    )
    ctx = InMemoryJobContext()
    run = login_module._Run(
        job=ctx,
        user_id=ctx.user_id,
        connection_id="fixture-connection",
        info=ConnectionInfo(ORIGIN, handle.allow_hosts),
        preset=None,
        hosts=list(handle.allow_hosts),
        start_url=ORIGIN + "/auth/login",
        handle=cast(PlaywrightHandle, handle),
        creds=login_module._Creds(None, None),
        submitted=1,
        probe={"url": API_ORIGIN + "/me", "expectStatus": 200},
    )
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(login_module, "probe_ok", probe)
    result = await flow._try_finish(run, PageSnapshot(url=ORIGIN + "/app", text="Мои курсы"))
    assert result is state
    probe.assert_awaited_once()
    assert ctx.blocks == []
