"""Prompt redaction requires every credential; bounded Vault restoration is separate."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import BrowserContext, Page, Request

from botai.browser.egress import Egress
from botai.browser.gateway import CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.learn.observe import protect_observation
from botai.learn.redact import Redactor
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"


def inventory_browser() -> tuple[PlaywrightHandle, Mock]:
    context = Mock(spec=BrowserContext)
    context.cookies = AsyncMock(return_value=[])
    context.storage_state = AsyncMock(return_value={"origins": []})
    context.pages = []
    handle = PlaywrightHandle(
        gateway=PlaywrightGateway(cast(Egress, Mock()), sandbox=False),
        context=cast(BrowserContext, context),
        page=cast(Page, Mock(spec=Page)),
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test", "api.fakeuni.test"),
        origin=ORIGIN,
        capture=CaptureConfig(),
        clock=FrozenClock(NOW),
        read_only=True,
    )
    return handle, context


@pytest.mark.parametrize("overflow", ["entries", "value", "bytes", "tab"])
async def test_incomplete_inventory_fails_closed_instead_of_redacting_a_truncated_subset(
    overflow: str,
) -> None:
    handle, context = inventory_browser()
    rows = [{"name": "neutral", "value": "opaqueprivatecookie"}]
    if overflow == "entries":
        rows = [{"name": f"pref{i}", "value": "ordinary"} for i in range(64)] + rows
    elif overflow == "value":
        rows[0]["value"] += "x" * 32_768
    elif overflow == "bytes":
        rows = [{"name": str(i), "value": "я" * 16_384} for i in range(8)]
    if overflow == "tab":
        page = Mock(spec=Page)
        page.url = ORIGIN + "/"
        page.evaluate = AsyncMock(return_value={"origin": ORIGIN, "items": rows, "complete": False})
        context.pages = [page]
    else:
        context.storage_state.return_value = {"origins": [{"origin": ORIGIN, "localStorage": rows}]}
    with pytest.raises(BotaiError) as error:
        await protect_observation(handle, Redactor())
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "opaqueprivatecookie" not in str(error.value)


async def test_inventory_preserves_distinct_tab_stores_and_every_sent_bearer_rotation() -> None:
    handle, context = inventory_browser()
    pages = []
    for value in ("opaquetabalpha", "opaquetabbeta"):
        page = Mock(spec=Page)
        page.url = ORIGIN + "/"
        page.evaluate = AsyncMock(
            return_value={
                "origin": ORIGIN,
                "items": [{"name": "neutral", "value": value}],
                "complete": True,
            }
        )
        pages.append(page)
    context.pages = pages
    tokens = ("opaquefirstbearer", "opaquerefreshedbearer", "opaquesecondhostbearer")
    for host, token in zip(
        ("fakeuni.test", "fakeuni.test", "api.fakeuni.test"), tokens, strict=True
    ):
        request = Mock(spec=Request)
        request.is_navigation_request.return_value = False
        request.url, request.method, request.resource_type = f"https://{host}/data", "GET", "fetch"
        request.headers = {"authorization": "Bearer " + token}
        handle._on_request(cast(Request, request))
    redactor = Redactor()
    await protect_observation(handle, redactor)
    for value in (*tokens, "opaquetabalpha", "opaquetabbeta"):
        assert value not in (redactor.text(value) or "")


@pytest.mark.browser
@pytest.mark.parametrize("store", ["localStorage", "sessionStorage"])
async def test_real_browser_inventory_refuses_the_sixty_fifth_storage_entry(
    gateway: PlaywrightGateway,
    store: str,
) -> None:
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test",),
        read_only=True,
    )
    try:
        await handle.goto(ORIGIN + "/news")
        await handle.page.evaluate(
            "store => { const storage = window[store]; storage.clear();"
            " for(let i=0;i<64;i++) storage.setItem('pref'+i,'ordinary');"
            " storage.setItem('neutral','opaqueprivatecookie'); }",
            store,
        )
        with pytest.raises(BotaiError) as error:
            await protect_observation(handle, Redactor())
        assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    finally:
        await handle.close()
