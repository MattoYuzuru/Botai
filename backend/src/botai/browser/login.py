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
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlsplit

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
    replay,
    safe_label,
    secretish,
)
from botai.browser.gateway import (
    CaptureConfig,
    PlaywrightGateway,
    PlaywrightHandle,
    guard_refusal_error,
)
from botai.browser.presets import (
    MAX_HOSTS,
    ConnectionInfo,
    LoginStore,
    PgLoginStore,
    Recorder,
    build_preset,
    comparable,
    keep_known,
)
from botai.browser.probe import check_session, learn_probe, probe_ok, requires_authentication
from botai.browser.proxy import is_ip_literal, normalize_host
from botai.browser.session import (
    decode_session,
    encode_session,
    session_storage_expiry,
    storage_origin,
    with_confirmed_entry,
)
from botai.contracts.agent_ui import ConfirmBlock, InputBlock, LoginBlock, TextBlock, WaitBlock
from botai.contracts.specs import LoginPreset
from botai.core.browser import PageSnapshot, SessionState
from botai.core.clock import Clock, SystemClock
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import authentication_job, save_secret, student_is_needed
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobContext, JobState
from botai.core.vault import SecretKind, Vault

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_ADDITIONAL_LOGIN_HOSTS = 4
_BOT_PROTECTION_DETAIL = (
    "Сайт вуза не пускает автоматический заход. Мы не обходим такую защиту: "
    "попробуйте позже или войдите на сайте самостоятельно."
)
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
    def human_control(self) -> bool: ...

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
    async def register_native(
        self, user_id: str, connection_id: str, request: Callable[[], Coroutine[Any, Any, bool]]
    ) -> Callable[[], None]: ...

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


class _ManualRequested(Exception):
    """An explicit control request superseded the native question, never an input value."""


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
    entry_url: str | None = field(default=None, repr=False)
    entry_form_path: str | None = field(default=None, repr=False)
    force_relogin: bool = False
    manual: bool = False
    recorder: Recorder = field(default_factory=Recorder)
    attempts: dict[Any, int] = field(default_factory=lambda: dict[Any, int]())
    last_signature: Any = None
    submitted: int = 0
    probe: dict[str, Any] | None = None
    learned_probe: dict[str, Any] | None = None
    verified_page: tuple[str, str, int] | None = None
    final_url: str = ""
    steps_recorded: set[Any] = field(default_factory=lambda: set[Any]())
    manual_requested: bool = False
    native_waiting: bool = False
    manual_signal: asyncio.Event = field(default_factory=asyncio.Event)
    manual_ready: asyncio.Future[bool] | None = None
    human_confirmed: bool = False
    entry_verified: bool = False
    human_progress: bool = False
    push_confirmed: bool = False
    guard_fallbacks: int = 0


def _avoid_tokens(creds: _Creds) -> list[str]:
    """Lower-case fragments of what the student typed that must not end up in a shared preset."""
    if not creds.username:
        return []
    typed = creds.username.lower().strip()
    return [t for t in dict.fromkeys([typed, typed.split("@")[0]]) if len(t) >= 3]


def _ordinary_page(run: _Run, snap: PageSnapshot) -> bool:
    """Not a push wait, a captcha, a refusal or one of the login pages seen on the way."""
    return isinstance(detect(snap), UnknownPage) and (
        urlsplit(snap.url).path not in set(run.recorder.login_paths)
    )


def _same_site(host: str, site: str) -> bool:
    """Does `host` belong to the university's own domain (the origin's last two labels)? A rough
    answer to a question only asked of the student: whether a refused host is worth asking about."""
    root = ".".join(site.lower().split(".")[-2:])
    host = host.lower()
    return bool(root) and (host == root or host.endswith("." + root))


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

    async def ensure(
        self, ctx: object, *, user_id: str, connection_id: str, force: bool = False
    ) -> SessionState:
        """A session that works. `force` skips the stored one: a caller that has just seen the
        site refuse it (its data request answered 401 while the probe still passes) logs in
        anew instead of being handed the same session again."""
        return await self._ensure(ctx, user_id=user_id, connection_id=connection_id, force=force)

    async def ensure_for_entry(
        self,
        ctx: object,
        *,
        user_id: str,
        connection_id: str,
        entry_url: str,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        """Verify access to the supplied private page, using a screen when explicitly requested."""
        return await self._ensure(
            ctx,
            user_id=user_id,
            connection_id=connection_id,
            entry_url=entry_url,
            force=force or manual,
            manual=manual,
        )

    async def _ensure(
        self,
        ctx: object,
        *,
        user_id: str,
        connection_id: str,
        entry_url: str | None = None,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        job = authentication_job(cast(JobContext, ctx))
        key = (user_id, connection_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._check_cooldown(key)
            info = await self._store.connection(user_id, connection_id)
            preset = await self._store.preset_for(info.origin)
            hosts = self._hosts(info, preset)
            if entry_url is not None:
                self._validate_entry(info, hosts, entry_url)
            acquisition = await self._stored_session(user_id, connection_id)
            if entry_url is None and acquisition is not None and acquisition.entry_url is not None:
                entry_url = acquisition.entry_url.get_secret_value()
                self._validate_entry(info, hosts, entry_url)
            stored = None if force else acquisition
            diagnostic_event(
                "auth.session.check",
                connection_id=connection_id,
                force=force,
                stored_present=stored is not None,
                preset_present=preset is not None,
                allow_host_count=len(hosts),
            )
            if stored is not None:
                verdict = await check_session(
                    stored, preset, hosts, now=self._clock.now(), egress=self._egress
                )
                if verdict is True:
                    refreshed = (
                        stored
                        if entry_url is None
                        else await self._stored_entry_ready(
                            user_id, connection_id, info, preset, hosts, stored, entry_url
                        )
                    )
                    if refreshed is not None:
                        if entry_url is not None:
                            await save_secret(
                                job,
                                self._vault,
                                user_id,
                                connection_id,
                                SecretKind.SESSION,
                                encode_session(refreshed),
                                expires_at=session_storage_expiry(refreshed),
                            )
                        diagnostic_event("auth.session.reused", connection_id=connection_id)
                        return refreshed
            for added in range(MAX_ADDITIONAL_LOGIN_HOSTS + 1):
                try:
                    diagnostic_event(
                        "auth.session.login",
                        connection_id=connection_id,
                        allow_host_count=len(hosts),
                        host_round=added,
                    )
                    state = await self._login(
                        job,
                        user_id,
                        connection_id,
                        info,
                        preset,
                        hosts,
                        entry_url=entry_url,
                        force=force,
                        manual=manual,
                    )
                    diagnostic_event("auth.session.ready", connection_id=connection_id)
                    return state
                except BotaiError as exc:
                    host = self._host_to_approve(exc, hosts)
                    if (
                        host is None
                        or added == MAX_ADDITIONAL_LOGIN_HOSTS
                        or len(hosts) >= MAX_HOSTS
                    ):
                        raise
                    sends = (
                        "отправляет данные входа на"
                        if exc.extra.get("post")
                        else ("перенаправил вход на")
                    )
                    answer = await job.ask(
                        ConfirmBlock(
                            id=new_id(),
                            at=self._clock.now(),
                            question=f"Сайт вуза {sends} {host}. "
                            "Разрешить этому подключению обращаться к этому адресу?",
                            confirm_label="Разрешить и продолжить",
                            cancel_label="Не разрешать",
                        )
                    )
                    if answer.values is not True:
                        raise BotaiError(
                            ErrorCode.EGRESS_BLOCKED,
                            f"Переход на {host} не разрешён. Вход остановлен.",
                            extra={"host": host},
                        ) from None
                    if isinstance(self._store, PgLoginStore):
                        await self._store.allow_host(user_id, connection_id, host, ctx=job)
                    else:
                        await self._store.allow_host(user_id, connection_id, host)
                    info = await self._store.connection(user_id, connection_id)
                    preset = await self._store.preset_for(info.origin)
                    hosts = self._hosts(info, preset)
            raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _validate_entry(info: ConnectionInfo, hosts: list[str], entry_url: str) -> None:
        if (
            not entry_url
            or "\\" in entry_url
            or _CONTROL.search(entry_url)
            or storage_origin(entry_url, hosts) != info.origin
        ):
            raise BotaiError(
                ErrorCode.VALIDATION,
                "Для входа нужна HTTPS-ссылка на тот же разрешённый сайт источника",
            )

    @staticmethod
    def _entry_ready(
        info: ConnectionInfo,
        preset: LoginPreset | None,
        snap: PageSnapshot,
        entry_url: str,
        login_paths: Sequence[str] = (),
        *,
        human_confirmed: bool = False,
        allow_generic_root: bool = False,
    ) -> bool:
        if (
            storage_origin(snap.url, (urlsplit(info.origin).hostname or "",)) != info.origin
            or not snap.text.strip()
            or requires_authentication(snap)
            or not isinstance(detect(snap), UnknownPage)
        ):
            return False
        # A guest bootstrap session may turn a protected link into a public
        # catalogue. A material redirect is not browser authorization evidence.
        # Query canonicalization and a trailing slash do not change the view.
        requested = urlsplit(entry_url)
        requested_path = unquote(requested.path).rstrip("/") or "/"
        actual_path = unquote(urlsplit(snap.url).path).rstrip("/") or "/"
        generic_root = (
            allow_generic_root
            and requested.path in {"", "/"}
            and not requested.query
            and not requested.fragment
        )
        if not human_confirmed and not generic_root and actual_path != requested_path:
            return False
        path = urlsplit(snap.url).path
        known = preset.expired.get("redirectPathPrefix", []) if preset else []
        return path not in login_paths and not any(
            isinstance(prefix, str) and prefix and path.startswith(prefix) for prefix in known
        )

    async def _stored_entry_ready(
        self,
        user_id: str,
        connection_id: str,
        info: ConnectionInfo,
        preset: LoginPreset | None,
        hosts: list[str],
        state: SessionState,
        entry_url: str,
    ) -> SessionState | None:
        handle = await self._gateway.open(
            user_id=user_id,
            connection_id=connection_id,
            allow_hosts=hosts,
            origin=info.origin,
            session=state,
            read_only=True,
            background=True,
        )
        try:
            snap = await handle.goto(entry_url)
            if isinstance(detect(snap), ErrorPage):
                raise handle.navigation_failure() or BotaiError(
                    ErrorCode.SOURCE_UNAVAILABLE, "Страница источника не открылась"
                )
            if not self._entry_ready(info, preset, snap, entry_url):
                return None
            return with_confirmed_entry(await handle.export_session(), entry_url)
        except BotaiError as exc:
            if exc.code == ErrorCode.EGRESS_BLOCKED:
                # The fresh login handles an exact-host consent, rather than a
                # read-only restore silently granting a newly required host.
                return None
            raise
        finally:
            await handle.close()

    @staticmethod
    def _hosts(info: ConnectionInfo, preset: LoginPreset | None) -> list[str]:
        known: list[str] = list(preset.allow_hosts) if preset else []
        hosts = list(dict.fromkeys([*known, *info.allow_hosts]))
        if len(hosts) > MAX_HOSTS:
            raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Достигнут предел адресов этого подключения")
        return hosts

    @staticmethod
    def _host_to_approve(exc: BotaiError, hosts: list[str]) -> str | None:
        if exc.code != ErrorCode.EGRESS_BLOCKED:
            return None
        raw = exc.extra.get("host")
        host = normalize_host(raw) if isinstance(raw, str) else None
        if host is None or is_ip_literal(host) or host in hosts:
            return None
        return host

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
        *,
        entry_url: str | None = None,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        username = (
            None if manual else await self._vault.get(user_id, connection_id, SecretKind.LOGIN)
        )
        password = (
            None if manual else await self._vault.get(user_id, connection_id, SecretKind.PASSWORD)
        )
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
        run: _Run | None = None
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
                entry_url=entry_url,
                force_relogin=force,
                manual=manual,
                manual_requested=manual,
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
            if run is not None and run.manual_ready is not None and not run.manual_ready.done():
                run.manual_ready.set_result(False)
            await handle.close()

    async def _drive(self, run: _Run) -> SessionState:
        if run.entry_url is not None:
            async with run.handle.read_only_entry():
                snap = await run.handle.goto(run.entry_url)
        else:
            snap = await run.handle.goto(run.start_url)
        if run.entry_url is not None and requires_authentication(snap):
            run.entry_form_path = urlsplit(snap.url).path
        for _ in range(self._timing.max_pages):
            if run.manual:
                if isinstance(detect(snap), ErrorPage):
                    raise self._navigation_failure(run)
                if isinstance(detect(snap), HardBlock):
                    raise BotaiError(ErrorCode.SOURCE_BOT_PROTECTION, _BOT_PROTECTION_DETAIL)
                outcome = await self._human(
                    run, snap, hint="Вы просили ручной вход. Войдите на экране и нажмите «Готово»."
                )
                if isinstance(outcome, SessionState):
                    return outcome
                snap = outcome
                continue
            done = await self._try_finish(run, snap)
            if done is not None:
                return done
            # Entry verification can navigate from the candidate cabinet back to
            # a login/refusal. Process that fresh page, never the pre-check DOM.
            snap = await run.handle.snapshot()
            learned = None
            if run.preset and run.preset.steps and not snap.captcha_suspected:
                learned = replay(run.preset.steps, snap)
            detection = learned or detect(snap)
            diagnostic_event(
                "auth.page",
                connection_id=run.connection_id,
                form_detected=isinstance(detection, FormStep),
                push_detected=isinstance(detection, PushWait),
                captcha_detected=isinstance(detection, CaptchaPage),
                unknown_detected=isinstance(detection, UnknownPage),
                hard_block_detected=isinstance(detection, HardBlock),
                error_detected=isinstance(detection, ErrorPage),
                preset_replayed=learned is not None,
            )
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
                outcome = await self._form(run, detection)
            elif isinstance(detection, PushWait):
                snap = await self._push(run, snap)
                continue
            elif isinstance(detection, CaptchaPage):
                outcome = await self._captcha(run, snap)
            elif isinstance(detection, UnknownPage):
                outcome = await self._unknown(run, snap)
            elif isinstance(detection, HardBlock):
                raise BotaiError(
                    ErrorCode.SOURCE_BOT_PROTECTION,
                    _BOT_PROTECTION_DETAIL,
                )
            else:
                assert isinstance(detection, ErrorPage)
                failure = self._navigation_failure(run)
                if failure.code != ErrorCode.LOGIN_GUARD_REFUSED:
                    raise failure
                outcome = await self._guard_fallback(run, failure)
            if isinstance(outcome, SessionState):
                return outcome
            snap = outcome
        raise BotaiError(ErrorCode.LOGIN_FAILED, "Вход занял слишком много шагов, остановились")

    def _navigation_failure(self, run: _Run) -> BotaiError:
        failure = run.handle.navigation_failure()
        if failure is not None:
            return failure
        return BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Страница входа вуза не открылась")

    async def _guard_fallback(self, run: _Run, refusal: BotaiError) -> Outcome:
        """Our guard stopped a request of the login. The site works; what the page sent is not
        something we can verify. Say that, and give the human the fallback screen on a fresh copy
        of the page: the refused submission never reached the site, so nothing there has changed.

        Once per login. A second refusal means the screen did not help (a page that always sends
        what we do not pass): the student is told so instead of being sent round again."""
        can_fall_back = (
            self._screens is not None and run.entry_url is not None and run.guard_fallbacks == 0
        )
        diagnostic_event(
            "auth.guard_refused",
            connection_id=run.connection_id,
            error_code=refusal.code.value,
            fallback=can_fall_back,
        )
        if not can_fall_back or run.entry_url is None:
            raise guard_refusal_error(fallback_used=run.guard_fallbacks > 0)
        run.guard_fallbacks += 1
        run.submitted = max(0, run.submitted - 1)  # the refused form was never submitted
        await run.job.emit(
            TextBlock(
                id=new_id(),
                at=self._clock.now(),
                markdown="Сайт вуза работает, но отправил форму входа так, что Ботай её не "
                "пропустил: мы пропускаем только то, что страница отправляет сразу после вашего "
                "шага. Открываю запасной экран: завершите шаг там.",
            )
        )
        async with run.handle.read_only_entry():
            snap = await run.handle.goto(run.entry_url)
        run.last_signature = None  # the site did not refuse anything: not a failed attempt
        return await self._human(
            run, snap, hint="Завершите этот шаг входа на экране. Дальше продолжим в полях Ботай."
        )

    # -- success -------------------------------------------------------------------------------

    async def _try_finish(
        self, run: _Run, snap: PageSnapshot, *, force: bool = False
    ) -> SessionState | None:
        """Session-dependent GETs are useful probes, but cannot prove a student logged in."""
        if snap.url.startswith("chrome-error:") or snap.captcha_suspected:
            return None
        if (urlsplit(snap.url).hostname or "").lower() not in run.handle.allow_hosts:
            return None
        if requires_authentication(snap) or not isinstance(detect(snap), UnknownPage):
            return None
        if run.manual and not force:
            return None
        signature = page_signature(snap)
        if run.verified_page == signature and not force:
            return None
        network = await run.handle.network_log()
        state = await run.handle.export_session()
        if not state.cookies and state.bearer is None and not state.bearer_scopes:
            return None
        if not (run.submitted or run.push_confirmed or run.human_progress or force):
            # A public page may issue guest cookies/bearers whose GETs refuse a
            # bare HTTP client. That is client bootstrap, not authentication.
            run.verified_page = signature
            return None
        if run.entry_url is not None:
            async with run.handle.read_only_entry():
                fresh = await run.handle.goto(run.entry_url)
            found = detect(fresh)
            if isinstance(found, ErrorPage):
                raise self._navigation_failure(run)
            if isinstance(found, HardBlock):
                raise BotaiError(ErrorCode.SOURCE_BOT_PROTECTION, _BOT_PROTECTION_DETAIL)
            private_path = urlsplit(run.entry_url).path
            paths = [
                p for p in run.recorder.login_paths if p not in {private_path, run.entry_form_path}
            ]
            if not self._entry_ready(
                run.info,
                run.preset,
                fresh,
                run.entry_url,
                paths,
                human_confirmed=force,
                allow_generic_root=bool(run.submitted or run.push_confirmed),
            ):
                run.verified_page = page_signature(fresh)
                return None
            snap = fresh
            run.entry_verified = True
            network = await run.handle.network_log()
            state = await run.handle.export_session()
        if run.probe is not None:
            if await probe_ok(state, run.probe, run.hosts, egress=self._egress):
                run.human_confirmed = force
                run.final_url = snap.url
                return state
            run.verified_page = signature
            return None
        if run.entry_url is not None:
            private_path = urlsplit(run.entry_url).path
            private_paths = {private_path, run.entry_form_path}
            network = [
                request for request in network if urlsplit(request.url).path not in private_paths
            ]
        learned = await learn_probe(
            network,
            state,
            run.hosts,
            # A personal entry cannot become a shared landing-page probe.
            run.start_url if run.entry_url is not None else snap.url,
            egress=self._egress,
            avoid=_avoid_tokens(run.creds),
        )
        if learned is not None:
            run.human_confirmed = force
            run.probe = run.learned_probe = learned
            run.final_url = snap.url
            return state
        if (run.submitted > 0 or force) and (
            run.entry_url is not None or _ordinary_page(run, snap)
        ):
            # An explicit Done can confirm the ordinary page after all success guards above.
            run.human_confirmed = force
            run.final_url = snap.url
            return state
        run.verified_page = signature
        return None

    async def _finish(self, run: _Run, session: SessionState) -> None:
        user, conn = run.user_id, run.connection_id
        if run.entry_verified and run.entry_url is not None:
            # A supplied link is promoted only after the independent entry check succeeded.
            session.entry_url = with_confirmed_entry(session, run.entry_url).entry_url
        await save_secret(
            run.job,
            self._vault,
            user,
            conn,
            SecretKind.SESSION,
            encode_session(session),
            expires_at=session_storage_expiry(session),
        )
        if run.creds.typed_username and run.creds.username:
            await save_secret(
                run.job, self._vault, user, conn, SecretKind.LOGIN, run.creds.username
            )
        if run.creds.typed_password and run.creds.password is not None:
            await save_secret(
                run.job,
                self._vault,
                user,
                conn,
                SecretKind.PASSWORD,
                run.creds.password.get_secret_value(),
            )
        await self._remember_preset(run, session)
        await run.job.emit(
            TextBlock(
                id=new_id(),
                at=self._clock.now(),
                markdown=(
                    "Вы завершили вход на экране. Проверяю доступ к учебным данным."
                    if run.human_confirmed
                    else "Вход выполнен. Продолжаю подключение к учебным данным."
                ),
            )
        )

    async def _remember_preset(self, run: _Run, session: SessionState) -> None:
        reached = [h for h in run.handle.visited_hosts() if h not in run.handle.blocked_hosts()]
        hosts = list(dict.fromkeys([*run.hosts, *reached]))
        try:
            recorder = run.recorder
            entry = urlsplit(run.entry_url) if run.entry_url is not None else None
            if entry is not None and (entry.path not in {"", "/"} or entry.query or entry.fragment):
                private_path = entry.path
                private_paths = {private_path, run.entry_form_path}
                # A login form served directly at a supplied private URL must
                # not teach that URL to other users of this origin. A generic
                # root carries no private view: keep its discovered auth steps,
                # as the legacy entrance does, without a later revision churn.
                recorder = replace(
                    recorder,
                    forms=[f for f in recorder.forms if urlsplit(f.url).path not in private_paths],
                    login_paths=[p for p in recorder.login_paths if p not in private_paths],
                )
            preset = build_preset(
                origin=run.info.origin,
                start_url=run.start_url,
                hosts=hosts,
                recorder=recorder,
                session=session,
                probe=run.probe,
                final_url=run.final_url or run.start_url,
                now=self._clock.now(),
                avoid=_avoid_tokens(run.creds),
            )
            known = run.preset
            if known is not None:
                # this run met only some of the pages (a human did every step, or the site trusted
                # the device and asked for no code): the others stay in the shared preset
                preset = keep_known(preset, known)
            if known is None or comparable(known) != comparable(preset):
                if isinstance(self._store, PgLoginStore):
                    revision = await self._store.save_preset(
                        run.info.origin, preset, connection_id=run.connection_id, ctx=run.job
                    )
                else:
                    revision = await self._store.save_preset(
                        run.info.origin, preset, connection_id=run.connection_id
                    )
                log.info("login preset for %s saved as revision %d", run.info.origin, revision)
        except ValueError:
            log.warning("learned login preset for %s did not validate; not saved", run.info.origin)

    # -- mirrored form -------------------------------------------------------------------------

    async def _form(self, run: _Run, step: FormStep) -> Outcome:
        failed = await self._register(run, step)
        try:
            return await self._enter(run, step, failed=failed, submit=True)
        except _ManualRequested:
            return await self._manual_screen(run)
        except BotaiError as exc:
            if exc.code != ErrorCode.LOGIN_GUARD_REFUSED:
                raise
            return await self._guard_fallback(run, exc)

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

    async def _enter(
        self, run: _Run, step: FormStep, *, failed: bool, submit: bool
    ) -> PageSnapshot:
        """Ask for the values of the step and type them, one by one: what the student has given
        stays on the page when they take the screen. Each value is a step of its own for the
        guard (the page may react to it); the last one is typed inside the step that sends the
        form, so the page's login check and the submission share one window."""
        host = urlsplit(step.url).hostname or ""
        attempt = run.attempts.get(step.signature, 1)
        selectors = [f.selector for f in step.fields]
        run.handle.take_guard_refusals()  # what an earlier step met is not this step's
        for index, f in enumerate(step.fields):
            value = await self._value_for(run, f, host=host, failed=failed, attempt=attempt)
            if run.manual_requested:
                raise _ManualRequested
            if submit and index == len(step.fields) - 1:
                return await self._submit(run, step, [(f, value)])
            async with run.handle.auth_step(fields=selectors):
                await run.handle.fill(f.selector, value)
        return await run.handle.snapshot()

    async def _native_answer[T](self, run: _Run, ask: Callable[[], Coroutine[Any, Any, T]]) -> T:
        await student_is_needed()
        if self._screens is None:
            return await ask()
        run.manual_ready = asyncio.get_running_loop().create_future()
        ready = run.manual_ready
        run.native_waiting = True
        run.manual_signal.clear()
        answer: asyncio.Task[T] | None = None
        manual: asyncio.Task[bool] | None = None
        unregister: Callable[[], None] | None = None

        async def request() -> bool:
            if not run.native_waiting or run.handle.closed:
                return False
            run.manual_requested = True
            run.manual_signal.set()
            return await asyncio.shield(ready)

        try:
            unregister = await self._screens.register_native(
                run.user_id, run.connection_id, request
            )
            answer = asyncio.create_task(ask())
            manual = asyncio.create_task(run.manual_signal.wait())
            await asyncio.wait({answer, manual}, return_when=asyncio.FIRST_COMPLETED)
            if run.manual_requested:  # explicit manual control wins simultaneous completion
                raise _ManualRequested
            return answer.result()
        finally:
            run.native_waiting = False
            if unregister is not None:
                unregister()
            tasks: list[asyncio.Task[Any]] = [task for task in (answer, manual) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not run.manual_requested and not ready.done():
                ready.set_result(False)

    async def _manual_screen(self, run: _Run) -> Outcome:
        # Values already typed stay in this browser. They are no longer assumed to match
        # credentials the human may edit, and must not be persisted as a new saved password.
        run.creds.typed_username = run.creds.typed_password = False
        return await self._human(
            run,
            await run.handle.snapshot(),
            hint="Браузер открыт по вашей просьбе. Завершите вход на сайте; "
            "мы не будем заполнять поля или отправлять формы за вас.",
        )

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
            secret = await self._native_answer(
                run, lambda: run.job.ask_secret(self._block(f, question, secret=True))
            )
            if f.role == "password":
                creds.password = secret
                creds.password_from_vault = False
                creds.typed_password = True
            return secret.get_secret_value()
        if f.role == "text" and secretish(f.label):  # a security question: not plain text
            return (
                await self._native_answer(
                    run, lambda: run.job.ask_secret(self._block(f, question, secret=True))
                )
            ).get_secret_value()
        answer = await self._native_answer(
            run, lambda: run.job.ask(self._block(f, question, secret=False))
        )
        if not isinstance(answer.values, str) or not answer.values:
            raise JobCancelled("the user left a login field empty")
        if f.role == "username":
            creds.username = answer.values
            creds.typed_username = True
        return answer.values

    def _question(self, f: StepField, host: str, *, failed: bool, attempt: int) -> str:
        label = _CONTROL.sub(" ", safe_label(f.label, f.role))
        if failed and f.role == "otp":
            return f"Вход в {host}. Вуз не принял код. Введите ещё раз: {label} (попытка {attempt} из {self._timing.max_attempts})"
        if failed:
            return (
                f"Вход в {host}. Вуз не принял данные. Проверьте и введите ещё раз: {label} "
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
            kind = "password" if secret else ("number" if f.input_type == "number" else "text")
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

    async def _submit(
        self, run: _Run, step: FormStep, answers: Sequence[tuple[StepField, str]] = ()
    ) -> PageSnapshot:
        """Type the step's values and send the form, inside one window of the guard: a check of
        the login the page runs after typing and the submission itself are both part of the step."""
        if run.manual_requested:
            raise _ManualRequested
        run.submitted += 1
        await run.job.status("Отправляю данные на сайт вуза")
        async with run.handle.auth_step(fields=[f.selector for f in step.fields]):
            for f, value in answers:
                await run.handle.fill(f.selector, value)
            if step.submit:
                try:
                    await run.handle.click(step.submit)
                except BotaiError:
                    await run.handle.press(step.fields[-1].selector, "Enter")
            else:
                await run.handle.press(step.fields[-1].selector, "Enter")
        snap = await run.handle.snapshot()
        self._check_refusals(run, step, snap)
        return snap

    def _check_refusals(self, run: _Run, step: FormStep, snap: PageSnapshot) -> None:
        """A page that stays on the same form after our guard turned away something it posted did
        not say no to the student's values: the refusal is ours. Counting it as a wrong password
        would ask again, pause the account and delete the stored password for nothing.

        A host the page posted to is put to the student (the login restarts with it allowed) when it
        belongs to the site; one that does not (analytics) changes nothing. A page that moved on
        shrugged the refusal off."""
        refusals = run.handle.take_guard_refusals()
        if not refusals:
            return
        learned = (
            replay(run.preset.steps, snap)
            if run.preset and run.preset.steps and not snap.captcha_suspected
            else None
        )
        again = learned or best_step(snap)
        if not isinstance(again, FormStep) or again.signature != step.signature:
            return
        site = urlsplit(run.info.origin).hostname or ""
        for reason, host in refusals:
            if reason == "auth_host_not_allowed" and host and _same_site(host, site):
                raise BotaiError(
                    ErrorCode.EGRESS_BLOCKED,
                    f"Сайт вуза отправляет данные входа на {host}, а этот адрес не входит "
                    "в разрешённые для подключения",
                    extra={"host": host, "post": True},
                )
        if any(reason != "auth_host_not_allowed" for reason, _ in refusals):
            raise guard_refusal_error()

    # -- push approval -------------------------------------------------------------------------

    async def _push(self, run: _Run, snap: PageSnapshot) -> PageSnapshot:
        await student_is_needed()
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
                run.push_confirmed = not isinstance(detect(now), PushWait)
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
            try:
                await self._enter(run, found, failed=failed, submit=False)
            except _ManualRequested:
                return await self._manual_screen(run)
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
        await student_is_needed()
        # Both public and private routes can race disconnection or job cancellation.
        await self._store.connection(run.user_id, run.connection_id)
        await run.job.status("Открываю браузер для ручного входа", state=JobState.WAITING_USER)
        run.recorder.human()
        link = await self._screens.open(
            run.handle, user_id=run.user_id, connection_id=run.connection_id
        )
        try:
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
        except BaseException:
            await link.close("login no longer active")
            raise
        if run.manual_requested and run.manual_ready is not None and not run.manual_ready.done():
            run.manual_ready.set_result(True)
        await run.job.status("Жду, пока вы пройдёте шаг на экране", state=JobState.WAITING_USER)
        handed_over = page_signature(snap)
        handed_path = urlsplit(snap.url).path
        step_then = pending.signature if pending else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.human_wait_s
        while loop.time() < deadline:
            await asyncio.sleep(t.poll_s)
            if link.closed or run.handle.closed:
                raise JobCancelled("the login screen was closed")
            now = await run.handle.snapshot()
            found = detect(now)
            # A human's CDP click does not pass through handle.goto(), which maps
            # navigation errors. Do not leave a failed page waiting for 30 minutes
            # or accept it because a previously captured session still probes OK.
            if isinstance(found, ErrorPage):
                await link.close("navigation_failed")
                raise self._navigation_failure(run)
            if isinstance(found, HardBlock):
                await link.close("bot_protection")
                raise BotaiError(ErrorCode.SOURCE_BOT_PROTECTION, _BOT_PROTECTION_DETAIL)
            said_done = link.take_done()
            if not run.force_relogin and link.human_control and page_signature(now) != handed_over:
                run.human_progress = True
            finished = await self._try_finish(run, now, force=said_done)
            if finished is not None:
                await link.succeed()
                return finished
            now = await run.handle.snapshot()
            found = detect(now)
            same_form = best_step(now)
            if (
                said_done
                and not run.manual_requested
                and pending is not None
                and isinstance(same_form, FormStep)
                and same_form.signature == step_then
                and urlsplit(now.url).path == handed_path
            ):
                now = await self._submit(run, pending)  # the human did the check, we send the form
                continue
            if said_done:
                await link.needs("input", "Вход пока не подтверждён: завершите его на экране")
            if (
                isinstance(found, FormStep)
                and not run.manual_requested
                and not link.human_control
                and page_signature(now) != handed_over
                and (found.signature != step_then or urlsplit(now.url).path != handed_path)
                and link.idle_s() >= t.resume_idle_s
            ):
                await link.needs("input", "Дальше вход продолжится в обычных полях BotAI")
                await run.job.emit(
                    TextBlock(
                        id=new_id(),
                        at=self._clock.now(),
                        markdown="На сайте появилась обычная форма входа. "
                        "Продолжим в полях BotAI — браузер больше не нужен для этого шага.",
                    )
                )
                await link.close("mirrored")  # a normal form again: back to native fields
                run.last_signature = None  # the human acted in between: not a refusal of ours
                return now
        raise JobCancelled("the login screen timed out")


def _probe_of(preset: LoginPreset | None) -> dict[str, Any] | None:
    if preset is None:
        return None
    probe: Any = preset.success.get("probe")
    return dict(probe) if probe else None
