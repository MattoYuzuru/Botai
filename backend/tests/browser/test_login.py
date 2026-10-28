"""LoginFlow with real Chromium against the synthetic university (marker `browser`).

The student is played by a `responder` that answers the native fields; "the human at the fallback
screen" is a coroutine that works the page directly (the real screen arrives with browser.screen).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from botai.browser.egress import LOGIN_TOKEN_TTL_S, ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.login import LoginFlow, LoginTiming
from botai.browser.presets import InMemoryLoginStore
from botai.browser.session import decode_session
from botai.contracts import schemas
from botai.contracts.agent_ui import (
    AgentUiBlock,
    ConfirmBlock,
    InputBlock,
    LoginBlock,
    TextBlock,
    WaitBlock,
)
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext, JobCancelled
from botai.core.vault import InMemoryVault, SecretKind
from tests.fakeuni import data, login

pytestmark = pytest.mark.browser

ORIGIN = "https://fakeuni.test"
FAST = LoginTiming(
    push_wait_s=6.0,
    human_wait_s=25.0,
    poll_s=0.2,
    unknown_grace_s=0.8,
    resume_idle_s=0.4,
    cooldown_s=600.0,
    max_attempts=3,
    max_pages=14,
)


# ----------------------------------------------------------------------------------- doubles


class FakeLink:
    """The fallback screen as the flow sees it."""

    def __init__(self) -> None:
        self.ticket = "ticket-" + "x" * 20
        self.ws_url = "wss://botai.test/api/v1/login-sessions/01J0000000000000000000SESS/ws"
        self.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        self.closed = False
        self.human_control = False
        self.idle = math.inf
        self._done = False
        self.needs_calls: list[tuple[str, str]] = []
        self.succeeded = False
        self.close_reason: str | None = None

    def idle_s(self) -> float:
        return self.idle

    def request_done(self) -> None:
        self._done = True

    def take_done(self) -> bool:
        done, self._done = self._done, False
        return done

    async def needs(self, what: str, hint: str) -> None:
        self.needs_calls.append((what, hint))

    async def succeed(self) -> None:
        self.succeeded = True

    async def close(self, reason: str) -> None:
        self.closed = True
        self.close_reason = reason


Human = Callable[[PlaywrightHandle, FakeLink], Awaitable[None]]


class FakeScreens:
    def __init__(self, human: Human | None = None) -> None:
        self.human = human
        self.links: list[FakeLink] = []
        self._tasks: list[asyncio.Task[None]] = []
        self.native: dict[tuple[str, str], Callable[[], Awaitable[bool]]] = {}

    async def register_native(
        self, user_id: str, connection_id: str, request: Callable[[], Awaitable[bool]]
    ) -> Callable[[], None]:
        key = (user_id, connection_id)
        self.native[key] = request

        def unregister() -> None:
            if self.native.get(key) is request:
                self.native.pop(key)

        return unregister

    async def open(self, handle: PlaywrightHandle, *, user_id: str, connection_id: str) -> FakeLink:
        link = FakeLink()
        self.links.append(link)
        if self.human is not None:
            self._tasks.append(asyncio.create_task(self.human(handle, link)))
        return link

    async def settle(self) -> None:
        for task in self._tasks:
            await task


@dataclass
class Student:
    """Answers the native fields, counts what was asked."""

    password: str = data.PASSWORD
    otp: str = data.OTP
    username: str = data.USERNAME
    wrong_passwords: int = 0  # answer wrong this many times first
    wrong_otps: int = 0
    approve_hosts: bool = False
    asked: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    questions: list[str] = field(default_factory=lambda: list[str]())
    confirmations: list[str] = field(default_factory=lambda: list[str]())

    def __call__(self, block: AgentUiBlock) -> str | bool | None:
        if isinstance(block, ConfirmBlock):
            self.confirmations.append(block.question)
            return self.approve_hosts
        if not isinstance(block, InputBlock):
            return None
        self.questions.append(block.question)
        if block.input_kind == "password":
            self.asked.append(("password", block.autocomplete))
            if self.wrong_passwords > 0:
                self.wrong_passwords -= 1
                return "not-the-password"
            return self.password
        if block.input_kind == "code":
            self.asked.append(("code", block.autocomplete))
            if self.wrong_otps > 0:
                self.wrong_otps -= 1
                return "000000"
            return self.otp
        self.asked.append(("login", block.autocomplete))
        return self.username

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.asked if k == kind)


class PendingPasswordContext(InMemoryJobContext):
    """A real pending native question; a late fixture answer must not reach Chromium."""

    def __init__(self, user: str, connection: str) -> None:
        super().__init__(user_id=user, connection_id=connection, responder=Student())
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def ask_secret(self, block: InputBlock) -> SecretStr:
        await self.emit(block)
        self.secrets_requested.append(block.id)
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return SecretStr("discarded-native-fixture")


@dataclass
class Rig:
    flow: LoginFlow
    store: InMemoryLoginStore
    vault: InMemoryVault
    screens: FakeScreens
    gateway: PlaywrightGateway
    user: str
    conn: str

    def ctx(self, student: Student | None) -> InMemoryJobContext:
        return InMemoryJobContext(user_id=self.user, connection_id=self.conn, responder=student)

    async def ensure(self, student: Student | None) -> tuple[SessionState, InMemoryJobContext]:
        ctx = self.ctx(student)
        session = await self.flow.ensure(ctx, user_id=self.user, connection_id=self.conn)
        return session, ctx


def preset_dict(
    start: str, hosts: tuple[str, ...] = ("fakeuni.test",), probe: bool = True
) -> dict[str, Any]:
    """A minimal learned preset for a flow that starts somewhere other than the site root."""
    success: dict[str, Any] = {"urlOrigin": ORIGIN}
    if probe:
        success["probe"] = {
            "method": "GET",
            "url": f"{ORIGIN}/api/v2/me",
            "expectStatus": 200,
        }
    return {
        "id": "fakeuni-test",
        "name": "fakeuni.test",
        "start": {"url": start},
        "allowHosts": list(hosts),
        "success": success,
        "expired": {"redirectPathPrefix": ["/auth/", "/plain/", "/two/", "/push/", "/sso/"]},
        "mfa": {"possible": "unknown", "automate": False},
        "captcha": {"possible": "unknown", "automate": False},
        "capture": {"cookiesFor": list(hosts), "bearerFrom": "none", "storage": "encrypted"},
        "mode": "assisted",
        "revision": 1,
    }


@pytest.fixture
def make_rig(
    gateway: PlaywrightGateway, egress_access: ProxyEgress, trust_fakeuni: Path
) -> Callable[..., Rig]:
    def build(
        human: Human | None = None,
        timing: LoginTiming = FAST,
        gw: PlaywrightGateway | None = None,
        vault: InMemoryVault | None = None,
    ) -> Rig:
        user, conn = new_id(), new_id()
        store = InMemoryLoginStore()
        store.add_connection(user, conn, ORIGIN)
        vault = vault or InMemoryVault()
        screens = FakeScreens(human)
        flow = LoginFlow(
            gateway=gw or gateway,
            vault=vault,
            store=store,
            egress=egress_access,
            screens=screens,
            timing=timing,
        )
        return Rig(flow, store, vault, screens, gw or gateway, user, conn)

    return build


async def works(session: SessionState) -> bool:
    """Does the session open the cabinet (checked straight against the fixture's own state)?"""
    cookie = next((c for c in session.cookies if c.name == login.COOKIE), None)
    return cookie is not None and cookie.value.get_secret_value() in login.SESSIONS


# ------------------------------------------------------------------------------ first login


async def test_first_login_mirrors_the_form_stores_secrets_and_learns_a_preset(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    student = Student()
    session, ctx = await rig.ensure(student)

    assert await works(session)
    # native fields: a login (not secret) and two secrets, in the order the site asked
    inputs = [b for b in ctx.blocks if isinstance(b, InputBlock)]
    assert [(b.input_kind, b.secret, b.autocomplete) for b in inputs] == [
        ("email", False, "username"),
        ("password", True, "current-password"),
        ("code", True, "one-time-code"),
    ]
    assert len(ctx.secrets_requested) == 2
    assert inputs[2].max_length == 6

    # the Vault: session, login and password (OTP is never kept)
    user, conn = rig.user, rig.conn
    assert (await rig.vault.get(user, conn, SecretKind.PASSWORD)) is not None
    assert (await rig.vault.get(user, conn, SecretKind.LOGIN)).get_secret_value() == data.USERNAME  # type: ignore[union-attr]
    stored = await rig.vault.get(user, conn, SecretKind.SESSION)
    assert stored is not None
    assert decode_session(stored.get_secret_value()).cookies
    assert not await rig.vault.has(user, conn, SecretKind.TRANSIENT)

    # the learned preset
    (raw,) = rig.store.presets[ORIGIN]
    assert schemas.errors("login-preset.schema.json", raw) == []
    assert raw["start"]["url"] == f"{ORIGIN}/"
    assert raw["allowHosts"] == ["fakeuni.test"]
    assert raw["success"]["probe"] == {
        "method": "GET",
        "url": f"{ORIGIN}/api/v2/me",
        "expectStatus": 200,
    }
    assert raw["success"]["urlPathNot"] == ["/auth/"]
    assert raw["expired"] == {"redirectPathPrefix": ["/auth/"], "probe401": ["/api/v2/me"]}
    assert raw["mfa"] == {"possible": "yes", "kinds": ["sms"], "automate": False}
    assert raw["captcha"]["automate"] is False
    assert raw["mode"] == "assisted"
    assert [s["id"] for s in raw["steps"]] == ["username-password", "otp"]
    assert raw["steps"][0]["fields"][0] == {
        "role": "username",
        "selector": "#username",
        "label": "Почта",
    }
    assert raw["steps"][0]["submit"] == "#kc-login"
    assert rig.store.linked[rig.conn] == 1


async def test_a_valid_stored_session_is_returned_without_a_browser(
    make_rig: Callable[..., Rig], gateway: PlaywrightGateway
) -> None:
    rig = make_rig()
    first, _ = await rig.ensure(Student())
    opened_before = len(gateway._handles)
    student = Student()
    again, ctx = await rig.ensure(student)
    assert [c.value.get_secret_value() for c in again.cookies] == [
        c.value.get_secret_value() for c in first.cookies
    ]
    assert ctx.blocks == []
    assert student.asked == []
    assert len(gateway._handles) == opened_before
    assert len(rig.store.presets[ORIGIN]) == 1


async def test_force_skips_a_session_the_caller_has_just_seen_refused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = await make_rig_first_login(make_rig)
    first = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    student = Student()
    ctx = rig.ctx(student)
    again = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn, force=True)
    assert await works(again)
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    assert first is not None
    assert stored is not None
    assert stored.get_secret_value() != first.get_secret_value()
    assert student.asked == [("code", "one-time-code")]  # logged in anew, password from the Vault


async def make_rig_first_login(make_rig: Callable[..., Rig]) -> Rig:
    rig = make_rig()
    await rig.ensure(Student())
    return rig


async def test_expired_session_logs_in_again_with_the_stored_password(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    first, _ = await rig.ensure(Student())
    login.expire_all_sessions()
    student = Student(password="never used", username="never used")
    second, ctx = await rig.ensure(student)
    assert await works(second)
    assert second.cookies[0].value != first.cookies[0].value
    # only the one-time code was asked: login and password came from the Vault
    assert student.asked == [("code", "one-time-code")]
    assert len(ctx.secrets_requested) == 1
    # the preset was replayed and did not change
    assert len(rig.store.presets[ORIGIN]) == 1


async def test_a_login_that_meets_no_code_page_does_not_make_the_preset_forget_it(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preset is shared by every student of the origin. A site that trusts this device and
    asks for no SMS code once must not teach everybody else that it never asks (found by the
    full-stack test, W7)."""
    rig = make_rig()
    await rig.ensure(Student())
    (before,) = rig.store.presets[ORIGIN]
    assert [s["id"] for s in before["steps"]] == ["username-password", "otp"]

    login.expire_all_sessions()
    monkeypatch.setattr(login, "OTP_REQUIRED", False)
    student = Student(password="never used", username="never used")
    second, _ = await rig.ensure(student)

    assert await works(second)
    assert student.asked == []  # nothing to ask: the Vault had the login and the password
    (after,) = rig.store.presets[ORIGIN]  # no new revision, the old one was replayed
    assert [s["id"] for s in after["steps"]] == ["username-password", "otp"]
    assert after["mfa"] == before["mfa"] == {"possible": "yes", "automate": False, "kinds": ["sms"]}


async def test_a_page_a_login_does_meet_is_learned_next_to_the_known_ones(
    make_rig: Callable[..., Rig],
) -> None:
    """Keeping what an earlier login saw does not stop the preset from learning something new."""
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/"))  # a preset that knows no step yet
    await rig.ensure(Student())

    known, learned = rig.store.presets[ORIGIN]
    assert known.get("steps", []) == []
    assert [s["id"] for s in learned["steps"]] == ["username-password", "otp"]
    assert learned["revision"] == 2


async def test_login_without_otp_step_and_a_two_page_flow(make_rig: Callable[..., Rig]) -> None:
    for start, expected in (
        ("/plain/login", ["login", "password"]),
        ("/two/username", ["login", "password"]),
    ):
        rig = make_rig()
        rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}{start}"))
        student = Student()
        session, _ = await rig.ensure(student)
        assert await works(session), start
        assert [k for k, _ in student.asked] == expected, start


async def test_wrong_password_is_asked_again_with_a_clear_question(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    student = Student(wrong_passwords=1)
    session, ctx = await rig.ensure(student)
    assert await works(session)
    assert student.count("password") == 2
    retry = [q for q in student.questions if "не принял" in q]
    assert retry
    assert "попытка 2 из 3" in retry[0]
    # only the working password was stored
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.PASSWORD)
    assert stored is not None
    assert stored.get_secret_value() == data.PASSWORD
    assert ctx.secrets_requested


async def test_three_wrong_passwords_stop_and_pause_for_ten_minutes(
    make_rig: Callable[..., Rig], gateway: PlaywrightGateway
) -> None:
    rig = make_rig()
    before = len(login.ATTEMPTS)
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student(wrong_passwords=99))
    assert exc.value.code == ErrorCode.LOGIN_FAILED
    assert "10 мин" in exc.value.detail
    assert len(login.ATTEMPTS) - before == 3
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.PASSWORD)
    # the pause: no browser is opened and the site is not touched
    opened = len(gateway._handles)
    with pytest.raises(BotaiError, match="Подождите"):
        await rig.ensure(Student())
    assert len(gateway._handles) == opened
    assert len(login.ATTEMPTS) - before == 3


async def test_wrong_code_is_asked_again_then_accepted(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    student = Student(wrong_otps=1)
    session, _ = await rig.ensure(student)
    assert await works(session)
    assert student.count("code") == 2
    assert any("не принял код" in q for q in student.questions)


async def test_a_refused_stored_password_is_forgotten_and_the_user_is_asked(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    await rig.vault.put(rig.user, rig.conn, SecretKind.LOGIN, data.USERNAME)
    await rig.vault.put(rig.user, rig.conn, SecretKind.PASSWORD, "an-old-password")
    before = len(login.ATTEMPTS)
    student = Student()
    session, _ = await rig.ensure(student)
    assert await works(session)
    assert len(login.ATTEMPTS) - before == 1  # one automatic try, no more
    assert student.count("password") == 1  # then the user was asked
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.PASSWORD)
    assert stored is not None
    assert stored.get_secret_value() == data.PASSWORD


async def test_nobody_answering_cancels_the_job(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    with pytest.raises(JobCancelled):
        await rig.ensure(None)


# --------------------------------------------------------------------------------- push wait


async def test_a_push_page_is_not_a_finished_login_even_without_a_probe(
    make_rig: Callable[..., Rig],
) -> None:
    """No learned probe must not turn "confirm on your phone" into a successful login."""
    rig = make_rig(timing=replace(FAST, push_wait_s=1.5))
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/push/login", probe=False))
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student())
    assert exc.value.code == ErrorCode.LOGIN_FAILED
    assert "не подтвердили" in exc.value.detail
    for kind in (SecretKind.SESSION, SecretKind.PASSWORD, SecretKind.LOGIN):
        assert not await rig.vault.has(rig.user, rig.conn, kind)  # nothing kept from a half login


async def test_the_same_push_login_succeeds_once_approved_and_learns_its_probe(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/push/login", probe=False))
    ctx = rig.ctx(Student())

    async def approve_when_asked() -> None:
        while not any(isinstance(b, WaitBlock) for b in ctx.blocks):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        login.approve_push()

    approver = asyncio.create_task(approve_when_asked())
    session = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    await approver
    assert await works(session)
    assert rig.store.presets[ORIGIN][-1]["success"]["probe"]["url"] == f"{ORIGIN}/api/v2/me"


async def test_a_refusal_shown_at_another_address_is_still_a_refusal(
    make_rig: Callable[..., Rig],
) -> None:
    """Keycloak-like: the form is served at /kc/auth, the refusal comes back at
    /kc/auth/authenticate. Three tries, not four; the stored password is tried once."""
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/kc/auth"))
    before = len(login.ATTEMPTS)
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student(wrong_passwords=99))
    assert exc.value.code == ErrorCode.LOGIN_FAILED
    assert len(login.ATTEMPTS) - before == 3

    stale = make_rig()
    stale.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/kc/auth"))
    await stale.vault.put(stale.user, stale.conn, SecretKind.LOGIN, data.USERNAME)
    await stale.vault.put(stale.user, stale.conn, SecretKind.PASSWORD, "an-old-password")
    before = len(login.ATTEMPTS)
    student = Student()
    session, _ = await stale.ensure(student)
    assert await works(session)
    assert len(login.ATTEMPTS) - before == 1  # the stale password is not sent a second time
    assert student.count("password") == 1


async def test_the_preset_keeps_what_the_student_typed_out_of_paths_and_probes(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    await rig.ensure(Student())
    text = json.dumps(rig.store.presets[ORIGIN][-1], ensure_ascii=False).lower()
    assert "student" not in text  # the local part of the login, anywhere


async def test_push_wait_is_a_wait_block_only_once_per_wait(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(timing=replace(FAST, push_wait_s=2.0))
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/push/login"))
    ctx = rig.ctx(Student())
    with pytest.raises(BotaiError):
        await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    assert len([b for b in ctx.blocks if isinstance(b, WaitBlock)]) == 1


async def test_push_approval_is_a_wait_block_and_needs_no_field(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/push/login"))
    student = Student()
    ctx = rig.ctx(student)

    async def approve_when_asked() -> None:
        while not any(isinstance(b, WaitBlock) for b in ctx.blocks):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        login.approve_push()

    approver = asyncio.create_task(approve_when_asked())
    session = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    await approver
    assert await works(session)
    waits = [b for b in ctx.blocks if isinstance(b, WaitBlock)]
    assert len(waits) == 1
    assert "телефон" in waits[0].message
    assert waits[0].expires_at is not None
    assert [k for k, _ in student.asked] == ["login", "password"]
    assert any("подтверждения" in stage for stage, _ in ctx.statuses)
    preset = rig.store.presets[ORIGIN][-1]
    assert preset["mfa"] == {"possible": "yes", "kinds": ["push"], "automate": False}


async def test_push_that_is_never_approved_fails_after_the_wait(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(timing=replace(FAST, push_wait_s=1.5))
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/push/login"))
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student())
    assert exc.value.code == ErrorCode.LOGIN_FAILED
    assert "не подтвердили" in exc.value.detail


# ---------------------------------------------------------------------------- the human (screen)


async def tick_captcha_and_submit(handle: PlaywrightHandle, link: FakeLink) -> None:
    page = handle.page
    await page.frame_locator("iframe").locator("#human").check()
    await asyncio.sleep(0.3)
    await page.click("#go")


async def tick_captcha_then_say_done(handle: PlaywrightHandle, link: FakeLink) -> None:
    await handle.page.frame_locator("iframe").locator("#human").check()
    await asyncio.sleep(0.3)
    link.request_done()


async def test_captcha_goes_to_the_human_and_never_to_automation(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(human=tick_captcha_and_submit)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    student = Student()
    session, ctx = await rig.ensure(student)
    await rig.screens.settle()
    assert await works(session)
    # the student's fields stayed native; only the check was handed over
    assert [k for k, _ in student.asked] == ["login", "password"]
    (block,) = [b for b in ctx.blocks if isinstance(b, LoginBlock)]
    assert block.connection_id == rig.conn
    assert str(block.ws_url).startswith("wss://botai.test/")
    assert block.hint is not None
    assert "робот" in block.hint
    (link,) = rig.screens.links
    assert link.succeeded is True
    preset = rig.store.presets[ORIGIN][-1]
    assert preset["captcha"] == {"possible": "yes", "automate": False}
    assert any(state.value == "waiting_user" for _, state in ctx.statuses)


async def test_done_from_the_screen_submits_the_prefilled_form(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(human=tick_captcha_then_say_done)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    session, _ = await rig.ensure(Student())
    await rig.screens.settle()
    assert await works(session)


async def test_a_page_nobody_taught_us_is_handed_to_the_human(
    make_rig: Callable[..., Rig],
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        await handle.page.goto(f"{ORIGIN}/plain/login")
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
        await handle.page.click("button[type=submit]")

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/download-less-page"))
    # a 404 page has no fields and no push text: the flow waits a moment, then calls the human
    session, ctx = await rig.ensure(Student())
    await rig.screens.settle()
    assert await works(session)
    assert [type(b) for b in ctx.blocks if isinstance(b, LoginBlock)] == [LoginBlock]
    preset = rig.store.presets[ORIGIN][-1]
    assert preset["mode"] == "screen"


async def test_the_human_not_finishing_cancels_the_job(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(timing=replace(FAST, human_wait_s=1.2))
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    with pytest.raises(JobCancelled):
        await rig.ensure(Student())


@pytest.mark.parametrize(
    ("target", "expected_code", "blocked_host", "close_reason"),
    [
        ("/redirect/evil", ErrorCode.EGRESS_BLOCKED, "evil.localhost", "navigation_failed"),
        ("/screen/unavailable", ErrorCode.SOURCE_UNAVAILABLE, None, "navigation_failed"),
        ("/blocked", ErrorCode.SOURCE_BOT_PROTECTION, None, "bot_protection"),
    ],
)
async def test_failed_navigation_from_the_human_screen_closes_it_and_reports_the_error(
    make_rig: Callable[..., Rig],
    target: str,
    expected_code: ErrorCode,
    blocked_host: str | None,
    close_reason: str,
) -> None:
    """A real click after handoff must not leave the job waiting on a Chromium error page."""

    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        if target == "/screen/unavailable":
            await handle.page.route(
                f"{ORIGIN}{target}", lambda route: route.abort("connectionfailed")
            )
        await handle.page.locator("#a").evaluate(
            "(anchor, target) => anchor.setAttribute('href', target)", target
        )
        await handle.page.click("#a", no_wait_after=True)

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, {**preset_dict(f"{ORIGIN}/screen/lab"), "mode": "screen"})
    with pytest.raises(BotaiError) as exc:
        # FAST.human_wait_s is 25 s: before the fix this timed out or got
        # JobCancelled instead of promptly reporting the actual failure.
        await asyncio.wait_for(rig.ensure(Student()), timeout=10.0)
    await rig.screens.settle()
    assert exc.value.code == expected_code
    assert exc.value.extra == ({"host": blocked_host} if blocked_host else {})
    (link,) = rig.screens.links
    assert link.closed
    assert link.close_reason == close_reason
    assert not link.succeeded
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)
    if expected_code == ErrorCode.SOURCE_BOT_PROTECTION:
        opened = len(rig.gateway._handles)
        with pytest.raises(BotaiError, match="отклонил автоматический заход"):
            await rig.ensure(None)
        assert len(rig.gateway._handles) == opened


async def test_after_the_captcha_a_normal_form_is_mirrored_again(
    make_rig: Callable[..., Rig],
) -> None:
    """The human gets past the check; the next page is a plain form: back to native fields."""

    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.idle = 1.0  # the viewer has been quiet for a second
        await handle.page.goto(f"{ORIGIN}/plain/login")

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    student = Student()
    session, ctx = await rig.ensure(student)
    await rig.screens.settle()
    assert await works(session)
    (link,) = rig.screens.links
    assert link.close_reason == "mirrored"
    # typed once, reused by the form that followed
    assert student.count("password") == 1
    assert len([b for b in ctx.blocks if isinstance(b, LoginBlock)]) == 1
    assert any(isinstance(b, TextBlock) and "обычная форма" in b.markdown for b in ctx.blocks)


async def test_interacting_with_the_screen_keeps_manual_control_after_idle(
    make_rig: Callable[..., Rig],
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.human_control = True
        link.idle = math.inf  # switching tabs is not consent to automate the next form
        await handle.page.goto(f"{ORIGIN}/plain/login")
        await asyncio.sleep(FAST.resume_idle_s * 2)
        assert not link.closed
        assert await handle.page.input_value("#u") == ""
        assert await handle.page.input_value("#p") == ""
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
        await handle.page.click("button[type=submit]")

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    session, _ = await rig.ensure(Student())
    await rig.screens.settle()
    assert await works(session)
    assert rig.screens.links[0].succeeded


@pytest.mark.parametrize("answer_simultaneously", [False, True])
async def test_manual_request_supersedes_native_answer_without_reopening_the_page(
    make_rig: Callable[..., Rig], answer_simultaneously: bool
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        assert await handle.page.input_value("#u") == data.USERNAME
        assert await handle.page.input_value("#p") == ""
        assert await handle.page.evaluate("window.__handoffMarker") == "same-browser"
        await asyncio.sleep(FAST.resume_idle_s * 2)
        assert not link.closed
        assert await handle.page.input_value("#p") == ""
        await handle.page.fill("#p", data.PASSWORD)
        await handle.page.click("button[type=submit]")

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/plain/login"))
    ctx = PendingPasswordContext(rig.user, rig.conn)
    opened = len(rig.gateway._handles)
    running = asyncio.create_task(rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn))
    request: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(ctx.waiting.wait(), 5)
        handle = next(h for h in rig.gateway._handles if h.connection_id == rig.conn)
        await handle.page.evaluate("window.__handoffMarker = 'same-browser'")
        request = asyncio.create_task(rig.screens.native[(rig.user, rig.conn)]())
        if answer_simultaneously:
            ctx.release.set()
        assert await asyncio.wait_for(request, 3)
        assert len(rig.gateway._handles) == opened + 1
        if not answer_simultaneously:
            assert ctx.cancelled.is_set()
            ctx.release.set()  # a late answer has no waiter left to fill or submit it
        session = await asyncio.wait_for(running, 8)
        await rig.screens.settle()
        assert await works(session)
        assert (rig.user, rig.conn) not in rig.screens.native
        assert not await rig.vault.has(rig.user, rig.conn, SecretKind.PASSWORD)
        assert any(isinstance(b, TextBlock) and "Вход выполнен" in b.markdown for b in ctx.blocks)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)


async def test_cancelled_native_wait_unregisters_the_manual_request(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/plain/login"))
    ctx = PendingPasswordContext(rig.user, rig.conn)
    running = asyncio.create_task(rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn))
    try:
        await asyncio.wait_for(ctx.waiting.wait(), 5)
        late_request = rig.screens.native[(rig.user, rig.conn)]
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert (rig.user, rig.conn) not in rig.screens.native
        assert await late_request() is False
        assert not rig.screens.links
        assert ctx.cancelled.is_set()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("case", ["confirmed", "visible-login", "negative-known-probe"])
async def test_manual_done_without_a_learnable_probe_respects_existing_success_guards(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        if case == "visible-login":
            # A cookie is insufficient while the login form is still visible.
            await handle.context.add_cookies(
                [{"name": "fixture", "value": "not-proof", "domain": "fakeuni.test", "path": "/"}]
            )
        else:
            await handle.page.fill("#p", data.PASSWORD)
            await handle.page.click("button[type=submit]")
        await asyncio.sleep(FAST.poll_s * 3)
        assert not link.succeeded  # no native submit or learnable probe: wait for real Done
        link.request_done()
        if case != "confirmed":
            await asyncio.sleep(FAST.poll_s * 3)
            assert not link.succeeded
            assert any("не подтверждён" in hint for _, hint in link.needs_calls)
            await link.close("synthetic incomplete login")

    rig = make_rig(human=human)
    rig.store.seed_preset(
        ORIGIN, preset_dict(f"{ORIGIN}/plain/login", probe=case == "negative-known-probe")
    )
    monkeypatch.setattr("botai.browser.login.learn_probe", AsyncMock(return_value=None))
    if case == "negative-known-probe":
        monkeypatch.setattr("botai.browser.login.probe_ok", AsyncMock(return_value=False))
    ctx = PendingPasswordContext(rig.user, rig.conn)
    running = asyncio.create_task(rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn))
    request: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(ctx.waiting.wait(), 5)
        request = asyncio.create_task(rig.screens.native[(rig.user, rig.conn)]())
        assert await asyncio.wait_for(request, 3)
        if case == "confirmed":
            session = await asyncio.wait_for(running, 8)
            assert await works(session)
            text = [b.markdown for b in ctx.blocks if isinstance(b, TextBlock)]
            assert text[-1] == "Вы завершили вход на экране. Проверяю доступ к учебным данным."
        else:
            with pytest.raises(JobCancelled):
                await asyncio.wait_for(running, 8)
        await rig.screens.settle()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)


async def test_manual_handoff_rechecks_the_connection_before_opening_a_screen(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/plain/login"))
    ctx = PendingPasswordContext(rig.user, rig.conn)
    running = asyncio.create_task(rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn))
    request: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(ctx.waiting.wait(), 5)
        monkeypatch.setattr(
            rig.store,
            "connection",
            AsyncMock(side_effect=BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")),
        )
        request = asyncio.create_task(rig.screens.native[(rig.user, rig.conn)]())
        with pytest.raises(BotaiError) as error:
            await running
        assert error.value.code == ErrorCode.FORBIDDEN
        assert await request is False
        assert not rig.screens.links
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)


async def test_bot_protection_is_reported_without_retries_or_a_screen(
    make_rig: Callable[..., Rig], gateway: PlaywrightGateway
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/blocked"))
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student())
    assert exc.value.code == ErrorCode.SOURCE_BOT_PROTECTION
    assert rig.screens.links == []
    # nobody else gets to hammer the site meanwhile: the next call does not even open a browser
    opened = len(gateway._handles)
    with pytest.raises(BotaiError, match="отклонил автоматический заход"):
        await rig.ensure(Student())
    assert len(gateway._handles) == opened


async def test_a_screen_mode_preset_goes_straight_to_the_human(
    make_rig: Callable[..., Rig],
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
        await handle.page.click("button[type=submit]")

    rig = make_rig(human=human)
    rig.store.seed_preset(ORIGIN, {**preset_dict(f"{ORIGIN}/plain/login"), "mode": "screen"})
    student = Student()
    session, ctx = await rig.ensure(student)
    await rig.screens.settle()
    assert await works(session)
    assert student.asked == []  # nothing was mirrored
    assert [b for b in ctx.blocks if isinstance(b, LoginBlock)]
    assert rig.store.presets[ORIGIN][-1]["mode"] == "screen"


# ---------------------------------------------------------------------------------------- SSO


async def test_sso_second_host_listed_in_the_preset(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.store.seed_preset(
        ORIGIN, preset_dict(f"{ORIGIN}/sso/start", hosts=("fakeuni.test", "id.fakeuni.test"))
    )
    student = Student()
    session, _ = await rig.ensure(student)
    assert await works(session)
    assert [k for k, _ in student.asked] == ["login", "password"]
    assert all("Вход в id.fakeuni.test." in question for question in student.questions)


async def test_sso_second_host_missing_from_the_preset_is_egress_blocked(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student())
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra["host"] == "id.fakeuni.test"


async def test_sso_host_approval_persists_it_and_reopens_with_a_fresh_exact_grant(
    make_rig: Callable[..., Rig],
    gateway: PlaywrightGateway,
    egress_access: ProxyEgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = AsyncMock(wraps=gateway.open)
    grants = AsyncMock(wraps=egress_access.credentials)
    monkeypatch.setattr(gateway, "open", opened)
    monkeypatch.setattr(egress_access, "credentials", grants)
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    student = Student(approve_hosts=True)

    def respond(block: AgentUiBlock) -> str | bool | None:
        if isinstance(block, ConfirmBlock):
            # The failed context and screen are closed before permission is requested.
            assert rig.user not in gateway._by_user
        return student(block)

    ctx = InMemoryJobContext(user_id=rig.user, connection_id=rig.conn, responder=respond)
    session = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    assert await works(session)
    assert [kind for kind, _ in student.asked] == ["login", "password"]
    (confirm,) = [block for block in ctx.blocks if isinstance(block, ConfirmBlock)]
    assert "id.fakeuni.test" in confirm.question
    assert "://" not in confirm.question
    assert "?" not in confirm.question[:-1]
    assert confirm.confirm_label == "Разрешить и продолжить"
    assert confirm.cancel_label == "Не разрешать"
    info = await rig.store.connection(rig.user, rig.conn)
    assert info.allow_hosts == ("fakeuni.test", "id.fakeuni.test")
    assert opened.await_count == 2
    assert [call.kwargs["allow_hosts"] for call in opened.await_args_list] == [
        ["fakeuni.test"],
        ["fakeuni.test", "id.fakeuni.test"],
    ]
    browser_grants = [
        call for call in grants.await_args_list if call.kwargs["ttl_s"] == LOGIN_TOKEN_TTL_S
    ]
    assert [list(call.args[0]) for call in browser_grants] == [
        ["fakeuni.test"],
        ["fakeuni.test", "id.fakeuni.test"],
    ]
    assert all(call.kwargs["suffixes"] == [] for call in browser_grants)


async def test_saved_preset_keeps_the_connections_approved_hosts_on_forced_relogin(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.add_connection(
        rig.user, rig.conn, ORIGIN, allow_hosts=("fakeuni.test", "id.fakeuni.test")
    )
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    student = Student()
    ctx = rig.ctx(student)
    session = await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn, force=True)
    assert await works(session)
    assert student.confirmations == []
    assert [kind for kind, _ in student.asked] == ["login", "password"]


@pytest.mark.parametrize("answer", [False, "true", ["yes"]])
async def test_an_unapproved_sso_host_never_changes_the_connection_or_retries(
    make_rig: Callable[..., Rig], answer: bool | str | list[str]
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    ctx = InMemoryJobContext(user_id=rig.user, connection_id=rig.conn, responder=lambda _: answer)
    with pytest.raises(BotaiError, match="не разрешён") as exc:
        await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert len([block for block in ctx.blocks if isinstance(block, ConfirmBlock)]) == 1
    assert (await rig.store.connection(rig.user, rig.conn)).allow_hosts == ("fakeuni.test",)
    assert rig.user not in rig.gateway._by_user


@pytest.mark.parametrize(
    "host",
    [
        None,
        42,
        "https://id.fakeuni.test/path",
        "*.fakeuni.test",
        "127.0.0.1",
        "127.1",
        "[::1]",
        "fakeuni.test",
    ],
)
async def test_only_a_new_exact_dns_host_can_be_approved(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, host: object
) -> None:
    rig = make_rig()
    failure = BotaiError(ErrorCode.EGRESS_BLOCKED, "Запрос запрещён", extra={"host": host})
    login = AsyncMock(side_effect=failure)
    monkeypatch.setattr(rig.flow, "_login", login)
    student = Student(approve_hosts=True)
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(student)
    assert exc.value is failure
    assert student.confirmations == []
    assert login.await_count == 1
    assert (await rig.store.connection(rig.user, rig.conn)).allow_hosts == ("fakeuni.test",)


async def test_a_repeated_failure_of_an_already_approved_host_does_not_prompt_again(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    failure = BotaiError(
        ErrorCode.EGRESS_BLOCKED, "Запрос запрещён", extra={"host": "id.fakeuni.test"}
    )
    login = AsyncMock(side_effect=failure)
    monkeypatch.setattr(rig.flow, "_login", login)
    student = Student(approve_hosts=True)
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(student)
    assert exc.value is failure
    assert len(student.confirmations) == 1
    assert login.await_count == 2
    assert (await rig.store.connection(rig.user, rig.conn)).allow_hosts == (
        "fakeuni.test",
        "id.fakeuni.test",
    )


async def test_late_host_consent_cannot_reopen_a_connection_rejected_by_the_store(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    login = AsyncMock(
        side_effect=BotaiError(
            ErrorCode.EGRESS_BLOCKED, "Запрос запрещён", extra={"host": "id.fakeuni.test"}
        )
    )
    allow_host = AsyncMock(side_effect=BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено"))
    monkeypatch.setattr(rig.flow, "_login", login)
    monkeypatch.setattr(rig.store, "allow_host", allow_host)
    student = Student(approve_hosts=True)
    with pytest.raises(BotaiError, match="Подключение отключено") as exc:
        await rig.ensure(student)
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert len(student.confirmations) == 1
    assert login.await_count == 1  # no second browser or proxy grant after the late answer
    allow_host.assert_awaited_once_with(rig.user, rig.conn, "id.fakeuni.test")
    assert (await rig.store.connection(rig.user, rig.conn)).allow_hosts == ("fakeuni.test",)


async def test_new_login_hosts_require_separate_confirmations_and_stop_after_four(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    login = AsyncMock(
        side_effect=[
            BotaiError(
                ErrorCode.EGRESS_BLOCKED, "Запрос запрещён", extra={"host": f"sso-{i}.fakeuni.test"}
            )
            for i in range(5)
        ]
    )
    monkeypatch.setattr(rig.flow, "_login", login)
    student = Student(approve_hosts=True)
    ctx = rig.ctx(student)
    with pytest.raises(BotaiError):
        await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    confirmations = [block for block in ctx.blocks if isinstance(block, ConfirmBlock)]
    assert len(confirmations) == len({block.id for block in confirmations}) == 4
    assert login.await_count == 5
    assert (await rig.store.connection(rig.user, rig.conn)).allow_hosts == (
        "fakeuni.test",
        *(f"sso-{i}.fakeuni.test" for i in range(4)),
    )


async def test_first_login_follows_the_site_to_its_own_subdomains_and_learns_them(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "ROOT_TARGET", "/sso/start")
    rig = make_rig()
    session, _ = await rig.ensure(Student())
    assert await works(session)
    preset = rig.store.presets[ORIGIN][-1]
    assert preset["allowHosts"] == ["fakeuni.test", "id.fakeuni.test"]
    assert preset["idp"] == {"kind": "unknown"}
    assert schemas.errors("login-preset.schema.json", preset) == []
    # only the application's own cookies are kept, not the identity provider's
    assert preset["capture"]["cookiesFor"] == ["fakeuni.test"]


# ------------------------------------------------------------------------------- C1: secrets


def _markers_in(blob: bytes) -> list[str]:
    return [m for m in (data.PASSWORD, data.OTP) if m.encode() in blob]


async def test_no_secret_reaches_events_logs_preset_vault_or_disk(
    egress_access: ProxyEgress,
    make_rig: Callable[..., Rig],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1: the password and the code appear nowhere but in the sealed ciphertext."""
    private_gateway = PlaywrightGateway(
        egress_access, sandbox=False, ignore_https_errors=True, tmp_dir=tmp_path / "chromium"
    )
    try:
        rig = make_rig(gw=private_gateway)
        with caplog.at_level(logging.DEBUG):
            session, ctx = await rig.ensure(Student())
        assert await works(session)

        events = json.dumps([b.model_dump(mode="json", by_alias=True) for b in ctx.blocks])
        assert _markers_in(events.encode()) == []
        assert _markers_in("\n".join(r.getMessage() for r in caplog.records).encode()) == []
        assert _markers_in(json.dumps(rig.store.presets).encode()) == []
        assert _markers_in(repr(session).encode()) == []
        assert _markers_in(repr(ctx.statuses).encode()) == []

        # the vault rows are ciphertext: neither the markers nor the cookie value show through
        cookie = session.cookies[0].value.get_secret_value().encode()
        rows = rig.vault.raw()
        assert {kind for (_, _, kind) in rows} >= {
            SecretKind.SESSION,
            SecretKind.PASSWORD,
            SecretKind.LOGIN,
        }
        for sealed in rows.values():
            blob = sealed.ciphertext + sealed.wrapped_dek + sealed.nonce
            assert _markers_in(blob) == []
            assert cookie not in blob
    finally:
        await private_gateway.aclose()
    # the browser's own temporary files (profile, caches)
    leaked = [
        str(p.relative_to(tmp_path))
        for p in (tmp_path / "chromium").rglob("*")
        if p.is_file() and _markers_in(p.read_bytes())
    ]
    assert leaked == []


async def test_learned_preset_has_no_personal_values(make_rig: Callable[..., Rig]) -> None:
    """C4 for logins: nothing typed or personal in the preset, only structure."""
    rig = make_rig()
    await rig.ensure(Student())
    text = json.dumps(rig.store.presets[ORIGIN][-1], ensure_ascii=False)
    for personal in (data.USERNAME, "student", "@", "Иванова", "u-1001", "s***"):
        assert personal not in text, personal


# ------------------------------------------------------------------------- misc behaviours


async def test_unknown_connection_is_not_found(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    with pytest.raises(BotaiError) as exc:
        await rig.flow.ensure(rig.ctx(None), user_id=rig.user, connection_id=new_id())
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_two_users_do_not_share_secrets(make_rig: Callable[..., Rig]) -> None:
    vault = InMemoryVault()
    a, b = make_rig(vault=vault), make_rig(vault=vault)
    await a.ensure(Student())
    assert not await vault.has(b.user, b.conn, SecretKind.SESSION)
    assert await vault.has(a.user, a.conn, SecretKind.SESSION)
