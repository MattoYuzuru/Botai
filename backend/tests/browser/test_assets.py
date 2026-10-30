"""Page assets from other hosts (security.md §4.3), with real Chromium on the fakeuni stand.

An identity provider or a cabinet loads its style sheets and scripts from a CDN or a static host.
The stand serves a login page whose form is built by a script from `cdn.fakeuni-assets.test` (not a
subdomain of the origin), so the page is an empty hidden box unless that host is reachable. What the
stand records per Host is the evidence: a refused request is the one that never shows up there.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.proxy import EgressProxy
from botai.contracts.agent_ui import ConfirmBlock
from botai.core.browser import SessionCookie, SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from tests.browser.conftest import PROXY_SECRET
from tests.browser.test_login import (
    ORIGIN,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
    works,
)
from tests.fakeuni import data
from tests.fakeuni.login import ASSET_HOST, ASSETS, SIBLING_HOST

pytestmark = pytest.mark.browser

Opener = Callable[..., Awaitable[PlaywrightHandle]]
LOGIN_PAGE = f"{ORIGIN}/assets/login"
PROBES = "document.getElementById('a-probes').dataset.done"


@pytest.fixture(autouse=True)
def fresh_stand() -> None:
    ASSETS.reset()


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    seen: list[tuple[str, dict[str, object]]] = []

    def record(event: str, **facts: object) -> None:
        seen.append((event, facts))

    monkeypatch.setattr("botai.browser.assets.diagnostic_event", record)
    return seen


@pytest.fixture
async def open_browser(gateway: PlaywrightGateway) -> AsyncIterator[Opener]:
    opened: list[PlaywrightHandle] = []

    async def _open(hosts: tuple[str, ...] = ("fakeuni.test",), **kwargs: Any) -> PlaywrightHandle:
        kwargs.setdefault("user_id", new_id())
        handle = await gateway.open(connection_id=new_id(), allow_hosts=hosts, **kwargs)
        opened.append(handle)
        return handle

    yield _open
    for handle in opened:
        await handle.close()


async def probes(handle: PlaywrightHandle) -> dict[str, str]:
    """What the page's own script found out about the asset host (after all its attempts ended)."""
    await handle.page.wait_for_function(f"!!{PROBES}", timeout=10_000)
    return json.loads(await handle.page.evaluate(PROBES))


async def load_script(handle: PlaywrightHandle, url: str) -> str:
    return await handle.page.evaluate(
        """(url) => new Promise((resolve) => {
            const node = document.createElement('script');
            node.src = url;
            node.onload = () => resolve('loaded');
            node.onerror = () => resolve('failed');
            document.head.append(node);
        })""",
        url,
    )


def gate(handle: PlaywrightHandle) -> Any:
    assert handle._assets is not None  # pyright: ignore[reportPrivateUsage]
    return handle._assets  # pyright: ignore[reportPrivateUsage]


# ------------------------------------------------------------------------- the page works


async def test_a_page_with_assets_from_another_host_renders_and_its_script_works(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE)
    await handle.page.wait_for_selector("#a-next", state="visible", timeout=10_000)
    # the style sheet applied: the inline style hides #app, the sheet from the CDN shows it
    assert await handle.page.evaluate("getComputedStyle(document.body).backgroundColor") == (
        "rgb(1, 2, 3)"
    )
    assert not await handle.page.is_visible("#a-password")

    snap = await handle.click("#a-next")  # «Далее»: a handler the CDN script installed
    assert await handle.page.inner_text("#a-state") == "step2"
    assert await handle.page.is_visible("#a-password")
    assert any(f.input_type == "password" for form in snap.forms for f in form.fields)

    assert set(ASSETS.paths(ASSET_HOST)) >= {
        "/assets/login.css",
        "/assets/login.js",
        "/assets/pixel.png",
    }
    assert list(gate(handle).hosts) == [ASSET_HOST]


async def test_without_an_allowance_the_same_page_stays_empty(
    open_browser: Opener, gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: an exact-host allowlist leaves a CDN-built page without CSS and JS."""
    monkeypatch.setattr(gateway, "_asset_hosts", 0)
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE)
    await asyncio.sleep(0.5)
    assert not await handle.page.is_visible("#a-next")
    assert ASSETS.paths(ASSET_HOST) == []
    assert handle._assets is None  # pyright: ignore[reportPrivateUsage]


# ------------------------------------------------------- what an asset host must not be used for


async def test_xhr_fetch_post_beacon_and_socket_to_the_asset_host_are_blocked(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE)
    assert await probes(handle) == {
        "xhr": "blocked",
        "fetch": "blocked",
        "post": "blocked",
        "beacon": "sent",  # queued by the browser; whether it was sent is the server's word
        "socket": "closed",
    }
    await asyncio.sleep(0.3)  # a beacon is sent in the background
    assert set(ASSETS.paths(ASSET_HOST)) <= {
        "/assets/login.css",
        "/assets/login.js",
        "/assets/pixel.png",
    }
    counts = gate(handle).counts
    assert counts["asset_type"] >= 3  # xhr, fetch, socket (the beacon may be an `other` as well)
    assert counts["asset_method"] >= 1  # the POST

    # the stand does record such requests when they are let through (same origin as the page)
    await handle.page.evaluate("fetch('/assets/data')")
    assert "/assets/data" in ASSETS.paths("fakeuni.test")


async def test_a_navigation_to_the_asset_host_still_asks_for_approval(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE)
    await handle.page.wait_for_selector("#a-cdn", state="visible", timeout=10_000)
    await handle.page.click("#a-cdn", no_wait_after=True)
    for _ in range(100):
        if handle.navigation_failure() is not None:
            break
        await asyncio.sleep(0.05)
    failure = handle.navigation_failure()
    assert failure is not None
    assert failure.code == ErrorCode.EGRESS_BLOCKED
    assert failure.extra == {"host": ASSET_HOST}  # what LoginFlow puts to the student
    assert "/assets/document" not in ASSETS.paths(ASSET_HOST)
    assert ASSET_HOST in handle.blocked_hosts()
    assert handle.allow_hosts == ("fakeuni.test",)


async def test_a_document_the_asset_host_serves_for_a_goto_is_refused_before_the_browser(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    with pytest.raises(BotaiError) as exc:
        await handle.goto(f"https://{ASSET_HOST}/assets/document")
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert ASSETS.paths(ASSET_HOST) == []


async def test_a_server_redirect_to_an_unarmed_host_is_still_refused_by_the_proxy(
    open_browser: Opener,
) -> None:
    """Playwright's routes do not see redirect hops: only the proxy can refuse this one."""
    handle = await open_browser()
    with pytest.raises(BotaiError) as exc:
        await handle.goto(f"{ORIGIN}/sso/start")
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra == {"host": "id.fakeuni.test"}
    assert handle._assets is not None  # pyright: ignore[reportPrivateUsage]
    assert not gate(handle).hosts  # nothing was armed on the way


# -------------------------------------------------------------------------- no credentials


async def test_the_sources_cookies_and_authorization_never_reach_the_asset_host(
    open_browser: Opener,
) -> None:
    state = SessionState(
        origin=ORIGIN,
        cookies=[
            SessionCookie(
                name="SRC_SESSION", value=SecretStr("src-cookie-fixture"), domain="fakeuni.test"
            )
        ],
        bearer=SecretStr("src-bearer-fixture-0123456789"),
        captured_at=datetime.now(UTC),
    )
    handle = await open_browser(session=state)
    await handle.goto(LOGIN_PAGE + "?code=one-time-code-fixture")
    await probes(handle)
    await handle.page.wait_for_load_state("networkidle")

    # control: the session is live, so the page of the source itself did carry both
    (page,) = [r for r in ASSETS.to("fakeuni.test") if r["path"] == "/assets/login"]
    assert "src-cookie-fixture" in str(page["cookie"])
    assert page["authorization"] == "Bearer src-bearer-fixture-0123456789"

    cdn = ASSETS.to(ASSET_HOST)
    assert {r["path"] for r in cdn} >= {"/assets/login.css", "/assets/login.js"}
    for request in cdn:
        assert "src-cookie-fixture" not in str(request["cookie"])
        assert request["authorization"] is None
    # with the browser's default policy the CDN is told the origin only, not the code in the query
    (pixel,) = [r for r in cdn if r["path"] == "/assets/pixel.png"]
    assert pixel["referer"] == "https://fakeuni.test/"


async def test_a_sibling_inside_the_cookie_domain_of_the_source_is_not_an_asset_host(
    open_browser: Opener,
) -> None:
    """`Domain=.fakeuni.test` would carry the session to `static.fakeuni.test`: refused."""
    script = f"https://{SIBLING_HOST}/assets/enable.js"
    plain = await open_browser()
    await plain.goto(f"{ORIGIN}/assets/data")
    assert await load_script(plain, script) == "loaded"  # no cookie of the source: an asset host
    assert SIBLING_HOST in gate(plain).hosts
    ASSETS.reset()

    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await handle.context.add_cookies(
        [
            {
                "name": "SRC_DOMAIN_SESSION",
                "value": "domain-cookie-fixture",
                "domain": ".fakeuni.test",
                "path": "/",
                "secure": True,
            }
        ]
    )
    assert await load_script(handle, script) == "failed"
    assert ASSETS.paths(SIBLING_HOST) == []
    assert gate(handle).counts["asset_credentials"] == 1
    assert SIBLING_HOST not in gate(handle).hosts


# ------------------------------------------------------------------------ session and hosts


async def test_the_exported_session_and_the_visited_hosts_leave_the_asset_host_out(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/auth/login")
    await handle.fill("#username", data.USERNAME)
    await handle.fill("#password", data.PASSWORD)
    await handle.click("#kc-login")
    await handle.fill("#otp", data.OTP)
    await handle.click("#kc-otp-submit")
    await handle.goto(LOGIN_PAGE)
    await probes(handle)
    await handle.page.wait_for_load_state("networkidle")

    # the CDN really set a cookie of its own, and it is in the jar ...
    jar = await handle.context.cookies()
    assert any(c.get("name") == "CDN_TRACK" and c.get("domain") == ASSET_HOST for c in jar)
    # ... but not in anything that is stored
    state = await handle.export_session()
    assert {c.domain for c in state.cookies} == {"fakeuni.test"}
    assert "CDN_TRACK" not in {c.name for c in state.cookies}
    assert ASSET_HOST not in state.model_dump_json()
    assert ASSET_HOST not in handle.visited_hosts()
    assert ASSET_HOST in gate(handle).hosts
    assert handle.allow_hosts == ("fakeuni.test",)


async def test_only_a_few_hosts_may_serve_assets(
    open_browser: Opener, gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_asset_hosts", 1)
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    assert await load_script(handle, f"https://{ASSET_HOST}/assets/enable.js") == "loaded"
    assert await load_script(handle, f"https://{SIBLING_HOST}/assets/enable.js") == "failed"
    assert gate(handle).counts["asset_cap"] == 1
    assert ASSETS.paths(SIBLING_HOST) == []


@pytest.mark.parametrize("target", ["https://127.0.0.1:8443/x.js", "https://[::1]/x.js"])
async def test_an_ip_literal_is_never_an_asset_host(open_browser: Opener, target: str) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    assert await load_script(handle, target) == "failed"
    assert gate(handle).counts["asset_unavailable"] == 1
    assert not gate(handle).hosts


# ---------------------------------------------------------------------------- diagnostics


async def test_diagnostics_count_asset_hosts_by_hash_only(
    open_browser: Opener, events: list[tuple[str, dict[str, object]]]
) -> None:
    handle = await open_browser()
    await handle.goto(LOGIN_PAGE + "?code=private-query-marker")
    await probes(handle)
    await handle.page.wait_for_load_state("networkidle")
    await handle.close()

    allowed = [facts for name, facts in events if name == "browser.asset_host_allowed"]
    assert len(allowed) == 1
    assert allowed[0]["host_hash"] == hashlib.sha256(ASSET_HOST.encode()).hexdigest()[:16]
    assert allowed[0]["asset_host_count"] == 1
    assert allowed[0]["asset_host_cap"] == 16
    refused = [facts for name, facts in events if name == "browser.asset_refused"]
    assert {str(f["reason"]) for f in refused} >= {"asset_type", "asset_method"}
    (summary,) = [facts for name, facts in events if name == "browser.assets"]
    assert summary["asset_host_count"] == 1
    assert summary["request_count"] >= 3  # style sheet, script, image
    assert summary["rejected_count"] == len(refused)
    text = repr(events)
    for private in (ASSET_HOST, "private-query-marker", "/assets/", "login.js"):
        assert private not in text


# ----------------------------------------------------- the login flow around an SSO host


async def test_sso_host_approved_by_the_student_renders_with_its_cdn_assets(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> None:
    """The incident: the student approves the identity host, and its page needs a CDN script (the
    submit button is disabled until it runs). The login completes; the CDN stays out of the preset
    and the session."""
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso-assets/start"))
    student = Student(approve_hosts=True)
    ctx = InMemoryJobContext(user_id=rig.user, connection_id=rig.conn, responder=student)
    session = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)

    assert await works(session)
    (confirm,) = [b for b in ctx.blocks if isinstance(b, ConfirmBlock)]
    assert "id.fakeuni.test" in confirm.question
    assert ASSET_HOST not in confirm.question  # the CDN was never put to the student
    assert {"/assets/login.css", "/assets/enable.js"} <= set(ASSETS.paths(ASSET_HOST))

    for preset in rig.store.presets[ORIGIN]:
        assert ASSET_HOST not in json.dumps(preset)
    info = rig.store.connections[(rig.user, rig.conn)]
    assert info.allow_hosts == ("fakeuni.test", "id.fakeuni.test")
    assert ASSET_HOST not in session.model_dump_json()
    assert "CDN_TRACK" not in {c.name for c in session.cookies}


async def test_a_first_login_on_a_page_with_cdn_assets_does_not_widen_the_allowlist(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/assets/plain"))
    student = Student()
    session, _ = await rig.ensure(student)
    assert await works(session)
    assert {"/assets/login.css", "/assets/enable.js"} <= set(ASSETS.paths(ASSET_HOST))
    for preset in rig.store.presets[ORIGIN]:
        assert preset["allowHosts"] == ["fakeuni.test"]
        assert ASSET_HOST not in json.dumps(preset)
    assert ASSET_HOST not in session.model_dump_json()


async def test_a_proxy_in_another_process_is_armed_over_the_wire(egress: EgressProxy) -> None:
    """Production runs the proxy in its own container: `ARM` crosses a socket there."""
    gw = PlaywrightGateway(
        ProxyEgress(PROXY_SECRET, server_url=egress.url), sandbox=False, ignore_https_errors=True
    )
    try:
        handle = await gw.open(
            user_id=new_id(), connection_id=new_id(), allow_hosts=["fakeuni.test"]
        )
        await handle.goto(LOGIN_PAGE)
        await handle.page.wait_for_selector("#a-next", state="visible", timeout=10_000)
        assert list(gate(handle).hosts) == [ASSET_HOST]
        assert egress.stats["allow:asset_armed"] >= 1
    finally:
        await gw.aclose()
