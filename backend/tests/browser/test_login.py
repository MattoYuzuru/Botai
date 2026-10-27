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

import pytest

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.login import LoginFlow, LoginTiming
from botai.browser.presets import InMemoryLoginStore
from botai.browser.session import decode_session
from botai.contracts import schemas
from botai.contracts.agent_ui import AgentUiBlock, InputBlock, LoginBlock, WaitBlock
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
    asked: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    questions: list[str] = field(default_factory=lambda: list[str]())

    def __call__(self, block: AgentUiBlock) -> str | None:
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


async def test_sso_second_host_missing_from_the_preset_is_egress_blocked(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/sso/start"))
    with pytest.raises(BotaiError) as exc:
        await rig.ensure(Student())
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert exc.value.extra["host"] == "id.fakeuni.test"


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
