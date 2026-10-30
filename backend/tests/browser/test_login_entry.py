"""A guest bootstrap API cannot replace authentication of the student's actual entry page."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from playwright.async_api import Request as BrowserRequest

from botai.browser import gateway
from botai.browser.egress import ProxyEgress
from botai.browser.forms import has_credential_fields
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.login import LoginFlow
from botai.browser.presets import ConnectionInfo, InMemoryLoginStore
from botai.browser.probe import requires_authentication
from botai.browser.proxy import EgressProxy
from botai.browser.session import EntrySessionProvider, decode_session, encode_session
from botai.contracts.agent_ui import InputBlock, LoginBlock, TextBlock
from botai.core.browser import PageSnapshot, SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled
from botai.core.vault import InMemoryVault, SecretKind
from tests.browser.conftest import PROXY_SECRET, QuietServer, Tls
from tests.browser.test_login import (
    FAST,
    ORIGIN,
    FakeLink,
    FakeScreens,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
)
from tests.fakeuni import data, deep

pytestmark = pytest.mark.browser

ENTRY = f"{ORIGIN}/private/classes?term=synthetic-private-filter"
GUEST = "synthetic-anonymous-client"
STUDENT = "synthetic-student-session"


@dataclass
class GuestStack:
    gateway: PlaywrightGateway
    egress: ProxyEgress
    visited: list[str]
    sessions: set[str]
    side_effects: dict[str, int]
    root_destination: list[str]

    def rig(self, human: Any = None) -> Rig:
        user, conn = new_id(), new_id()
        store = InMemoryLoginStore()
        store.add_connection(user, conn, ORIGIN)
        vault = InMemoryVault()
        screens = FakeScreens(human)
        flow = LoginFlow(
            gateway=self.gateway,
            vault=vault,
            store=store,
            egress=self.egress,
            screens=screens,
            timing=FAST,
        )
        return Rig(flow, store, vault, screens, self.gateway, user, conn)


@pytest.fixture(scope="module")
async def guest_stack(tls: Tls) -> AsyncIterator[GuestStack]:
    """Own isolated fakeuni server/proxy, so session fixtures of other tests remain unchanged."""
    app = FastAPI()
    visited: list[str] = []
    sessions: set[str] = set()
    side_effects = {"posts": 0, "sockets": 0}
    root_destination: list[str] = []

    def page(body: str, *, student: bool = False) -> HTMLResponse:
        script = (
            f"localStorage.setItem('workspace','ready');sessionStorage.setItem('client','{STUDENT}');"
            if student
            else f"sessionStorage.setItem('client','{GUEST}');"
        )
        response = HTMLResponse(
            "<!doctype html><title>Учебный портал</title><main>"
            + body
            + "</main><script>"
            + script
            + "fetch('/api/client-bootstrap', {headers:{Authorization:"
            "'Bearer '+sessionStorage.getItem('client')}});"
            "fetch('/data-write',{method:'POST'}).catch(()=>{});"
            "new WebSocket('wss://fakeuni.test/data-socket').onerror=()=>{};</script>"
        )
        response.set_cookie("client_device", GUEST, secure=True, httponly=True)
        return response

    @app.get("/", response_model=None)
    async def public() -> HTMLResponse | RedirectResponse:
        visited.append("/")
        if root_destination:
            return RedirectResponse(root_destination[0], status_code=302)
        return page(
            '<h1>Открытый каталог</h1><form action="/search"><label for="q">Поиск курсов</label>'
            '<input id="q" type="search" name="q"><button>Найти</button></form>'
            '<a href="/private/classes">Мои занятия</a>'
        )

    @app.get("/api/client-bootstrap")
    async def bootstrap(request: Request) -> JSONResponse:
        if request.cookies.get("client_device") == GUEST:
            return JSONResponse({"client": "anonymous-browser"})
        return JSONResponse({"error": "client-required"}, status_code=401)

    @app.post("/data-write")
    async def side_effect() -> dict[str, bool]:
        side_effects["posts"] += 1
        return {"written": True}

    @app.websocket("/data-socket")
    async def socket_side_effect(socket: WebSocket) -> None:
        side_effects["sockets"] += 1
        await socket.accept()
        await socket.close()

    @app.get("/private/classes")
    async def classes(request: Request) -> HTMLResponse:
        visited.append(str(request.url.path) + "?" + request.url.query)
        if request.cookies.get("student_session") in sessions:
            return page(
                "<h1>Мои занятия</h1><article>Семинар по тестированию</article>", student=True
            )
        return page(
            '<h1>Please sign in</h1><form action="/auth/login" method="post">'
            '<label for="u">Логин</label><input id="u" name="username" autocomplete="username">'
            '<label for="p">Пароль</label><input id="p" name="password" type="password">'
            '<button type="submit">Войти</button></form>'
        )

    @app.post("/auth/login")
    async def authenticate(username: str = Form(), password: str = Form()) -> RedirectResponse:
        response = RedirectResponse("/landing", status_code=303)
        if username == data.USERNAME and password == data.PASSWORD:
            session = new_id()
            sessions.add(session)
            response.set_cookie("student_session", session, secure=True, httponly=True)
        return response

    @app.get("/landing")
    async def landing(request: Request) -> HTMLResponse:
        return page(
            "<h1>Образовательные программы</h1>",
            student=request.cookies.get("student_session") in sessions,
        )

    @app.get("/spa-auth")
    async def spa_auth(request: Request) -> HTMLResponse:
        if request.cookies.get("student_session") in sessions:
            return page("<h1>Мои занятия</h1>", student=True)
        return page(
            '<h1>Please sign in</h1><form id="auth">'
            '<label for="u">Логин</label><input id="u" name="username" autocomplete="username">'
            '<label for="p">Пароль</label><input id="p" name="password" type="password">'
            '<button type="submit">Войти</button></form><script>'
            "document.querySelector('form').onsubmit=async e=>{e.preventDefault();"
            "const form=new URLSearchParams(new FormData(e.target));"
            "const r=await fetch('/api/client-login',{method:'POST',body:form});"
            "if(r.ok){document.querySelector('main').innerHTML='<h1>Мои занятия</h1>';"
            "fetch('/data-write',{method:'POST'}).catch(()=>{});}};</script>"
        )

    @app.post("/api/client-login")
    async def client_login(username: str = Form(), password: str = Form()) -> JSONResponse:
        if username != data.USERNAME or password != data.PASSWORD:
            return JSONResponse({"accepted": False}, status_code=401)
        session = new_id()
        sessions.add(session)
        response = JSONResponse({"accepted": True})
        response.set_cookie("student_session", session, secure=True, httponly=True)
        return response

    @app.get("/email-entry")
    async def title_auth() -> HTMLResponse:
        return HTMLResponse(
            '<!doctype html><title>Sign in</title><form action="/api/email-step" method="post">'
            '<label for="email">Email</label><input id="email" type="email" name="email" '
            'autocomplete="email"><button type="submit">Continue</button></form>'
        )

    @app.post("/api/email-step")
    async def email_step(email: str = Form()) -> JSONResponse:
        visited.append("/api/email-step")
        return JSONResponse({"accepted": email == data.USERNAME})

    @app.get("/private/canonical")
    async def canonical() -> RedirectResponse:
        return RedirectResponse("/private/classes", status_code=302)

    @app.get("/private/public-redirect")
    async def public_redirect() -> RedirectResponse:
        return RedirectResponse("/", status_code=302)

    @app.get("/private/refused")
    async def refused() -> HTMLResponse:
        return page("<h1>Please sign in</h1><p>Authentication required</p>")

    @app.get("/private/captcha")
    async def captcha() -> HTMLResponse:
        return page('<h1>Проверка</h1><iframe src="/recaptcha/challenge"></iframe>')

    @app.get("/recaptcha/challenge")
    async def challenge() -> HTMLResponse:
        return HTMLResponse("<p>Я не робот</p>")

    server = QuietServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_certfile=str(tls.cert),
            ssl_keyfile=str(tls.key),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    proxy = EgressProxy(PROXY_SECRET, test_hosts={"fakeuni.test": ("127.0.0.1", port)})
    await proxy.start()
    egress = ProxyEgress(PROXY_SECRET, proxy=proxy)
    gateway = PlaywrightGateway(egress, sandbox=False, ignore_https_errors=True)
    try:
        yield GuestStack(gateway, egress, visited, sessions, side_effects, root_destination)
    finally:
        await gateway.aclose()
        await proxy.close()
        server.should_exit = True
        await task


async def guest_session(rig: Rig) -> SessionState:
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    try:
        await handle.goto(ORIGIN + "/")
        return await handle.export_session()
    finally:
        await handle.close()


async def test_spontaneous_public_dom_change_is_not_human_login_progress(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    async def site_update(handle: PlaywrightHandle, link: FakeLink) -> None:
        await handle.page.evaluate("document.querySelector('h1').textContent += ' Обновлено'")
        await asyncio.sleep(FAST.poll_s * 3)
        assert not link.succeeded
        await link.close("synthetic public update")

    rig = guest_stack.rig(site_update)
    ctx = rig.ctx(Student())
    with pytest.raises(JobCancelled):
        await rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn)
    await rig.screens.settle()
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)


@pytest.mark.parametrize("force", [False, True])
async def test_guest_root_probe_never_finishes_without_login_or_human(
    guest_stack: GuestStack, trust_fakeuni: Path, force: bool
) -> None:
    rig = guest_stack.rig()
    if force:
        preset = preset_dict(ORIGIN + "/")
        preset["success"]["probe"]["url"] = ORIGIN + "/api/client-bootstrap"
        rig.store.seed_preset(ORIGIN, preset)
        await rig.vault.put(
            rig.user, rig.conn, SecretKind.SESSION, encode_session(await guest_session(rig))
        )
    ctx = rig.ctx(Student())
    running = asyncio.create_task(
        rig.flow.ensure(ctx, user_id=rig.user, connection_id=rig.conn, force=force)
    )
    try:
        async with asyncio.timeout(5):
            while not rig.screens.links:
                assert not running.done()
                await asyncio.sleep(0.05)
        await asyncio.sleep(FAST.poll_s * 2)
        assert not running.done()
        assert not any(
            isinstance(block, TextBlock) and "Вход выполнен" in block.markdown
            for block in ctx.blocks
        )
        assert any(isinstance(block, LoginBlock) for block in ctx.blocks)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("forced_guest", [False, True])
async def test_deep_entry_requires_real_login_and_roundtrips_storage(
    guest_stack: GuestStack, trust_fakeuni: Path, forced_guest: bool
) -> None:
    rig = guest_stack.rig()
    if forced_guest:
        preset = preset_dict(ORIGIN + "/")
        preset["success"]["probe"]["url"] = ORIGIN + "/api/client-bootstrap"
        rig.store.seed_preset(ORIGIN, preset)
        await rig.vault.put(
            rig.user, rig.conn, SecretKind.SESSION, encode_session(await guest_session(rig))
        )
    assert isinstance(rig.flow, EntrySessionProvider)
    student = Student()
    ctx = rig.ctx(student)
    state = await rig.flow.ensure_for_entry(
        ctx, user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY, force=forced_guest
    )
    assert [kind for kind, _ in student.asked] == ["login", "password"]
    assert any(isinstance(block, InputBlock) for block in ctx.blocks)
    assert "/private/classes?term=synthetic-private-filter" in guest_stack.visited
    raw = rig.store.presets[ORIGIN][-1]
    serialized = json.dumps(raw)
    assert raw["start"]["url"] == ORIGIN + "/"
    assert "synthetic-private-filter" not in serialized
    assert "/private/classes" not in serialized
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    assert stored is not None
    restored = decode_session(stored.get_secret_value())
    assert restored.browser_storage
    handle = await rig.gateway.open(
        user_id=rig.user,
        connection_id=rig.conn,
        allow_hosts=("fakeuni.test",),
        session=restored,
        read_only=True,
        background=True,
    )
    try:
        snap = await handle.goto(ENTRY)
        assert not requires_authentication(snap)
        assert "Семинар по тестированию" in snap.text
        assert await handle.page.evaluate("localStorage.getItem('workspace')") == "ready"
        assert await handle.page.evaluate("sessionStorage.length") == 1
    finally:
        await handle.close()
    reused = await rig.flow.ensure_for_entry(
        rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY
    )
    assert reused.cookies == state.cookies
    # Stored API200 alone is insufficient after the site's actual browser state expires.
    guest_stack.sessions.clear()
    relogin = Student()
    refreshed = await rig.flow.ensure_for_entry(
        rig.ctx(relogin), user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY
    )
    assert not relogin.asked  # the user's password is already sealed in the Vault
    assert refreshed.cookies != state.cookies


async def test_manual_mode_opens_screen_despite_valid_stored_probe_and_waits_for_done(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    await rig.flow.ensure_for_entry(
        rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY
    )
    await rig.vault.put(rig.user, rig.conn, SecretKind.LOGIN, data.USERNAME)
    await rig.vault.put(rig.user, rig.conn, SecretKind.PASSWORD, data.PASSWORD)

    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.human_control = True
        assert await handle.page.input_value("#u") == ""
        assert await handle.page.input_value("#p") == ""
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
        await handle.arm_human_auth()
        await handle.page.click('button[type="submit"]')
        await asyncio.sleep(FAST.poll_s * 2)
        assert not link.succeeded
        link.request_done()

    rig.screens.human = human
    student = Student()
    ctx = rig.ctx(student)
    await rig.flow.ensure_for_entry(
        ctx, user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY, manual=True
    )
    await rig.screens.settle()
    assert not student.asked
    assert not ctx.secrets_requested
    assert len(rig.screens.links) == 1
    assert rig.screens.links[0].succeeded
    assert any(isinstance(block, LoginBlock) for block in ctx.blocks)
    assert not any(isinstance(block, InputBlock) for block in ctx.blocks)


@pytest.mark.parametrize("path", ["/private/refused", "/private/captcha"])
async def test_manual_done_cannot_override_target_authentication_or_captcha(
    guest_stack: GuestStack, trust_fakeuni: Path, path: str
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.human_control = True
        # A genuine authenticated landing and a passing bootstrap API are still
        # insufficient when the requested target independently refuses access.
        await handle.page.goto(ENTRY)
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
        await handle.arm_human_auth()
        await handle.page.click('button[type="submit"]')
        link.request_done()
        await asyncio.sleep(FAST.poll_s * 3)
        assert not link.succeeded
        await link.close("synthetic incomplete login")

    rig = guest_stack.rig(human)
    ctx = rig.ctx(Student())
    with pytest.raises(JobCancelled):
        await rig.flow.ensure_for_entry(
            ctx, user_id=rig.user, connection_id=rig.conn, entry_url=ORIGIN + path, manual=True
        )
    await rig.screens.settle()
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)
    assert not any(
        isinstance(block, TextBlock) and "Вход выполнен" in block.markdown for block in ctx.blocks
    )


async def test_public_landing_after_wrong_password_cannot_hide_the_fresh_refused_entry(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    preset = preset_dict(ORIGIN + "/")
    preset["success"]["probe"]["url"] = ORIGIN + "/api/client-bootstrap"
    rig.store.seed_preset(ORIGIN, preset)
    student = Student(wrong_passwords=99)
    ctx = rig.ctx(student)
    with pytest.raises(BotaiError) as error:
        await rig.flow.ensure_for_entry(
            ctx, user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY, force=True
        )
    assert error.value.code == ErrorCode.LOGIN_FAILED
    assert student.count("password") == FAST.max_attempts
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)
    assert not any(
        isinstance(block, TextBlock) and "Вход выполнен" in block.markdown for block in ctx.blocks
    )


async def test_entry_material_canonical_redirect_requires_human_confirmation(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.request_done()

    rig = guest_stack.rig(human)
    student = Student()
    await rig.flow.ensure_for_entry(
        rig.ctx(student),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ORIGIN + "/private/canonical?filter=private",
    )
    await rig.screens.settle()
    assert [kind for kind, _ in student.asked] == ["login", "password"]
    assert len(rig.screens.links) == 1
    assert rig.screens.links[0].succeeded


async def test_entry_initial_login_redirect_and_final_documents_cannot_write(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    before = guest_stack.side_effects.copy()
    await rig.flow.ensure_for_entry(
        rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=ENTRY
    )
    assert guest_stack.side_effects == before


async def test_real_deep_portal_script_never_writes_during_login_entry_verification(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = make_rig()
    student = Student()
    before = deep.STATE.writes
    await rig.flow.ensure_for_entry(
        rig.ctx(student),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ORIGIN + "/deep-portal?begin_date=2026-10-26",
    )
    assert [kind for kind, _ in student.asked] == ["login", "password", "code"]
    assert deep.STATE.writes == before


async def test_generic_root_after_native_login_can_reach_the_real_cabinet_without_a_screen(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = make_rig()
    student = Student()
    state = await rig.flow.ensure_for_entry(
        rig.ctx(student), user_id=rig.user, connection_id=rig.conn, entry_url=ORIGIN + "/"
    )
    assert [kind for kind, _ in student.asked] == ["login", "password", "code"]
    assert any(cookie.name == "FAKEUNI_SESSION" for cookie in state.cookies)
    assert not rig.screens.links


async def test_generic_entry_and_later_legacy_reauth_keep_one_complete_canonical_preset(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = make_rig()
    await rig.flow.ensure_for_entry(
        rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=ORIGIN + "/"
    )
    first = rig.store.presets[ORIGIN][0]
    assert [step["id"] for step in first["steps"]] == ["username-password", "otp"]
    assert first["expired"]["redirectPathPrefix"] == ["/auth/"]
    await rig.flow.ensure(rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, force=True)
    assert len(rig.store.presets[ORIGIN]) == 1
    assert rig.store.presets[ORIGIN][0] == first


@pytest.mark.parametrize(
    ("entry", "allowed"),
    [
        (ORIGIN, True),
        (ORIGIN + "/", True),
        (ORIGIN + "/?view=private", False),
        (ORIGIN + "/#private", False),
    ],
)
def test_generic_root_exception_keeps_query_fragment_and_stored_redirects_strict(
    entry: str, allowed: bool
) -> None:
    info = ConnectionInfo(ORIGIN, ("fakeuni.test",))
    cabinet = PageSnapshot(url=ORIGIN + "/app", text="Мои курсы")
    assert LoginFlow._entry_ready(info, None, cabinet, entry, allow_generic_root=True) is allowed
    assert not LoginFlow._entry_ready(info, None, cabinet, entry)


async def test_stored_guest_root_to_public_redirect_never_inherits_the_native_login_exception(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    preset = preset_dict(ORIGIN + "/")
    preset["success"]["probe"]["url"] = ORIGIN + "/api/client-bootstrap"
    rig.store.seed_preset(ORIGIN, preset)
    sealed = encode_session(await guest_session(rig))
    await rig.vault.put(rig.user, rig.conn, SecretKind.SESSION, sealed)
    guest_stack.root_destination.append("/landing")
    running = asyncio.create_task(
        rig.flow.ensure_for_entry(
            rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=ORIGIN + "/"
        )
    )
    try:
        async with asyncio.timeout(5):
            while not rig.screens.links:
                assert not running.done()
                await asyncio.sleep(0.05)
        assert not running.done()
        saved = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
        assert saved is not None
        assert saved.get_secret_value() == sealed
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        guest_stack.root_destination.clear()


async def test_soft_refusal_does_not_authorize_background_writes_from_a_human_screen(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.side_effects.copy()
    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/private/refused")
        await handle.arm_human_auth()
        await handle.page.evaluate("fetch('/data-write',{method:'POST'}).catch(()=>null)")
        assert guest_stack.side_effects == before
    finally:
        await handle.close()


async def test_spa_auth_fetch_can_complete_without_granting_its_data_page_a_write(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    before = guest_stack.side_effects.copy()
    student = Student()
    state = await rig.flow.ensure_for_entry(
        rig.ctx(student),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ORIGIN + "/spa-auth",
    )
    assert [kind for kind, _ in student.asked] == ["login", "password"]
    assert any(
        cookie.name == "student_session" and cookie.value.get_secret_value() in guest_stack.sessions
        for cookie in state.cookies
    )
    assert guest_stack.side_effects == before


async def test_get_completion_cannot_leave_a_stale_auth_window_after_same_url_dom_change(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.side_effects.copy()
    try:
        async with handle.read_only_entry():
            await handle.goto(ENTRY)
        await handle.arm_human_auth()
        await handle.page.evaluate(
            "async()=>{await fetch('/api/client-bootstrap');"
            "document.querySelector('main').innerHTML='<h1>Мои занятия</h1>'; }"
        )
        async with handle.page.expect_event(
            "requestfailed", predicate=lambda request: request.url == ORIGIN + "/data-write"
        ) as refused:
            assert (
                await handle.page.evaluate(
                    "fetch('/data-write',{method:'POST'}).then(()=>true).catch(()=>false)"
                )
                is False
            )
        assert (await refused.value).method == "POST"
        assert guest_stack.side_effects == before
    finally:
        await handle.close()


async def test_fields_the_flow_is_filling_keep_a_learned_login_the_detector_would_not_see(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.side_effects["posts"]
    post = "fetch('/data-write',{method:'POST'}).then(()=>true).catch(()=>false)"
    try:
        async with handle.read_only_entry():
            snap = await handle.goto(ORIGIN + "/")  # a catalogue: one search field, no credentials
        assert not has_credential_fields(snap)
        async with handle.auth_step():
            assert await handle.page.evaluate(post) is False  # nothing says this is a login
        assert guest_stack.side_effects["posts"] == before
        async with handle.auth_step(fields=["#q"]):  # a learned step names this very field
            assert await handle.page.evaluate(post) is True
        assert guest_stack.side_effects["posts"] == before + 1
    finally:
        await handle.close()


async def test_a_document_post_ends_the_step_and_the_next_document_has_no_window(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.side_effects.copy()
    try:
        async with handle.read_only_entry():
            await handle.goto(ENTRY)
        async with handle.auth_step(fields=["#u", "#p"]):
            await handle.fill("#u", data.USERNAME)
            await handle.fill("#p", data.PASSWORD)
            await handle.click('button[type="submit"]')
        assert handle.page.url.endswith("/landing")
        assert not handle._auth_windows
        # the landing page of the new document wants to write: no step, no window
        assert (
            await handle.page.evaluate(
                "fetch('/data-write',{method:'POST'}).then(()=>true).catch(()=>false)"
            )
            is False
        )
        assert guest_stack.side_effects == before
    finally:
        await handle.close()


async def test_other_data_tab_and_iframe_cannot_inherit_the_selected_auth_page_permission(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.side_effects.copy()
    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/private/refused")
        data_tab = handle.page
        auth_tab = await handle.context.new_page()
        await auth_tab.goto(ENTRY)
        await auth_tab.evaluate(
            "()=>{const f=document.createElement('iframe');f.src='/private/refused';document.body.append(f);}"
        )
        data_frame = await auth_tab.wait_for_selector("iframe")
        frame = await data_frame.content_frame()
        assert frame is not None
        await frame.wait_for_selector("h1")
        await handle.arm_human_auth()
        assert (
            await data_tab.evaluate(
                "fetch('/data-write',{method:'POST'}).then(()=>true).catch(()=>false)"
            )
            is False
        )
        assert (
            await frame.evaluate(
                "fetch('/data-write',{method:'POST'}).then(()=>true).catch(()=>false)"
            )
            is False
        )
        assert guest_stack.side_effects == before
    finally:
        await handle.close()


@pytest.mark.parametrize("document", [False, True], ids=["xhr-post", "document-post"])
async def test_title_only_email_auth_context_is_preserved_for_both_post_kinds(
    guest_stack: GuestStack, trust_fakeuni: Path, document: bool
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.visited.count("/api/email-step")
    try:
        async with handle.read_only_entry():
            snap = await handle.goto(ORIGIN + "/email-entry")
        assert has_credential_fields(snap)
        assert not has_credential_fields(snap.model_copy(update={"title": ""}))
        await handle.fill("#email", data.USERNAME)
        async with handle.auth_step(fields=["#email"]):
            if document:
                await handle.click("button")
            else:
                assert (
                    await handle.page.evaluate(
                        "async()=>{const r=await fetch('/api/email-step',{method:'POST',"
                        "body:new URLSearchParams(new FormData(document.querySelector('form')))});"
                        "return r.ok;}"
                    )
                    is True
                )
        assert guest_stack.visited.count("/api/email-step") == before + 1
    finally:
        await handle.close()


async def test_refused_document_post_keeps_the_window_for_the_auth_form(
    guest_stack: GuestStack, trust_fakeuni: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    before = guest_stack.visited.count("/api/email-step")
    side_effects = guest_stack.side_effects.copy()
    events: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, **facts: object) -> None:
        events.append((event, facts))

    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/email-entry")
        await handle.fill("#email", data.USERNAME)
        monkeypatch.setattr(handle, "_diagnostic", emit)
        foreign = Mock(spec=BrowserRequest)
        foreign.frame = handle.page.main_frame
        foreign.method = "POST"
        foreign.url = "https://elsewhere.fakeuni.test/data-write?private=private-auth-fixture"
        foreign.is_navigation_request.return_value = True
        async with handle.auth_step(fields=["#email"]):
            assert not await handle._entry_auth_allowed(cast(BrowserRequest, foreign))
            # The refused request never reaches a server. The real submission, to a host the
            # connection logs in at, still has its step.
            await handle.click("button")
        assert guest_stack.visited.count("/api/email-step") == before + 1
        assert guest_stack.side_effects == side_effects
        refused = [facts for name, facts in events if name == "browser.auth_permit_refused"]
        assert len(refused) == 1
        assert refused[0]["reason"] == "auth_host_not_allowed"
        encoded = json.dumps(events)
        assert "private-auth-fixture" not in encoded
        assert data.USERNAME not in encoded
        assert "/api/email-step" not in encoded
    finally:
        await handle.close()


async def test_auth_submission_preserves_current_navigation_failure_and_ignores_old_failure(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/email-entry")
        stale = BotaiError(ErrorCode.EGRESS_BLOCKED, "Прежняя навигация была отклонена")
        handle._navigation_error = stale
        async with handle.auth_step():
            pass

        failure = BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Текущая навигация была отклонена")

        async def fail_navigation() -> None:
            async with handle.auth_step():
                handle._navigation_request = cast(BrowserRequest, Mock(spec=BrowserRequest))
                handle._navigation_error = failure

        with pytest.raises(BotaiError) as raised:
            await fail_navigation()
        assert raised.value is failure
    finally:
        await handle.close()


async def test_concurrent_auth_requests_share_the_budget_of_one_window(
    guest_stack: GuestStack, trust_fakeuni: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "AUTH_TYPING_POSTS", 2)
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/email-entry")
        request = Mock(spec=BrowserRequest)
        request.frame = handle.page.main_frame
        request.method = "POST"
        request.url = ORIGIN + "/api/email-step"
        request.is_navigation_request.return_value = False
        async with handle.auth_step(fields=["#email"]):
            results = await asyncio.gather(
                *(handle._entry_auth_allowed(cast(BrowserRequest, request)) for _ in range(3))
            )
        assert sorted(results) == [False, True, True]
    finally:
        await handle.close()


async def test_navigation_during_an_auth_scan_cannot_restore_the_revoked_window(
    guest_stack: GuestStack, trust_fakeuni: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = guest_stack.rig()
    handle = await rig.gateway.open(
        user_id=rig.user, connection_id=rig.conn, allow_hosts=("fakeuni.test",), origin=ORIGIN
    )
    try:
        async with handle.read_only_entry():
            await handle.goto(ORIGIN + "/email-entry")
        frame = handle.page.main_frame
        request = Mock(spec=BrowserRequest)
        request.frame = frame
        request.method = "POST"
        request.url = ORIGIN + "/api/email-step"
        request.is_navigation_request.return_value = False
        scan_started, scan_released = asyncio.Event(), asyncio.Event()
        original_evaluate = frame.evaluate

        async def paused_scan(script: str) -> Any:
            scan_started.set()
            await scan_released.wait()
            return await original_evaluate(script)

        async with handle.auth_step(fields=["#email"]):
            monkeypatch.setattr(frame, "evaluate", paused_scan)
            checking = asyncio.create_task(
                handle._entry_auth_allowed(cast(BrowserRequest, request))
            )
            async with asyncio.timeout(2):
                await scan_started.wait()
            handle._revoke_auth_document(frame)
            scan_released.set()
            assert await checking is False
            assert frame not in handle._auth_windows
    finally:
        await handle.close()


async def test_entry_trailing_slash_and_query_canonicalization_do_not_require_a_screen(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    await rig.flow.ensure_for_entry(
        rig.ctx(Student()),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ORIGIN + "/private/classes/?filter=private",
    )
    assert not rig.screens.links


async def test_stored_guest_probe_cannot_verify_a_private_to_public_redirect(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    preset = preset_dict(ORIGIN + "/")
    preset["success"]["probe"]["url"] = ORIGIN + "/api/client-bootstrap"
    rig.store.seed_preset(ORIGIN, preset)
    sealed = encode_session(await guest_session(rig))
    await rig.vault.put(rig.user, rig.conn, SecretKind.SESSION, sealed)
    ctx = rig.ctx(Student())
    running = asyncio.create_task(
        rig.flow.ensure_for_entry(
            ctx,
            user_id=rig.user,
            connection_id=rig.conn,
            entry_url=ORIGIN + "/private/public-redirect",
        )
    )
    try:
        async with asyncio.timeout(5):
            while not rig.screens.links:
                assert not running.done()
                await asyncio.sleep(0.05)
        await asyncio.sleep(FAST.poll_s * 2)
        assert not running.done()
        saved = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
        assert saved is not None
        assert saved.get_secret_value() == sealed
        assert not any(
            isinstance(block, TextBlock) and "Вход выполнен" in block.markdown
            for block in ctx.blocks
        )
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_wrong_manual_password_and_public_redirect_do_not_prove_access(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    async def human(handle: PlaywrightHandle, link: FakeLink) -> None:
        link.human_control = True
        await handle.page.goto(ENTRY)
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", "synthetic-wrong-password")
        await handle.arm_human_auth()
        await handle.page.click('button[type="submit"]')
        await asyncio.sleep(FAST.poll_s * 3)
        assert not link.succeeded
        await link.close("synthetic incomplete login")

    rig = guest_stack.rig(human)
    ctx = rig.ctx(Student())
    with pytest.raises(JobCancelled):
        await rig.flow.ensure_for_entry(
            ctx,
            user_id=rig.user,
            connection_id=rig.conn,
            entry_url=ORIGIN + "/private/public-redirect",
        )
    await rig.screens.settle()
    assert not await rig.vault.has(rig.user, rig.conn, SecretKind.SESSION)


@pytest.mark.parametrize(
    "entry",
    [
        "http://fakeuni.test/private/classes",
        "https://id.fakeuni.test/private/classes",
        "https://fakeuni.test.evil.test/private/classes",
        "https://name:password@fakeuni.test/private/classes",
        "https://fakeuni.test:444/private/classes",
        "https://fakeuni.test/\\evil.test",
        "https://fakeuni.test/private\n/classes",
    ],
)
async def test_entry_rejects_new_origins_credentials_ports_and_ambiguous_urls(
    guest_stack: GuestStack, trust_fakeuni: Path, entry: str
) -> None:
    rig = guest_stack.rig()
    before = len(guest_stack.visited)
    with pytest.raises(BotaiError) as error:
        await rig.flow.ensure_for_entry(
            rig.ctx(Student()), user_id=rig.user, connection_id=rig.conn, entry_url=entry
        )
    assert error.value.code == ErrorCode.VALIDATION
    assert len(guest_stack.visited) == before
    assert not rig.screens.links
