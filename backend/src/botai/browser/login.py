"""LoginFlow: gives a connection a valid university session (core.browser.SessionProvider).

In order of preference:

1. the stored session, if its probe still passes;
2. an automatic login in the server's own Chromium with the password kept in the Vault;
3. an interactive login: every field of the university's form becomes a native BotAI field
   (`ctx.ask` for a login, `ctx.ask_secret` for passwords and codes), the values are typed into
   the page by the server, and a push approval is a `WaitBlock`.

A captcha, a bot-protection check or a page nobody has taught the flow about is never automated:
the job gets a `LoginBlock` (the fallback remote screen) and waits for the human. What a login
shows is remembered as a preset without personal values, so the next login of the same origin
needs no agent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from pydantic import SecretStr

from botai.browser.egress import Egress
from botai.browser.forms import (
    CaptchaPage,
    ErrorPage,
    FormStep,
    HardBlock,
    PushWait,
    StepField,
    UnknownPage,
    best_step,
    detect,
    has_credential_fields,
    replay,
    safe_label,
)
from botai.browser.gateway import CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.browser.presets import (
    ConnectionInfo,
    LoginStore,
    Recorder,
    build_preset,
    comparable,
)
from botai.browser.probe import check_session, learn_probe, probe_ok
from botai.browser.session import decode_session, encode_session
from botai.contracts.agent_ui import InputBlock, LoginBlock, WaitBlock
from botai.contracts.specs import LoginPreset
from botai.core.browser import PageSnapshot, SessionState
from botai.core.clock import Clock, SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobContext, JobState
from botai.core.vault import SecretKind, Vault

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
Outcome = PageSnapshot | SessionState


@dataclass(frozen=True, slots=True)
class LoginTiming:
    """Every wait of the flow, so tests can shorten them."""

    push_wait_s: float = 120.0
    human_wait_s: float = 30 * 60.0
    poll_s: float = 1.0
    unknown_grace_s: float = 4.0
    resume_idle_s: float = 3.0
    cooldown_s: float = 600.0
    max_attempts: int = 3
    max_pages: int = 14


class ScreenLink(Protocol):
    """One fallback remote screen opened for a login (implementation: browser.screen)."""

    ticket: str
    ws_url: str
    expires_at: datetime

    @property
    def closed(self) -> bool: ...

    def idle_s(self) -> float:
        """Seconds since the viewer last touched the page (inf if never)."""
        ...

    def take_done(self) -> bool:
        """True once after the viewer says they consider the login complete."""
        ...

    async def needs(self, what: str, hint: str) -> None: ...

    async def succeed(self) -> None: ...

    async def close(self, reason: str) -> None: ...


class LoginScreens(Protocol):
    async def open(
        self, handle: PlaywrightHandle, *, user_id: str, connection_id: str
    ) -> ScreenLink: ...


class _Creds:
    """The login and password of one run: from the Vault, or what the user types."""

    def __init__(self, username: str | None, password: SecretStr | None) -> None:
        self.username = username
        self.password = password
        self.password_from_vault = password is not None
        self.typed_username = False
        self.typed_password = False


@dataclass(slots=True)
class _Run:
    job: JobContext
    user_id: str
    connection_id: str
    info: ConnectionInfo
    preset: LoginPreset | None
    hosts: list[str]
    start_url: str
    handle: PlaywrightHandle
    creds: _Creds
    recorder: Recorder = field(default_factory=Recorder)
    attempts: dict[Any, int] = field(default_factory=lambda: dict[Any, int]())
    last_signature: Any = None
    submitted: int = 0
    probe: dict[str, Any] | None = None
    learned_probe: dict[str, Any] | None = None
    verified_page: tuple[str, str, int] | None = None
    final_url: str = ""
    steps_recorded: set[Any] = field(default_factory=lambda: set[Any]())


def page_signature(snap: PageSnapshot) -> tuple[str, str, int]:
    return snap.url, snap.title, len(snap.text)


class LoginFlow:
    """Implements core.browser.SessionProvider."""

    def __init__(
        self,
        *,
        gateway: PlaywrightGateway,
        vault: Vault,
        store: LoginStore,
        egress: Egress,
        screens: LoginScreens | None = None,
        clock: Clock | None = None,
        timing: LoginTiming | None = None,
    ) -> None:
        self._gateway = gateway
        self._vault = vault
        self._store = store
        self._egress = egress
        self._screens = screens
        self._clock = clock or SystemClock()
        self._timing = timing or LoginTiming()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cooldown: dict[tuple[str, str], tuple[float, str]] = {}

    # -- public ------------------------------------------------------------------------------

    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> SessionState:
        job = cast(JobContext, ctx)  # the SessionProvider protocol types it as `object`
        key = (user_id, connection_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._check_cooldown(key)
            info = await self._store.connection(user_id, connection_id)
            preset = await self._store.preset_for(info.origin)
            hosts = list(preset.allow_hosts) if preset else list(info.allow_hosts)
            stored = await self._stored_session(user_id, connection_id)
            if stored is not None:
                verdict = await check_session(
                    stored, preset, hosts, now=self._clock.now(), egress=self._egress
                )
                if verdict is True:
                    return stored
            return await self._login(job, user_id, connection_id, info, preset, hosts)

    # -- the stored session and creds ----------------------------------------------------------

    async def _stored_session(self, user_id: str, connection_id: str) -> SessionState | None:
        raw = await self._vault.get(user_id, connection_id, SecretKind.SESSION)
        if raw is None:
            return None
        try:
            return decode_session(raw.get_secret_value())
        except ValueError:
            log.warning("stored session of connection %s is unreadable", connection_id)
            return None

    def _pause(self, key: tuple[str, str], reason: str) -> None:
        self._cooldown[key] = (asyncio.get_running_loop().time() + self._timing.cooldown_s, reason)

    def _check_cooldown(self, key: tuple[str, str]) -> None:
        entry = self._cooldown.get(key)
        if entry is None:
            return
        until, reason = entry
        remaining = until - asyncio.get_running_loop().time()
        if remaining <= 0:
            del self._cooldown[key]
            return
        raise BotaiError(
            ErrorCode.LOGIN_FAILED,
            f"{reason} Подождите {int(remaining // 60) + 1} мин. и попробуйте снова.",
        )

    # -- one login -----------------------------------------------------------------------------

    async def _login(
        self,
        job: JobContext,
        user_id: str,
        connection_id: str,
        info: ConnectionInfo,
        preset: LoginPreset | None,
        hosts: list[str],
    ) -> SessionState:
        username = await self._vault.get(user_id, connection_id, SecretKind.LOGIN)
        password = await self._vault.get(user_id, connection_id, SecretKind.PASSWORD)
        creds = _Creds(username.get_secret_value() if username else None, password)
        origin_host = urlsplit(info.origin).hostname or ""
        capture = CaptureConfig()
        if preset is not None:
            cap = preset.capture
            capture = CaptureConfig(
                cookies_for=tuple(cap.get("cookiesFor") or ()) or None,
                bearer_from=cap.get("bearerFrom", "authorizationHeader"),
                bearer_key=cap.get("bearerKey"),
            )
        await job.status("Открываю страницу входа")
        handle = await self._gateway.open(
            user_id=user_id,
            connection_id=connection_id,
            allow_hosts=hosts or [origin_host],
            origin=info.origin,
            capture=capture,
            # a first login may follow the site to subdomains of its own domain, nothing else
            suffixes=[] if preset else [origin_host],
        )
        try:
            run = _Run(
                job=job,
                user_id=user_id,
                connection_id=connection_id,
                info=info,
                preset=preset,
                hosts=hosts or [origin_host],
                start_url=preset.start["url"] if preset else f"{info.origin}/",
                handle=handle,
                creds=creds,
                probe=_probe_of(preset),
            )
            try:
                session = await self._drive(run)
            except BotaiError as exc:
                if exc.code == ErrorCode.SOURCE_BOT_PROTECTION:  # no automatic retries (ADR-0001)
                    self._pause(
                        (user_id, connection_id), "Сайт вуза отклонил автоматический заход."
                    )
                raise
            await self._finish(run, session)
            return session
        finally:
            await handle.close()

    async def _drive(self, run: _Run) -> SessionState:
        snap = await run.handle.goto(run.start_url)
        for _ in range(self._timing.max_pages):
            done = await self._try_finish(run, snap)
            if done is not None:
                return done
            learned = None
            if run.preset and run.preset.steps and not snap.captcha_suspected:
                learned = replay(run.preset.steps, snap)
            detection = learned or detect(snap)
            if (
                run.preset is not None
                and run.preset.mode == "screen"
                and isinstance(detection, FormStep | UnknownPage)
            ):
                outcome = await self._human(run, snap, hint="Войдите на сайте вуза на этом экране.")
                if isinstance(outcome, SessionState):
                    return outcome
                snap = outcome
                continue
            if isinstance(detection, FormStep):
                snap = await self._form(run, detection)
                continue
            if isinstance(detection, PushWait):
                snap = await self._push(run, snap)
                continue
            if isinstance(detection, CaptchaPage):
                outcome = await self._captcha(run, snap)
            elif isinstance(detection, UnknownPage):
                outcome = await self._unknown(run, snap)
            elif isinstance(detection, HardBlock):
                raise BotaiError(
                    ErrorCode.SOURCE_BOT_PROTECTION,
                    "Сайт вуза не пускает автоматический заход. Мы не обходим такую защиту: "
                    "попробуйте позже или войдите на сайте самостоятельно.",
                )
            else:
                assert isinstance(detection, ErrorPage)
                raise self._navigation_failure(run)
            if isinstance(outcome, SessionState):
                return outcome
            snap = outcome
        raise BotaiError(ErrorCode.LOGIN_FAILED, "Вход занял слишком много шагов, остановились")

    def _navigation_failure(self, run: _Run) -> BotaiError:
        blocked = run.handle.blocked_hosts()
        if blocked:
            host = blocked[-1]
            return BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                f"Сайт вуза ведёт на {host}, а этот адрес не входит в разрешённые для подключения",
                extra={"host": host},
            )
        return BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Страница входа вуза не открылась")

    # -- success -------------------------------------------------------------------------------

    async def _try_finish(
        self, run: _Run, snap: PageSnapshot, *, force: bool = False
    ) -> SessionState | None:
        """The session, if this page is the logged-in side and a probe proves it."""
        if snap.url.startswith("chrome-error:") or snap.captcha_suspected:
            return None
        if (urlsplit(snap.url).hostname or "").lower() not in run.handle.allow_hosts:
            return None
        if has_credential_fields(snap):
            return None
        signature = page_signature(snap)
        if run.verified_page == signature and not force:
            return None
        state = await run.handle.export_session()
        if not state.cookies and state.bearer is None:
            return None
        if run.probe is not None:
            if await probe_ok(state, run.probe, run.hosts, egress=self._egress):
                run.final_url = snap.url
                return state
            run.verified_page = signature
            return None
        learned = await learn_probe(
            await run.handle.network_log(), state, run.hosts, snap.url, egress=self._egress
        )
        if learned is not None:
            run.probe = run.learned_probe = learned
            run.final_url = snap.url
            return state
        if run.submitted > 0:
            # a form was submitted and the site now shows no login: accept, with nothing to probe
            run.final_url = snap.url
            return state
        run.verified_page = signature
        return None

    async def _finish(self, run: _Run, session: SessionState) -> None:
        user, conn = run.user_id, run.connection_id
        await self._vault.put(
            user, conn, SecretKind.SESSION, encode_session(session), expires_at=session.expires_hint
        )
        if run.creds.typed_username and run.creds.username:
            await self._vault.put(user, conn, SecretKind.LOGIN, run.creds.username)
        if run.creds.typed_password and run.creds.password is not None:
            await self._vault.put(
                user, conn, SecretKind.PASSWORD, run.creds.password.get_secret_value()
            )
        await self._remember_preset(run, session)

    async def _remember_preset(self, run: _Run, session: SessionState) -> None:
        reached = [h for h in run.handle.visited_hosts() if h not in run.handle.blocked_hosts()]
        hosts = list(dict.fromkeys([*run.hosts, *reached]))
        try:
            preset = build_preset(
                origin=run.info.origin,
                start_url=run.start_url,
                hosts=hosts,
                recorder=run.recorder,
                session=session,
                probe=run.probe,
                final_url=run.final_url or run.start_url,
                now=self._clock.now(),
            )
            known = run.preset
            if run.preset is not None and not run.recorder.steps:
                # nothing new was observed (for instance a human did every step): keep the old steps
                preset["steps"] = [
                    s.model_dump(by_alias=True, exclude_none=True) for s in run.preset.steps
                ]
            if known is None or comparable(known) != comparable(preset):
                revision = await self._store.save_preset(
                    run.info.origin, preset, connection_id=run.connection_id
                )
                log.info("login preset for %s saved as revision %d", run.info.origin, revision)
        except ValueError:
            log.warning("learned login preset for %s did not validate; not saved", run.info.origin)

    # -- mirrored form -------------------------------------------------------------------------

    async def _form(self, run: _Run, step: FormStep) -> PageSnapshot:
        failed = await self._register(run, step)
        await self._fill(run, step, failed=failed)
        return await self._submit(run, step)

    async def _register(self, run: _Run, step: FormStep) -> bool:
        """Count this submission; True if the page is the one we just submitted (it refused)."""
        signature = step.signature
        failed = run.last_signature == signature
        attempts = run.attempts.get(signature, 0)
        if failed and attempts >= self._timing.max_attempts:
            self._pause(
                (run.user_id, run.connection_id),
                "Вуз несколько раз подряд не принял данные, мы остановились, чтобы не "
                "заблокировать аккаунт.",
            )
            raise BotaiError(
                ErrorCode.LOGIN_FAILED,
                "Вуз не принял данные три раза подряд. Мы остановились, чтобы не заблокировать "
                f"аккаунт: попробуйте через {int(self._timing.cooldown_s // 60)} мин.",
            )
        run.attempts[signature] = attempts + 1
        run.last_signature = signature
        if signature not in run.steps_recorded:
            run.steps_recorded.add(signature)
            run.recorder.form(step)
        if failed and run.creds.password_from_vault and "password" in step.roles:
            await self._vault.delete(run.user_id, run.connection_id, SecretKind.PASSWORD)
            run.creds.password = None
            run.creds.password_from_vault = False
            log.info(
                "stored password of connection %s was refused; asking the user", run.connection_id
            )
        return failed

    async def _fill(self, run: _Run, step: FormStep, *, failed: bool) -> None:
        host = urlsplit(run.info.origin).hostname or ""
        attempt = run.attempts.get(step.signature, 1)
        for f in step.fields:
            value = await self._value_for(run, f, host=host, failed=failed, attempt=attempt)
            await run.handle.fill(f.selector, value)

    async def _value_for(
        self, run: _Run, f: StepField, *, host: str, failed: bool, attempt: int
    ) -> str:
        creds = run.creds
        if f.role == "username" and creds.username is not None and not failed:
            return creds.username
        if f.role == "password" and creds.password is not None and not failed:
            return creds.password.get_secret_value()
        question = self._question(f, host, failed=failed, attempt=attempt)
        if f.role in ("password", "otp"):
            secret = await run.job.ask_secret(self._block(f, question, secret=True))
            if f.role == "password":
                creds.password = secret
                creds.password_from_vault = False
                creds.typed_password = True
            return secret.get_secret_value()
        answer = await run.job.ask(self._block(f, question, secret=False))
        if not isinstance(answer.values, str) or not answer.values:
            raise JobCancelled("the user left a login field empty")
        if f.role == "username":
            creds.username = answer.values
            creds.typed_username = True
        return answer.values

    def _question(self, f: StepField, host: str, *, failed: bool, attempt: int) -> str:
        label = _CONTROL.sub(" ", safe_label(f.label, f.role))
        if failed and f.role == "otp":
            return f"Вуз не принял код. Введите ещё раз: {label} (попытка {attempt} из {self._timing.max_attempts})"
        if failed:
            return (
                f"Вуз не принял данные. Проверьте и введите ещё раз: {label} "
                f"(попытка {attempt} из {self._timing.max_attempts})"
            )
        return f"Вход в {host}. {label}"

    def _block(self, f: StepField, question: str, *, secret: bool) -> InputBlock:
        kind: Any
        autocomplete: Any
        max_length = 500
        if f.role == "password":
            kind, autocomplete, max_length = "password", "current-password", 200
        elif f.role == "otp":
            kind, autocomplete = "code", "one-time-code"
            max_length = max(1, min(f.maxlength or 12, 64))
        elif f.role == "username":
            kind = "email" if f.input_type == "email" else "text"
            autocomplete = "username"
        else:
            kind = "number" if f.input_type == "number" else "text"
            autocomplete = "off"
        return InputBlock(
            id=new_id(),
            at=self._clock.now(),
            question=question[:500],
            input_kind=kind,
            required=True,
            max_length=max_length,
            secret=secret,
            autocomplete=autocomplete,
        )

    async def _submit(self, run: _Run, step: FormStep) -> PageSnapshot:
        run.submitted += 1
        await run.job.status("Отправляю данные на сайт вуза")
        if step.submit:
            try:
                return await run.handle.click(step.submit)
            except BotaiError:
                pass  # the learned or guessed button is gone: Enter in the last field
        return await run.handle.press(step.fields[-1].selector, "Enter")

    # -- push approval -------------------------------------------------------------------------

    async def _push(self, run: _Run, snap: PageSnapshot) -> PageSnapshot:
        t = self._timing
        run.recorder.push(snap.url)
        expires = self._clock.now() + timedelta(seconds=t.push_wait_s)
        await run.job.emit(
            WaitBlock(
                id=new_id(),
                at=self._clock.now(),
                message="Подтвердите вход на телефоне: сайт вуза ждёт подтверждения.",
                expires_at=expires,
            )
        )
        await run.job.status("Жду подтверждения входа на телефоне", state=JobState.WAITING_USER)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.push_wait_s
        while loop.time() < deadline:
            await asyncio.sleep(t.poll_s)
            now = await run.handle.snapshot()
            if now.url != snap.url or not isinstance(detect(now), PushWait):
                await run.job.status("Подтверждение получено, продолжаю вход")
                return now
        raise BotaiError(
            ErrorCode.LOGIN_FAILED,
            f"Вход не подтвердили за {int(t.push_wait_s // 60) or 1} мин. Попробуйте ещё раз.",
        )

    # -- the human: captcha and pages nobody taught us ---------------------------------------------

    async def _captcha(self, run: _Run, snap: PageSnapshot) -> Outcome:
        run.recorder.captcha(snap.url)
        pending: FormStep | None = None
        found = best_step(snap)
        if isinstance(found, FormStep):
            # the student's fields can still be native: fill them, the human does the check
            failed = await self._register(run, found)
            await self._fill(run, found, failed=failed)
            pending = found
        return await self._human(
            run,
            snap,
            hint="Пройдите проверку «я не робот» и нажмите «Войти» на экране. "
            "Капчу решает только человек.",
            pending=pending,
        )

    async def _unknown(self, run: _Run, snap: PageSnapshot) -> Outcome:
        """Give a page that is still loading a moment, then hand it to the human."""
        t = self._timing
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.unknown_grace_s
        signature = page_signature(snap)
        while loop.time() < deadline:
            await asyncio.sleep(min(0.5, t.poll_s))
            now = await run.handle.snapshot()
            if page_signature(now) != signature or not isinstance(detect(now), UnknownPage):
                return now
        return await self._human(
            run,
            snap,
            hint="Эту страницу входа мы не умеем заполнять сами: завершите вход на экране.",
        )

    async def _human(
        self, run: _Run, snap: PageSnapshot, *, hint: str, pending: FormStep | None = None
    ) -> Outcome:
        if self._screens is None:
            raise BotaiError(
                ErrorCode.LOGIN_FAILED, "Нужен ручной шаг входа, но запасной экран не настроен"
            )
        t = self._timing
        run.recorder.human()
        link = await self._screens.open(
            run.handle, user_id=run.user_id, connection_id=run.connection_id
        )
        await run.job.emit(
            LoginBlock(
                id=new_id(),
                at=self._clock.now(),
                connection_id=run.connection_id,
                ticket=link.ticket,
                ws_url=link.ws_url,  # type: ignore[arg-type]
                expires_at=link.expires_at,
                hint=hint,
            )
        )
        await run.job.status("Жду, пока вы пройдёте шаг на экране", state=JobState.WAITING_USER)
        handed_over = page_signature(snap)
        step_then = pending.signature if pending else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.human_wait_s
        while loop.time() < deadline:
            await asyncio.sleep(t.poll_s)
            if link.closed or run.handle.closed:
                raise JobCancelled("the login screen was closed")
            now = await run.handle.snapshot()
            said_done = link.take_done()
            finished = await self._try_finish(run, now, force=said_done)
            if finished is not None:
                await link.succeed()
                return finished
            found = detect(now)
            same_form = best_step(now)
            if (
                said_done
                and pending is not None
                and isinstance(same_form, FormStep)
                and same_form.signature == step_then
            ):
                now = await self._submit(run, pending)  # the human did the check, we send the form
                continue
            if said_done:
                await link.needs("input", "Вход пока не подтверждён: завершите его на экране")
            if (
                isinstance(found, FormStep)
                and page_signature(now) != handed_over
                and found.signature != step_then
                and link.idle_s() >= t.resume_idle_s
            ):
                await link.close("mirrored")  # a normal form again: back to native fields
                return now
        raise JobCancelled("the login screen timed out")


def _probe_of(preset: LoginPreset | None) -> dict[str, Any] | None:
    if preset is None:
        return None
    probe: Any = preset.success.get("probe")
    return dict(probe) if probe else None
