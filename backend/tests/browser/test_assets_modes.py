"""Who may load page assets from other hosts, and what is done about the ones refused
(security.md §4.4, review follow-ups):

* a login: the asset rule; the hosts that served assets are kept with the session of the connection
  (never in `allowHosts`);
* a rendered read: only those hosts; the chat agent's live read: none;
* a sibling host under the cookie domain of the site is refused silently mid-request and put to the
  student afterwards, like any host of the login;
* no shared workers; a legacy referrer policy does not leak the address of a login step; an old
  proxy that does not know `ARM` leaves the page as it was before the allowance.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from botai.browser import assets as assets_module
from botai.browser.assets import AssetGate
from botai.browser.egress import ProxyEgress, _arm_remote  # pyright: ignore[reportPrivateUsage]
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.proxy import EgressProxy
from botai.browser.session import decode_session, encode_session
from botai.contracts.agent_ui import ConfirmBlock
from botai.core import diagnostics as d
from botai.core.browser import SessionState
from botai.core.ids import new_id
from botai.core.vault import SecretKind
from botai.settings import Settings
from tests.browser.conftest import PROXY_SECRET
from tests.browser.test_assets import (  # noqa: F401 - fixtures
    LOGIN_PAGE,
    Opener,
    fresh_stand,
    gate,
    load_script,
    open_browser,
    probes,
)
from tests.browser.test_login import (
    ORIGIN,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
    works,
)
from tests.fakeuni.login import ASSET_HOST, ASSETS, SIBLING_HOST

pytestmark = pytest.mark.browser


def read_records(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in directory.glob("*.jsonl*")
        for line in path.read_text().splitlines()
    ]


def session_with(*hosts: str) -> SessionState:
    return SessionState(origin=ORIGIN, captured_at=datetime.now(UTC), asset_hosts=list(hosts))


async def next_button_visible(handle: PlaywrightHandle) -> bool:
    await asyncio.sleep(0.6)
    return await handle.page.is_visible("#a-next")


# ---------------------------------------------------------------- login: what is kept for reads


async def test_a_login_keeps_the_hosts_that_served_assets_with_its_session_not_in_allow_hosts(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE)
    await probes(handle)
    state = await handle.export_session()
    assert state.asset_hosts == [ASSET_HOST]
    assert handle.allow_hosts == ("fakeuni.test",)
    again = decode_session(encode_session(state))  # what the Vault stores
    assert again.asset_hosts == [ASSET_HOST]


async def test_a_flow_login_stores_the_asset_hosts_separately_from_the_allowlist(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/assets/plain"))
    session, _ = await rig.ensure(Student())
    assert await works(session)
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    assert stored is not None
    assert decode_session(stored.get_secret_value()).asset_hosts == [ASSET_HOST]
    assert rig.store.connections[(rig.user, rig.conn)].allow_hosts == ("fakeuni.test",)
    for preset in rig.store.presets[ORIGIN]:
        assert preset["allowHosts"] == ["fakeuni.test"]


async def test_a_session_may_not_name_anything_but_plain_hosts() -> None:
    for bad in ("Cdn.Test", "cdn.test/x", "cdn test", ""):
        with pytest.raises(ValueError, match="plain lower-case DNS name"):
            session_with(bad)
    with pytest.raises(ValueError, match="at most 16"):
        session_with(*(f"h{i}.test" for i in range(17)))


# ----------------------------------------------------------------------------- reads


async def test_a_rendered_read_loads_assets_only_from_the_hosts_of_the_last_login(
    open_browser: Opener,
) -> None:
    handle = await open_browser(session=session_with(ASSET_HOST), read_only=True, background=True)
    await handle.goto(LOGIN_PAGE)
    await handle.page.wait_for_selector("#a-next", state="visible", timeout=10_000)
    assert list(gate(handle).hosts) == [ASSET_HOST]
    # a host the login never used is not armed, however a page asks
    assert await load_script(handle, f"https://{SIBLING_HOST}/assets/enable.js") == "failed"
    assert gate(handle).counts["asset_unlisted"] == 1
    assert ASSETS.paths(SIBLING_HOST) == []
    # and a read does not add to the list it was given
    assert (await handle.export_session()).asset_hosts == [ASSET_HOST]


async def test_a_rendered_read_without_a_login_that_used_assets_gets_none(
    open_browser: Opener,
) -> None:
    for state in (None, session_with()):
        handle = await open_browser(session=state, read_only=True, background=True)
        await handle.goto(LOGIN_PAGE)
        assert not await next_button_visible(handle)
        assert handle._assets is None  # pyright: ignore[reportPrivateUsage]
        await handle.close()
    assert ASSETS.paths(ASSET_HOST) == []


async def test_the_agents_live_read_never_loads_assets_from_other_hosts(
    open_browser: Opener,
) -> None:
    handle = await open_browser(
        session=session_with(ASSET_HOST), read_only=True, background=True, agent_read=True
    )
    await handle.goto(LOGIN_PAGE)
    assert not await next_button_visible(handle)
    assert handle._assets is None  # pyright: ignore[reportPrivateUsage]
    assert ASSETS.paths(ASSET_HOST) == []
    assert (await handle.export_session()).asset_hosts == [ASSET_HOST]  # (it keeps the list)


# --------------------------------------------------------------------------- sibling hosts


async def test_a_sibling_under_the_cookie_domain_is_put_to_the_student_and_allowed(
    make_rig: Callable[..., Rig],
) -> None:
    """The page sets `Domain=.fakeuni.test` and wants its script from `static.fakeuni.test`: the
    gate refuses (the cookie would go along), the page is dead without it, the student is asked."""
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sibling/login?needed=1"))
    student = Student(approve_hosts=True)
    session, ctx = await rig.ensure(student)

    assert await works(session)
    (confirm,) = [b for b in ctx.blocks if isinstance(b, ConfirmBlock)]
    assert confirm.question.startswith(f"Сайт вуза загружает файлы с {SIBLING_HOST}.")
    assert "://" not in confirm.question
    assert rig.store.connections[(rig.user, rig.conn)].allow_hosts == (
        "fakeuni.test",
        SIBLING_HOST,
    )
    assert "/assets/enable.js" in ASSETS.paths(SIBLING_HOST)  # loaded once it was the site's own


async def test_declining_a_sibling_does_not_stop_a_login_that_works_without_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sibling/login?needed=0"))
    student = Student(approve_hosts=False)
    session, _ = await rig.ensure(student)

    assert await works(session)
    assert len(student.confirmations) == 1  # asked once, not again on the restart
    assert rig.store.connections[(rig.user, rig.conn)].allow_hosts == ("fakeuni.test",)
    assert ASSETS.paths(SIBLING_HOST) == []


# ------------------------------------------------------------------------ small hardenings


async def test_there_are_no_shared_workers(open_browser: Opener) -> None:
    """A shared worker's requests do not pass the routes of the context (the asset gate)."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    found = await handle.page.evaluate(
        """() => {
            const frame = document.createElement('iframe');
            document.body.append(frame);
            let made;
            try {
              new SharedWorker(URL.createObjectURL(new Blob(['onconnect = () => {}'])));
              made = 'created';
            } catch (e) { made = e.name; }
            return {top: typeof SharedWorker, frame: typeof frame.contentWindow.SharedWorker,
                    made, dedicated: typeof Worker};
        }"""
    )
    assert found == {
        "top": "undefined",
        "frame": "undefined",  # not recoverable from a fresh frame either
        "made": "ReferenceError",
        "dedicated": "function",
    }


async def test_a_dedicated_worker_goes_through_the_gate(open_browser: Opener) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    assert await load_script(handle, f"https://{ASSET_HOST}/assets/enable.js") == "loaded"
    await handle.page.evaluate(
        """(host) => new Promise((resolve) => {
            const code = "fetch('https://" + host + "/assets/data', {method: 'POST', body: 'w'})" +
                         ".then(() => postMessage('sent'), () => postMessage('refused'));";
            const worker = new Worker(URL.createObjectURL(new Blob([code])));
            worker.onmessage = (m) => resolve(m.data);
        })""",
        ASSET_HOST,
    )
    await asyncio.sleep(0.3)
    assert [r for r in ASSETS.requests if r["method"] == "POST"] == []
    assert gate(handle).counts["asset_method"] == 1


async def test_a_legacy_referrer_policy_does_not_leak_the_address_of_a_login_step(
    open_browser: Opener,
) -> None:
    """With `unsafe-url` the page's whole address (the code in its query) would go to the CDN.
    Chromium ignores a Referer set through `route`, so such requests are refused instead."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/legacy?code=one-time-code-fixture")
    assert not await next_button_visible(handle)
    assert ASSETS.paths(ASSET_HOST) == []
    assert gate(handle).counts["asset_referer"] >= 2  # the style sheet and the script
    assert ASSET_HOST in handle.blocked_hosts()  # (so it is never learned as a login host)
    # the same page without a query has nothing to leak
    await handle.goto(f"{ORIGIN}/assets/legacy")
    await handle.page.wait_for_selector("#a-next", state="visible", timeout=10_000)


def test_an_asset_that_is_not_https_is_refused_before_a_slot_is_spent() -> None:
    request = Mock()
    request.url = f"http://{ASSET_HOST}/assets/pixel.png"
    request.method = "GET"
    request.resource_type = "image"
    request.headers = {}
    gate_ = AssetGate(
        context=Mock(),
        allow_hosts=["fakeuni.test"],
        suffixes=[],
        cap=4,
        arm=Mock(),
        diagnostic_facts={},
    )
    assert gate_._static_refusal(request) == "asset_scheme"  # pyright: ignore[reportPrivateUsage]
    request.url = f"https://{ASSET_HOST}/assets/pixel.png"
    request.frame.url = f"https://{ASSET_HOST}/assets/document"  # a document of a foreign host
    assert gate_._static_refusal(request) == "asset_frame"  # pyright: ignore[reportPrivateUsage]
    request.frame.url = "https://fakeuni.test/"
    assert gate_._static_refusal(request) is None  # pyright: ignore[reportPrivateUsage]


# ----------------------------------------------------------------------- an old or silent proxy


class OldProxyEgress(ProxyEgress):
    """The proxy of the previous release answers every `ARM` with 405."""

    def __init__(self, proxy: EgressProxy, old_url: str) -> None:
        super().__init__(PROXY_SECRET, proxy=proxy)
        self.old_url = old_url
        self.asked: list[str] = []

    async def arm_asset_host(self, token: str, host: str) -> bool:
        self.asked.append(host)
        return await _arm_remote(self.old_url, token, host)


@pytest.fixture
async def old_proxy() -> AsyncIterator[str]:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 405 Method Not Allowed\r\nAllow: CONNECT\r\nConnection: close\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


async def test_an_old_proxy_that_answers_arm_with_405_leaves_the_page_as_it_was(
    egress: EgressProxy, old_proxy: str
) -> None:
    egress_access = OldProxyEgress(egress, old_proxy)
    gw = PlaywrightGateway(egress_access, sandbox=False, ignore_https_errors=True)
    try:
        handle = await gw.open(
            user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"]
        )
        await handle.goto(LOGIN_PAGE)
        assert not await next_button_visible(handle)  # exactly the page of the incident
        for host in ("id.fakeuni.test", "api.fakeuni.test", "calendar.fakeuni.test", SIBLING_HOST):
            assert await load_script(handle, f"https://{host}/assets/arm.js") == "failed"
        # the failures are counted, and after three the proxy is not asked again
        assert len(egress_access.asked) == 3
        assert not gate(handle).hosts
        assert gate(handle).counts["asset_unavailable"] >= 5
        assert ASSETS.paths(ASSET_HOST) == []
        # nothing else changed: the login host works
        await handle.goto(f"{ORIGIN}/auth/login")
        assert await handle.page.is_visible("#username")
    finally:
        await gw.aclose()


async def test_a_silent_proxy_holds_up_each_new_host_for_the_timeout_not_for_the_others(
    open_browser: Opener, gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(_token: str, _host: str) -> bool:
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(assets_module, "ARM_TIMEOUT_S", 0.4)
    monkeypatch.setattr(assets_module, "MAX_ARM_FAILURES", 99)
    monkeypatch.setattr(gateway._egress, "arm_asset_host", silent)  # pyright: ignore[reportPrivateUsage]
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    started = time.monotonic()
    results = await asyncio.gather(
        *(
            load_script(handle, f"https://{host}/assets/arm.js")
            for host in ("id.fakeuni.test", "api.fakeuni.test", "calendar.fakeuni.test", ASSET_HOST)
        )
    )
    assert results == ["failed"] * 4
    assert time.monotonic() - started < 1.2  # in parallel: one timeout, not four in a row


async def test_requests_to_one_new_host_in_parallel_ask_the_proxy_once(
    egress: EgressProxy,
) -> None:
    class Counting(ProxyEgress):
        asked = 0

        async def arm_asset_host(self, token: str, host: str) -> bool:
            Counting.asked += 1
            await asyncio.sleep(0.1)
            return await super().arm_asset_host(token, host)

    gw = PlaywrightGateway(
        Counting(PROXY_SECRET, proxy=egress), sandbox=False, ignore_https_errors=True
    )
    try:
        handle = await gw.open(
            user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"]
        )
        await handle.goto(f"{ORIGIN}/assets/data")
        loaded = await asyncio.gather(
            *(load_script(handle, f"https://{ASSET_HOST}/assets/arm.js?{i}") for i in range(6))
        )
        assert loaded == ["loaded"] * 6
        assert Counting.asked == 1
    finally:
        await gw.aclose()


# ------------------------------------------------------------------------------ diagnostics


async def test_the_real_diagnostics_vocabulary_keeps_the_asset_facts(
    open_browser: Opener, tmp_path: Path
) -> None:
    """The closed vocabulary drops what it does not know; a patched `diagnostic_event` would not
    notice. Go through the real writer and read the file."""
    d.configure_diagnostics(
        Settings(  # type: ignore[arg-type]
            _env_file=None, botai_diagnostics=True, botai_diagnostics_dir=str(tmp_path)
        )
    )
    try:
        handle = await open_browser()
        await handle.goto(LOGIN_PAGE)
        await probes(handle)
        await handle.page.wait_for_load_state("networkidle")
        await handle.close()
    finally:
        d.configure_diagnostics(
            Settings(  # type: ignore[arg-type]
                _env_file=None, botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path)
            )
        )
    records = read_records(tmp_path)
    by_name = lambda name: [r for r in records if r.get("event") == name]  # noqa: E731
    (allowed,) = by_name("browser.asset_host_allowed")
    assert allowed["asset_host_count"] == 1
    assert allowed["asset_host_cap"] == 16
    assert allowed["resource_type"] in {"stylesheet", "script"}
    assert len(allowed["host_hash"]) == 16
    refused = by_name("browser.asset_refused")
    assert {r["reason"] for r in refused} >= {"asset_type", "asset_method"}
    assert {r["resource_type"] for r in refused} >= {"xhr", "fetch"}
    (summary,) = by_name("browser.assets")
    assert summary["asset_host_count"] == 1
    assert summary["rejected_count"] == len(refused)
    assert by_name("egress.grant")[-1]["asset_host_cap"] == 16
    assert {r["reason"] for r in by_name("egress.decision")} >= {"asset_armed", "ok"}
    text = json.dumps(records)
    for private in (ASSET_HOST, "/assets/", "login.js"):
        assert private not in text
