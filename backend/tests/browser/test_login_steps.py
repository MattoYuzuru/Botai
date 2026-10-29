"""A login made of user steps (e-mail, password, code) behind a read-only entry guard.

The fixture is a Keycloak-like JS login (`tests/fakeuni/login.py`, `/cu/...`): the e-mail page has no
`action`, its script checks the login with a POST to another host and path, sets the action to
`/cu/login-actions/authenticate?session_code=…` and submits; the page also posts telemetry nobody
asked for. Every value the student gives opens one window for that step; nothing else may post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from botai.browser import gateway
from botai.browser.egress import ProxyEgress
from botai.browser.gateway import (
    PlaywrightGateway,
    PlaywrightHandle,
    guard_refusal_error,
    map_navigation_error,
)
from botai.browser.login import LoginFlow
from botai.contracts.agent_ui import InputBlock, LoginBlock, TextBlock
from botai.core import diagnostics
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobCancelled
from botai.core.vault import SecretKind
from botai.settings import Settings
from tests.browser.test_login import (
    FAST,
    ORIGIN,
    FakeLink,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
    works,
)
from tests.fakeuni import data, login

pytestmark = pytest.mark.browser

CU_ENTRY = f"{ORIGIN}/cu/app"
CU_HOSTS = ("fakeuni.test", "id.fakeuni.test")


@pytest.fixture(autouse=True)
def fresh_cu() -> None:
    login.cu_reset()


@pytest.fixture
def new_rig(make_rig: Callable[..., Rig]) -> Callable[..., Rig]:  # noqa: F811
    return make_rig


async def open_entry(
    rig: Rig, *, hosts: tuple[str, ...] = ("fakeuni.test",), suffixes: tuple[str, ...] = ()
) -> PlaywrightHandle:
    return await rig.gateway.open(
        user_id=rig.user,
        connection_id=rig.conn,
        allow_hosts=hosts,
        origin=ORIGIN,
        suffixes=suffixes,
    )


def know_cu_hosts(rig: Rig) -> None:
    """A learned preset names the login hosts, so a first login's subdomain rule is not in play."""
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/", hosts=CU_HOSTS))


def record_guard(handle: PlaywrightHandle, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def emit(event: str, **facts: object) -> None:
        events.append({"event": event, **facts})

    monkeypatch.setattr(handle, "_diagnostic", emit)
    return events


def trace_rows(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in directory.glob("*.jsonl*")
        for line in path.read_text().splitlines()
    ]


def leaked_files(directory: Path, markers: tuple[bytes, ...]) -> list[Path]:
    """The browser's own temporary files (profile, caches) that hold a marker."""
    return [
        path
        for path in directory.rglob("*")
        if path.is_file() and any(marker in path.read_bytes() for marker in markers)
    ]


def refusals(events: list[dict[str, Any]]) -> list[str]:
    return [e["reason"] for e in events if e["event"] == "browser.auth_permit_refused"]


POST_JS = "u => fetch(u, {method: 'POST'}).then(() => true).catch(() => false)"


# ------------------------------------------------------------------ a whole login, by steps


async def test_multi_step_js_login_passes_with_native_fields_and_stores_the_password(
    new_rig: Callable[..., Rig],
) -> None:
    rig = new_rig()
    student = Student()
    ctx = rig.ctx(student)
    state = await rig.flow.ensure_for_entry(
        ctx, user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )

    assert await works(state)
    inputs = [b for b in ctx.blocks if isinstance(b, InputBlock)]
    assert [(b.input_kind, b.secret, b.autocomplete) for b in inputs] == [
        ("email", False, "username"),
        ("password", True, "current-password"),
        ("code", True, "one-time-code"),
    ]
    assert len(ctx.secrets_requested) == 2
    # the three submissions reached the site, the page's own login check too, once
    assert login.CU.submissions == ["email", "password", "otp"]
    assert login.CU.checks == 1
    # what nobody asked for (telemetry of the login page and of the cabinet) never left the browser
    assert login.CU.telemetry == 0
    assert not rig.screens.links
    assert await rig.vault.has(rig.user, rig.conn, SecretKind.PASSWORD)
    assert await rig.vault.has(rig.user, rig.conn, SecretKind.LOGIN)


async def test_stored_password_walks_the_multi_step_form_and_only_the_code_is_asked(
    new_rig: Callable[..., Rig],
) -> None:
    rig = new_rig()
    first = await rig.flow.ensure_for_entry(
        rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )
    login.expire_all_sessions()
    login.CU.reset()

    again = Student(username="never used", password="never used")
    ctx = rig.ctx(again)
    second = await rig.flow.ensure_for_entry(
        ctx, user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )

    assert await works(second)
    assert second.cookies[0].value != first.cookies[0].value
    assert again.asked == [("code", "one-time-code")]  # e-mail and password came from the Vault
    assert login.CU.submissions == ["email", "password", "otp"]
    assert login.CU.telemetry == 0
    assert len(ctx.secrets_requested) == 1


async def test_a_refused_code_is_asked_again_on_the_multi_step_form(
    new_rig: Callable[..., Rig],
) -> None:
    rig = new_rig()
    student = Student(wrong_otps=1)
    state = await rig.flow.ensure_for_entry(
        rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )
    assert await works(state)
    assert student.count("code") == 2
    assert login.CU.submissions == ["email", "password", "otp", "otp"]


async def test_a_refused_password_is_asked_again_on_the_multi_step_form(
    new_rig: Callable[..., Rig],
) -> None:
    rig = new_rig()
    student = Student(wrong_passwords=1)
    state = await rig.flow.ensure_for_entry(
        rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )
    assert await works(state)
    assert student.count("password") == 2
    assert login.CU.submissions == ["email", "password", "password", "otp"]


async def test_no_secret_reaches_events_logs_preset_vault_or_disk_on_the_multi_step_form(
    egress_access: ProxyEgress,
    new_rig: Callable[..., Rig],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_gateway = PlaywrightGateway(
        egress_access, sandbox=False, ignore_https_errors=True, tmp_dir=tmp_path / "chromium"
    )
    try:
        rig = new_rig(gw=private_gateway)
        ctx = rig.ctx(Student())
        with caplog.at_level(logging.DEBUG):
            state = await rig.flow.ensure_for_entry(
                ctx, user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
            )
        assert await works(state)
        markers = (data.PASSWORD.encode(), data.OTP.encode())
        blocks = [b.model_dump(mode="json", by_alias=True) for b in ctx.blocks]
        assert not any(m in json.dumps(blocks).encode() for m in markers)
        logs = "\n".join(r.getMessage() for r in caplog.records).encode()
        assert not any(m in logs for m in markers)
        assert not any(m in json.dumps(rig.store.presets).encode() for m in markers)
        assert not any(m in repr(ctx.statuses).encode() for m in markers)
        for sealed in rig.vault.raw().values():
            blob = sealed.ciphertext + sealed.wrapped_dek + sealed.nonce
            assert not any(m in blob for m in markers)
    finally:
        await private_gateway.aclose()
    leaked = [
        str(p.relative_to(tmp_path))
        for p in leaked_files(tmp_path / "chromium", (data.PASSWORD.encode(), data.OTP.encode()))
    ]
    assert leaked == []


async def test_every_guard_decision_of_the_login_is_traced_without_values(
    new_rig: Callable[..., Rig], tmp_path: Path
) -> None:
    diagnostics.configure_diagnostics(
        Settings(botai_diagnostics=True, botai_diagnostics_dir=str(tmp_path))
    )
    try:
        rig = new_rig()
        await rig.flow.ensure_for_entry(
            rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
        )
    finally:
        diagnostics.configure_diagnostics(
            Settings(botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path))
        )
    rows = trace_rows(tmp_path)
    windows = [
        r for r in rows if r["event"] == "browser.auth_permit" and r.get("phase") == "request"
    ]
    posts = [
        r for r in rows if r["event"] == "browser.auth_permit" and r.get("phase") == "response"
    ]
    refused = [r for r in rows if r["event"] == "browser.auth_permit_refused"]
    assert len(windows) == 3  # one window for each of the e-mail, the password and the code
    assert all(r["window_ms"] == 15_000 for r in windows)
    # the e-mail step posted its login check (an XHR) and then the form; each later step the form
    assert sorted((r["navigation"], r["post_count"]) for r in posts) == [
        (False, 1),
        (True, 1),
        (True, 1),
        (True, 2),
    ]
    # telemetry, with no step behind it (the cabinet's, while its entry is verified, is read-only)
    assert {r["reason"] for r in refused} == {"auth_no_permit", "auth_read_only"}
    dump = json.dumps(rows, ensure_ascii=False)
    for private in (
        data.PASSWORD,
        data.OTP,
        data.USERNAME,
        "session_code",
        "tab_id",
        "client_data",
    ):
        assert private not in dump


# ------------------------------------------------------------ the guard around one step


async def test_a_post_without_a_user_step_is_refused_and_never_reaches_the_site(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig, hosts=CU_HOSTS)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(CU_ENTRY)  # lands on the identity host, whose page posts telemetry
        assert (
            await handle.page.evaluate(POST_JS, "https://fakeuni.test/cu/api/check-login") is False
        )
        assert login.CU.telemetry == login.CU.checks == 0
        # the page's own load-time POST (the entry is still being verified) and this one
        assert refusals(events)[0] == "auth_read_only"
        assert refusals(events)[-1] == "auth_no_permit"
    finally:
        await handle.close()


async def test_the_page_checks_the_login_and_submits_inside_the_step_whatever_its_action(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig, hosts=CU_HOSTS)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            snap = await handle.goto(CU_ENTRY)
        (form,) = snap.forms
        assert form.action is None  # no action at all: a script sets it when it submits
        async with handle.auth_step(fields=["#cu-login"]):
            await handle.fill("#cu-login", data.USERNAME)
            await handle.click("#cu-next")
        assert login.CU.checks == 1
        assert login.CU.submissions == ["email"]
        assert "password" in handle.page.url or await handle.page.locator("#cu-password").count()
        assert "/cu/login-actions/authenticate" in handle.page.url
        assert login.CU.telemetry == 0
        assert not handle._auth_windows
        assert "auth_host_not_allowed" not in refusals(events)
    finally:
        await handle.close()


async def test_each_step_has_its_own_window(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig, hosts=CU_HOSTS)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(CU_ENTRY)
        async with handle.auth_step(fields=["#cu-login"]):
            await handle.fill("#cu-login", data.USERNAME)
            await handle.click("#cu-next")
        # the e-mail step is over: its window does not serve the next page
        assert await handle.page.evaluate(POST_JS, "https://fakeuni.test/cu/api/telemetry") is False
        assert login.CU.telemetry == 0
        async with handle.auth_step(fields=["#cu-password"]):
            await handle.fill("#cu-password", data.PASSWORD)
            await handle.click("#cu-login-submit")
        assert login.CU.submissions == ["email", "password"]
        assert "auth_no_permit" in refusals(events)
    finally:
        await handle.close()


async def test_a_host_the_connection_does_not_log_in_at_is_refused_inside_the_window(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(f"{ORIGIN}/plain/login")
        async with handle.auth_step(fields=["#u", "#p"]):
            await handle.fill("#u", data.USERNAME)
            assert (
                await handle.page.evaluate(POST_JS, "https://id.fakeuni.test/cu/api/telemetry")
                is False
            )
        assert login.CU.telemetry == 0
        assert refusals(events) == ["auth_host_not_allowed"]
        # the guard said no before the proxy was asked
        (failed,) = [e for e in events if e["event"] == "browser.request_failed"]
        assert failed["blocked_by_client"] is True
        assert failed["proxy_failed"] is False
    finally:
        await handle.close()


async def test_on_a_first_login_the_subdomains_the_proxy_follows_the_site_to_are_login_hosts(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig, suffixes=("fakeuni.test",))
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(f"{ORIGIN}/plain/login")
        async with handle.auth_step(fields=["#u", "#p"]):
            await handle.fill("#u", data.USERNAME)
            # cross-origin without CORS headers: the page cannot read the answer, the site got it
            await handle.page.evaluate(POST_JS, "https://id.fakeuni.test/cu/api/telemetry")
        assert login.CU.telemetry == 1
        assert "auth_host_not_allowed" not in refusals(events)
    finally:
        await handle.close()


async def test_the_window_closes_after_its_time(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "AUTH_WINDOW_S", 0.4)
    rig = new_rig()
    handle = await open_entry(rig)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(f"{ORIGIN}/plain/login")
        async with handle.auth_step(fields=["#u", "#p"]):
            assert await handle.page.evaluate(POST_JS, f"{ORIGIN}/cu/api/telemetry") is True
            await asyncio.sleep(0.6)
            assert await handle.page.evaluate(POST_JS, f"{ORIGIN}/cu/api/telemetry") is False
        assert login.CU.telemetry == 1
        assert refusals(events) == ["auth_window_expired"]
    finally:
        await handle.close()


async def test_a_window_allows_a_few_posts_not_a_stream(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "AUTH_WINDOW_POSTS", 2)
    rig = new_rig()
    handle = await open_entry(rig)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(f"{ORIGIN}/plain/login")
        async with handle.auth_step(fields=["#u", "#p"]):
            sent = [
                await handle.page.evaluate(POST_JS, f"{ORIGIN}/cu/api/telemetry") for _ in range(3)
            ]
        assert sent == [True, True, False]
        assert login.CU.telemetry == 2
        assert refusals(events) == ["auth_window_exhausted"]
    finally:
        await handle.close()


async def test_a_page_that_no_longer_asks_for_a_login_gets_no_post_in_its_window(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = new_rig()
    handle = await open_entry(rig)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(f"{ORIGIN}/plain/login")
        async with handle.auth_step(fields=["#u", "#p"]):
            await handle.page.evaluate("document.body.innerHTML = '<h1>Мои занятия</h1>'")
            assert await handle.page.evaluate(POST_JS, f"{ORIGIN}/cu/api/telemetry") is False
        assert login.CU.telemetry == 0
        assert refusals(events) == ["auth_ui_missing"]
    finally:
        await handle.close()


async def test_a_submission_to_a_foreign_host_is_login_guard_refused_not_an_outage(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "https://elsewhere.fakeuni.test")
    rig = new_rig()
    handle = await open_entry(rig, hosts=CU_HOSTS)
    events = record_guard(handle, monkeypatch)
    try:
        async with handle.read_only_entry():
            await handle.goto(CU_ENTRY)

        async def email_step() -> None:
            async with handle.auth_step(fields=["#cu-login"]):
                await handle.fill("#cu-login", data.USERNAME)
                await handle.click("#cu-next")

        with pytest.raises(BotaiError) as raised:
            await email_step()
        assert raised.value.code == ErrorCode.LOGIN_GUARD_REFUSED
        assert "не отвечает" not in raised.value.detail
        assert "запасном экране" in raised.value.detail
        assert login.CU.submissions == []  # the refused form never reached the site
        assert "auth_host_not_allowed" in refusals(events)
        assert handle.navigation_failure() is raised.value
    finally:
        await handle.close()


def test_a_navigation_blocked_by_the_client_is_a_guard_refusal_only_behind_the_guard() -> None:
    failure = PlaywrightError("net::ERR_BLOCKED_BY_CLIENT at https://id.fakeuni.test/x?y=z")
    guarded = map_navigation_error(failure, "https://id.fakeuni.test/x", guarded=True)
    assert guarded.code == ErrorCode.LOGIN_GUARD_REFUSED
    assert "id.fakeuni.test" not in guarded.detail
    assert guarded.detail == guard_refusal_error().detail
    plain = map_navigation_error(failure, "https://id.fakeuni.test/x")
    assert plain.code == ErrorCode.SOURCE_UNAVAILABLE
    down = map_navigation_error(
        PlaywrightError("net::ERR_CONNECTION_REFUSED at https://id.fakeuni.test/"),
        "https://id.fakeuni.test/",
        guarded=True,
    )
    assert down.code == ErrorCode.SOURCE_UNAVAILABLE  # a site that is down still is


# ----------------------------------------------------------- the flow when the guard refuses


def flow_without_screen(rig: Rig, egress: ProxyEgress) -> LoginFlow:
    return LoginFlow(
        gateway=rig.gateway,
        vault=rig.vault,
        store=rig.store,
        egress=egress,
        screens=None,
        timing=FAST,
    )


async def test_flow_reports_guard_refusal_as_such_without_a_fallback_screen(
    new_rig: Callable[..., Rig], egress_access: ProxyEgress, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "https://elsewhere.fakeuni.test")
    rig = new_rig()
    know_cu_hosts(rig)
    flow = flow_without_screen(rig, egress_access)
    student = Student()
    with pytest.raises(BotaiError) as raised:
        await flow.ensure_for_entry(
            rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
        )
    assert raised.value.code == ErrorCode.LOGIN_GUARD_REFUSED
    assert raised.value.code != ErrorCode.SOURCE_UNAVAILABLE
    assert "не отвечает" not in raised.value.detail
    assert [kind for kind, _ in student.asked] == ["login"]  # nothing was asked again
    assert login.CU.submissions == []
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.PASSWORD)
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)


async def test_guard_refusal_does_not_cost_the_stored_password(
    new_rig: Callable[..., Rig], egress_access: ProxyEgress, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "https://elsewhere.fakeuni.test")
    rig = new_rig()
    know_cu_hosts(rig)
    await rig.vault.put(rig.user, rig.conn, SecretKind.LOGIN, data.USERNAME)
    await rig.vault.put(rig.user, rig.conn, SecretKind.PASSWORD, data.PASSWORD)
    with pytest.raises(BotaiError) as raised:
        await flow_without_screen(rig, egress_access).ensure_for_entry(
            rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
        )
    assert raised.value.code == ErrorCode.LOGIN_GUARD_REFUSED
    # the site never saw the form: this is not a refused password
    assert await rig.vault.has(rig.user, rig.conn, SecretKind.PASSWORD)
    assert login.CU.submissions == []


async def test_guard_refusal_opens_the_fallback_screen_and_says_the_site_works(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "https://elsewhere.fakeuni.test")

    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        await link.close("synthetic: the student leaves")

    rig = new_rig(human=human)
    know_cu_hosts(rig)
    ctx = rig.ctx(Student())
    with pytest.raises(JobCancelled):
        await rig.flow.ensure_for_entry(
            ctx, user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
        )
    await rig.screens.settle()
    assert len(rig.screens.links) == 1
    said = [b.markdown for b in ctx.blocks if isinstance(b, TextBlock)]
    assert any("Сайт вуза работает" in text and "запасной экран" in text for text in said)
    assert any(isinstance(b, LoginBlock) for b in ctx.blocks)
    assert login.CU.submissions == []


async def test_after_the_fallback_screen_the_flow_goes_on_in_native_fields(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "https://elsewhere.fakeuni.test")

    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        # the student does the e-mail step on the screen (here the site works properly again)
        monkeypatch.setattr(login, "CU_ACTION_ORIGIN", "")
        await handle.page.goto(CU_ENTRY)
        await handle.page.fill("#cu-login", data.USERNAME)
        await handle.arm_human_auth()
        await handle.page.click("#cu-next")

    rig = new_rig(human=human)
    know_cu_hosts(rig)
    student = Student()
    state = await rig.flow.ensure_for_entry(
        rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
    )
    await rig.screens.settle()
    assert await works(state)
    assert [kind for kind, _ in student.asked] == ["login", "password", "code"]
    assert login.CU.submissions == ["email", "password", "otp"]
    assert login.CU.telemetry == 0


async def test_a_login_check_posted_to_a_foreign_host_does_not_pass_either(
    new_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the page's own login check goes to a host the connection never logs in at: nothing posts it,
    # the page says it could not check, and the form stays (a refusal like any other, asked again)
    monkeypatch.setattr(login, "CU_CHECK_ORIGIN", "https://elsewhere.fakeuni.test")
    rig = new_rig()
    know_cu_hosts(rig)
    student = Student()
    with pytest.raises(BotaiError) as raised:
        await rig.flow.ensure_for_entry(
            rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=CU_ENTRY
        )
    assert raised.value.code == ErrorCode.LOGIN_FAILED
    assert login.CU.checks == 0
    assert login.CU.submissions == []
    assert student.count("login") == FAST.max_attempts
