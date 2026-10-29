"""A guest bootstrap API cannot replace authentication of the student's actual entry page."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.login import LoginFlow
from botai.browser.presets import InMemoryLoginStore
from botai.browser.probe import requires_authentication
from botai.browser.proxy import EgressProxy
from botai.browser.session import EntrySessionProvider, decode_session, encode_session
from botai.contracts.agent_ui import InputBlock, LoginBlock, TextBlock
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled
from botai.core.vault import InMemoryVault, SecretKind
from tests.browser.conftest import PROXY_SECRET, QuietServer, Tls
from tests.browser.test_login import FAST, ORIGIN, FakeLink, FakeScreens, Rig, Student, preset_dict
from tests.fakeuni import data

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
            "'Bearer '+sessionStorage.getItem('client')}});</script>"
        )
        response.set_cookie("client_device", GUEST, secure=True, httponly=True)
        return response

    @app.get("/")
    async def public() -> HTMLResponse:
        visited.append("/")
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

    @app.get("/private/canonical")
    async def canonical() -> RedirectResponse:
        return RedirectResponse("/private/classes", status_code=302)

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
        yield GuestStack(gateway, egress, visited, sessions)
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
        assert await handle.page.input_value("#u") == ""
        assert await handle.page.input_value("#p") == ""
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
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
        # A genuine authenticated landing and a passing bootstrap API are still
        # insufficient when the requested target independently refuses access.
        await handle.page.goto(ENTRY)
        await handle.page.fill("#u", data.USERNAME)
        await handle.page.fill("#p", data.PASSWORD)
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


async def test_entry_allows_source_canonical_redirect_after_actual_login(
    guest_stack: GuestStack, trust_fakeuni: Path
) -> None:
    rig = guest_stack.rig()
    student = Student()
    await rig.flow.ensure_for_entry(
        rig.ctx(student),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ORIGIN + "/private/canonical?filter=private",
    )
    assert [kind for kind, _ in student.asked] == ["login", "password"]


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
