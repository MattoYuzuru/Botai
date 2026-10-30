"""Service workers (security.md §4.4): a registered worker answers for the page and sends requests
of its own, so none may exist.

Playwright's `service_workers="block"` only stubs `navigator.serviceWorker.register`; the prototype
method still registers (verified on Chromium 1243, see the control below), and Chromium has no
switch against workers. What every registration and update has is a fetch of the worker's script
with the header `Service-Worker: script`: the gateway refuses that fetch, and without a script
there is no worker.

The fixture certificate is made trusted to Chromium by its public key (`trusted_spki`, tests only):
with `ignore_https_errors` a worker's script cannot be loaded at all, and a negative test would
pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from playwright.async_api import Route, async_playwright

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.core.ids import new_id
from tests.browser.conftest import Tls
from tests.browser.test_assets import fresh_stand, gate, load_script  # noqa: F401 - fixture
from tests.browser.test_login import ORIGIN
from tests.fakeuni.login import ASSET_HOST, ASSETS

pytestmark = pytest.mark.browser

REGISTER_BY_PROTOTYPE = """async () => {
    try {
        const registration = await ServiceWorkerContainer.prototype.register.call(
            navigator.serviceWorker, '/assets/sw.js', {scope: '/assets/'});
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const worker = registration.active || registration.installing || registration.waiting;
        return 'registered ' + (worker ? worker.state : 'without a worker');
    } catch (e) { return 'refused ' + e.name; }
}"""
REGISTER_BY_METHOD = """async () => {
    try {
        await navigator.serviceWorker.register('/assets/sw.js', {scope: '/assets/'});
        return 'registered';
    } catch (e) { return 'refused ' + e.name; }
}"""
COUNT = "async () => (await navigator.serviceWorker.getRegistrations()).length"


@pytest.fixture(scope="session")
def spki(tls: Tls) -> str:
    cert = x509.load_pem_x509_certificate(tls.cert.read_bytes())
    der = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


@pytest.fixture
async def trusting_gateway(
    egress_access: ProxyEgress, spki: str
) -> AsyncIterator[Callable[[], Awaitable[PlaywrightHandle]]]:
    gw = PlaywrightGateway(egress_access, sandbox=False, trusted_spki=[spki])

    async def open_handle() -> PlaywrightHandle:
        return await gw.open(user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"])

    yield open_handle
    await gw.aclose()


async def test_control_an_unprotected_chromium_registers_a_worker_even_with_playwrights_block(
    fakeuni_port: int, spki: str
) -> None:
    """Without this the tests below could pass because nothing would register anyway."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=[
                "--host-resolver-rules=MAP fakeuni.test 127.0.0.1",
                f"--ignore-certificate-errors-spki-list={spki}",
            ],
        )
        try:
            context = await browser.new_context(service_workers="block")
            page = await context.new_page()
            await page.goto(f"https://fakeuni.test:{fakeuni_port}/assets/data")
            assert await page.evaluate(REGISTER_BY_PROTOTYPE) == "registered activated"
            assert await page.evaluate(COUNT) == 1
        finally:
            await browser.close()


async def test_the_gateway_registers_no_service_worker_by_any_route(
    trusting_gateway: Callable[[], Awaitable[PlaywrightHandle]],
) -> None:
    handle = await trusting_gateway()
    await handle.goto(f"{ORIGIN}/assets/data")
    assert await handle.page.evaluate(REGISTER_BY_PROTOTYPE) == "refused TypeError"
    assert await handle.page.evaluate(REGISTER_BY_METHOD) in {"refused TypeError", "registered"}
    assert await handle.page.evaluate(COUNT) == 0  # (the stub of "block" registers nothing)
    assert handle.context.service_workers == []
    assert "/assets/sw.js" not in ASSETS.paths("fakeuni.test")  # the script was never fetched
    await handle.close()


async def test_control_with_the_refusal_taken_out_a_worker_registers_and_its_requests_are_routed(
    trusting_gateway: Callable[[], Awaitable[PlaywrightHandle]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway proper, minus the refusal: the worker registers (so the test above proves the
    refusal) — and what it posts to an armed host meets the asset gate like a page's would."""

    def let_through(_facts: dict[str, object]) -> Callable[[Route], Awaitable[None]]:
        async def fallback(route: Route) -> None:
            await route.fallback()

        return fallback

    monkeypatch.setattr("botai.browser.gateway.service_worker_refuser", let_through)
    handle = await trusting_gateway()
    await handle.goto(f"{ORIGIN}/assets/data")
    assert await load_script(handle, f"https://{ASSET_HOST}/assets/enable.js") == "loaded"
    assert await handle.page.evaluate(REGISTER_BY_PROTOTYPE) == "registered activated"
    await asyncio.sleep(0.5)
    posts: list[Any] = [r for r in ASSETS.requests if r["method"] == "POST"]
    assert posts == []  # the worker's POST was refused by the gate: its requests are routed
    assert gate(handle).counts["asset_method"] >= 1
    await handle.close()
