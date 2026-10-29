"""CHAT -> protected LoginFlow -> persisted recipe, on real PG/Chromium/proxy."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from websockets.asyncio.client import connect
from websockets.typing import Origin

from botai.browser.egress import get_egress
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.browser.router import get_login_store, get_screens_dep
from botai.browser.screen import ScreenRegistry
from botai.browser.session import encode_session
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn import revisions
from tests.agent.rig import call, final, step
from tests.browser.test_login import FAST
from tests.browser.test_screen import PUBLIC, ScreenClient
from tests.e2e.support import Asked, Attendance, Campus, Stack, Student, WireTap, sent_to_models
from tests.e2e.test_full_flow import CLASSES, Secrets, start_worker, week
from tests.fakeuni import data
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import live_http, session_header
from tests.sources import recipes
from tests.sources.helpers import session_state

pytestmark = [pytest.mark.db, pytest.mark.browser]
ENTRY = data.ORIGIN + "/app?begin_date=2026-10-26"


async def test_chat_manual_login_opens_real_screen_then_replays_saved_recipe_without_model(
    stack: Stack, wiretap: WireTap, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    learner = await Campus(env).enrol("manual-recovery@example.com", url=ENTRY)
    client, connection_id = learner.client, learner.connection_id
    attached = await client.post(f"/api/v1/connections/{connection_id}/conversation", json={})
    assert attached.status_code == 200
    thread = attached.json()["id"]
    intent = {
        "entry_urls": [ENTRY],
        "requested_kinds": ["scheduleEvents"],
        "absent_kinds": ["deadlines"],
        "user_note": "Дедлайнов здесь нет",
        "discovery_mode": "auto",
    }
    async with env.sm() as session:
        spec = await revisions.add_revision(
            session,
            origin=data.ORIGIN,
            adapter="recipe",
            spec=recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",)),
            state="active",
        )
        conn = await session.get(m.Connection, connection_id)
        old = await session.get(m.Job, learner.connect_job)
        conversation = await session.get(m.ChatThread, thread)
        assert conn is not None
        assert old is not None
        assert conversation is not None
        conn.adapter, conn.spec_id, conn.spec_revision = "recipe", spec.id, spec.revision
        conn.status, conn.error_code, conn.data_scope = "needs_reauth", "source_auth_required", None
        conversation.connect_context = {**conversation.connect_context, **intent}
        old.state, old.payload = "done", {"url": ENTRY, "learning_intent": intent}
        await session.commit()
        spec_id = spec.id
    # Saved credentials must not turn an explicit human request into automatic login.
    for kind, value in [
        (SecretKind.LOGIN, data.USERNAME),
        (SecretKind.PASSWORD, data.PASSWORD),
        (SecretKind.SESSION, encode_session(session_state("stale-manual-cookie"))),
    ]:
        await env.services.vault.put(learner.user_id, connection_id, kind, value)
    secrets = Secrets(env, learner)
    await secrets.session("stale")
    screens = ScreenRegistry(
        secret=b"manual-e2e-screen-key-012345678901", public_base_url=PUBLIC, clock=env.clock
    )
    store = PgLoginStore(env.sm)
    env.app.dependency_overrides[get_screens_dep] = lambda: screens
    env.app.dependency_overrides[get_login_store] = lambda: store
    env.services.sessions = LoginFlow(
        gateway=stack.gateway,
        vault=env.services.vault,
        store=store,
        egress=get_egress(env.settings),
        screens=screens,
        clock=env.clock,
        timing=FAST,
    )
    llm = ScriptedLlm(
        queue=[
            step(call("request_login", connection_id=connection_id)),
            final("Запросил защищённый вход в разговоре подключения. Не присылайте пароль в чат."),
        ]
    )
    start_worker(env, llm)
    entry_verified_after_done: list[bool] = []
    writes: list[str] = []
    viewers: list[ScreenClient] = []

    class Human(Student):
        async def _answer(self, job_id: str, block: dict[str, Any], attendance: Attendance) -> None:
            assert block["type"] == "login", "manual login must not emit native fields"
            attendance.asked.append(Asked("login", None, False, "Защищённый вход"))
            offer = await self._http.post(
                f"/api/v1/connections/{connection_id}/login-session", headers=self._headers
            )
            assert offer.status_code == 201
            screen = screens.get(offer.json()["sessionId"])
            assert screen is not None
            page = screen.handle.page
            assert await page.input_value("#username") == ""
            assert await page.input_value("#password") == ""
            said_done = False

            def request_seen(request: Any) -> None:
                if request.url == ENTRY and request.is_navigation_request():
                    entry_verified_after_done.append(said_done)
                if request.method == "POST":
                    writes.append(request.url)

            page.on("request", request_seen)
            base = str(self._http.base_url).rstrip("/").replace("http://", "ws://")
            viewer = ScreenClient(
                await connect(
                    base
                    + "/api/v1/login-sessions/"
                    + offer.json()["sessionId"]
                    + "/ws?ticket="
                    + offer.json()["ticket"],
                    origin=Origin(PUBLIC),
                    max_size=None,
                )
            )
            viewers.append(viewer)
            await viewer.first_frame()
            box = await page.locator("#username").bounding_box()
            assert box is not None
            await viewer.send(t="mouse", kind="click", x=box["x"] + 5, y=box["y"] + 5)
            # Synthetic human input; LoginFlow and its protected screen remain real.
            await page.fill("#username", data.USERNAME)
            await page.fill("#password", data.PASSWORD)
            await page.click("button[type=submit]")
            await page.wait_for_url(data.ORIGIN + "/app")
            await asyncio.sleep(FAST.poll_s * 2)
            assert (await env.job(job_id)).state == "waiting_user"
            assert entry_verified_after_done == []
            said_done = True
            await viewer.send(t="done")
            await viewer.until(lambda: bool(viewer.of("success")))

    try:
        async with live_http(env.app) as http:
            student = Human(http, session_header(client))
            posted = await client.post(
                f"/api/v1/chat/threads/{thread}/messages",
                json={"text": "Откройте защищённый браузер, я войду в кабинет сам"},
            )
            assert posted.status_code == 202
            chat_id = posted.json()["jobId"]
            assert (await env.job(chat_id)).kind == "chat"
            await student.attend(chat_id)
            runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
            manual_id = runtime["activeJob"]["id"]
            manual = await env.job(manual_id)
            assert manual.kind == "connect"
            assert manual.thread_id == thread
            assert manual.payload["manual_login"] is True
            assert manual.payload["learning_intent"]["discovery_mode"] == "auto"
            assert manual.payload["url"] == ENTRY
            result = await student.attend(manual_id)
            assert result.result == {"status": "ok", "login_resumed": True}
            assert [asked.type for asked in result.asked] == ["login"]
            assert entry_verified_after_done == [True]
            assert writes == [data.ORIGIN + "/auth/login"]
            assert [request.purpose for request in llm.requests] == ["chat", "chat"]
            assert llm.observation_requests == []
            events = (await client.get("/api/v1/schedule", params=week())).json()
            assert [event["title"] for event in events] == [item[0] for item in CLASSES]
            async with env.sm() as session:
                conn = await session.get(m.Connection, connection_id)
                assert conn is not None
                assert conn.status == "active"
                assert conn.spec_id == spec_id
                assert conn.data_scope is None
                assert await session.scalar(select(func.count()).select_from(m.AdapterSpecRow)) == 1
            await secrets.session("manual")
            history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).text
            for marker in secrets.needles.values():
                assert marker not in sent_to_models(llm)
                assert marker not in history
                assert marker not in wiretap.text
    finally:
        for viewer in viewers:
            await viewer.close()
        await screens.close_all()
