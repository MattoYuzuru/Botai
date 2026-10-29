"""Real API/queue/Postgres/Chromium/proxy discovery; only the university and LLM are fixtures."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from botai.browser.egress import get_egress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.browser.probe import probe_ok, requires_authentication
from botai.browser.screen import ScreenRegistry
from botai.browser.session import decode_session, encode_session
from botai.core.ids import new_id
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from tests.browser.test_login import FAST
from tests.e2e.support import (
    Asked,
    Attendance,
    Campus,
    Learner,
    Stack,
    Student,
    WireTap,
    dump_database,
    sent_to_models,
)
from tests.e2e.test_full_flow import (
    CLASSES,
    Secrets,
    check_no_marker_anywhere,
    check_nothing_leaked,
    start_worker,
    week,
)
from tests.fakeuni import data, deep
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import live_http, session_header
from tests.learn import support as ls
from tests.sources import recipes

pytestmark = [pytest.mark.db, pytest.mark.browser]
ENTRY = data.ORIGIN + "/deep-portal?begin_date=2026-10-26"


def courses_endpoint() -> dict[str, Any]:
    return {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": "/api/deep-panel"},
        "response": {
            "format": "json",
            "itemsPath": "/items",
            "fields": {
                "externalId": {"path": "/id"},
                "title": {"path": "/title", "required": True},
            },
        },
    }


class DeepPlanner(ScriptedLlm):
    """Choose only server-owned refs. Fail immediately if a real browser observation is stale."""

    def __init__(self, *, restricted: bool) -> None:
        classification = LlmResponse(
            content=json.dumps(
                {
                    "linkIndices": [],
                    "kinds": ["scheduleEvents"] if restricted else [],
                    "absentKinds": ["courses", "deadlines"] if restricted else ["deadlines"],
                    "note": "Только расписание"
                    if restricted
                    else "Подключить доступные учебные данные",
                },
                ensure_ascii=False,
            )
        )
        schedule = ls.answer([recipes.timetable_endpoint(path="/api/deep-schedule")])
        # The external-service label is observed too: the existing policy extractor checks
        # whether it states a restriction on third-party access before recipe authoring.
        warning = LlmResponse(content='{"warning":null}')
        answers = [classification, warning, schedule]
        if not restricted:
            # Explorer preserves the first discovered type's order.
            answers = [classification, warning, ls.answer([courses_endpoint()]), schedule]
        super().__init__(queue=answers)
        self.seen: list[dict[str, Any]] = []
        self.old_page: str | None = None
        self.panel_ref: str | None = None

    async def complete_observation(self, request: LlmRequest) -> LlmResponse:
        self.observation_requests.append(request)
        state = json.loads(request.messages[1].content)["observation"]
        self.seen.append(state)
        step = len(self.seen)
        action, ref, kinds, evidence = "finish", None, [], []
        if step == 1:
            # Nine harmless menu links come first. Taking the first six cannot find the course.
            courses = next(link for link in state["links"] if link["label"] == "Мои курсы")
            assert state["links"].index(courses) == 9
            assert courses["url"] == "/deep-courses"
            assert state["pages"][-1]["url"] == "/deep-portal?begin_date=2026-10-26"
            assert all("evil.localhost" not in link["url"] for link in state["links"])
            action, ref = "navigate", courses["ref"]
        elif step == 2:
            course = next(link for link in state["links"] if link["label"] == deep.COURSE_TITLE)
            action, ref = "navigate", course["ref"]
        elif step == 3:
            page = state["pages"][-1]
            assert page["url"] == "/deep-course"
            assert "Старое содержимое вкладки" in page["html"]
            self.old_page = page["ref"]
            self.panel_ref = next(
                r["ref"] for r in state["responses"] if r["url"] == "/api/deep-panel"
            )
            assert deep.COURSE_TITLE in next(
                r["body"] for r in state["responses"] if r["ref"] == self.panel_ref
            )
            action, ref, kinds, evidence = "inspect", self.old_page, ["courses"], [self.panel_ref]
        elif step == 4:
            assert self.old_page in state["details"]
            assert "Старое содержимое вкладки" in state["details"][self.old_page]
            tab = next(view for view in state["views"] if view["label"] == "Занятия")
            action, ref = "activate", tab["ref"]
        elif step == 5:
            fresh = state["pages"][-1]
            assert fresh["url"] == "/deep-course"  # A SPA panel changed without navigation.
            assert "Занятия курса" in fresh["html"], {
                "fresh": fresh,
                "responses": state["responses"],
            }
            assert CLASSES[0][0] in fresh["html"]
            assert "Старое содержимое вкладки" not in fresh["html"]
            panel = next(r for r in state["responses"] if r["ref"] == self.panel_ref)
            assert CLASSES[0][0] in panel["body"]  # Same request, newest response.
            assert self.panel_ref in state["newEvidenceRefs"]
            assert next(r for r in state["responses"] if r["url"] == "/api/deep-schedule")
            action, ref, kinds, evidence = (
                "inspect",
                self.panel_ref,
                ["scheduleEvents"],
                [fresh["ref"], self.panel_ref],
            )
        elif step == 6:
            assert CLASSES[0][0] in state["details"][self.panel_ref]
            evidence = [self.panel_ref, state["pages"][-1]["ref"]]
            kinds = ["courses", "scheduleEvents"]
        else:
            raise AssertionError("the planner must finish after the bounded deep branch")
        return LlmResponse(
            content=json.dumps(
                {
                    "stateRef": state["stateRef"],
                    "action": action,
                    "ref": ref,
                    "discoveries": kinds,
                    "evidenceRefs": evidence,
                }
            )
        )


@pytest.mark.parametrize("restricted", [False, True], ids=["automatic-kinds", "explicit-schedule"])
async def test_chat_connect_deep_spa_canary_persistence_and_model_free_sync(
    stack: Stack, wiretap: WireTap, restricted: bool
) -> None:
    env = stack.env
    llm = DeepPlanner(restricted=restricted)
    start_worker(env, llm)
    friend = env.new_client()
    account = await env.account(friend)
    response = await friend.post("/api/v1/chat/threads", json={"purpose": "connect"})
    assert response.status_code == 201, response.text
    thread = response.json()["id"]
    runtime = (await friend.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    connect_job = runtime["activeJob"]["id"]
    source_message = "Подключи вуз, вот ссылка на мои курсы: " + ENTRY
    if restricted:
        source_message += ". Нужны только пары; курсы и дедлайны не подключай."

    class ChatStudent(Student):
        sent = False

        async def _answer(self, job_id: str, block: dict[str, Any], attendance: Attendance) -> None:
            if (
                block["type"] == "input"
                and block.get("autocomplete") == "off"
                and not block.get("secret")
            ):
                assert not self.sent, "valid source and scope must not be asked twice"
                self.sent = True
                attendance.asked.append(
                    Asked("input", block.get("inputKind"), False, block["question"])
                )
                posted = await self._http.post(
                    f"/api/v1/chat/threads/{thread}/messages",
                    headers=self._headers,
                    json={
                        "text": source_message,
                        "targetJobId": job_id,
                        "clientMessageId": new_id(),
                    },
                )
                assert posted.status_code == 202, posted.text
                assert posted.json()["jobId"] == job_id
                return
            await super()._answer(job_id, block, attendance)

    async with live_http(env.app) as http:
        student = ChatStudent(http, session_header(friend))
        connected = await student.attend(connect_job)
        assert not [event for event in connected.events if event["event"] == "error"], json.dumps(
            {
                "fresh_page": llm.seen[-1]["pages"][-1] if llm.seen else None,
                "responses": llm.seen[-1]["responses"] if llm.seen else None,
                "panels": deep.STATE.panels,
            },
            ensure_ascii=False,
        )
        assert connected.result["status"] == "ok"
        assert connected.result["kinds"] == (
            ["scheduleEvents"] if restricted else ["courses", "scheduleEvents"]
        )
        assert [request.purpose for request in llm.requests] == (
            ["classify", "extract", "learn"]
            if restricted
            else ["classify", "extract", "learn", "learn"]
        )
        assert student.sent
        assert len(llm.observation_requests) == 6
        runtime = (await friend.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
        connection = runtime["connection"]
        assert connection["status"] == "active"
        assert runtime["pending"] == []
        connection_id = connection["id"]
        learner = Learner(friend, account.id, connection["spaceId"], connection_id, connect_job)
        secrets = Secrets(env, learner)
        await secrets.session("deep portal")

        events = (await friend.get("/api/v1/schedule", params=week())).json()
        assert [event["title"] for event in events] == [title for title, _, _ in CLASSES]
        assert {event["provenance"]["sourceUrl"].split("?")[0] for event in events} == {
            data.ORIGIN + "/api/deep-schedule"
        }
        today = (await friend.get("/api/v1/today")).json()
        assert [event["title"] for event in today["events"]] == [CLASSES[0][0], CLASSES[1][0]]
        courses = (await friend.get("/api/v1/courses")).json()
        assert [course["title"] for course in courses] == (
            [] if restricted else [deep.COURSE_TITLE]
        )
        assert (await friend.get("/api/v1/deadlines")).json() == []
        async with env.sm() as session:
            stored = await session.get(m.Connection, connection_id)
            assert stored is not None
            assert stored.spec_id is not None
            spec = await session.get(m.AdapterSpecRow, stored.spec_id)
            assert spec is not None
            assert spec.state == "active"
            assert {endpoint["kind"] for endpoint in spec.spec["recipe"]["endpoints"]} == set(
                connected.result["kinds"]
            )
            assert stored.allow_hosts == ["fakeuni.test"]
            thread_row = await session.get(m.ChatThread, thread)
            assert thread_row is not None
            assert thread_row.connect_context["entry_urls"] == [ENTRY]
            assert thread_row.connect_context["absent_kinds"] == (
                ["courses", "deadlines"] if restricted else ["deadlines"]
            )
        calls = len(llm.requests)
        env.advance(hours=3)
        sync_job = (await friend.post(f"/api/v1/connections/{connection_id}/sync")).json()["jobId"]
        synced = await student.attend(sync_job)
        assert synced.result["status"] == "ok"
        assert synced.asked == []
        assert len(llm.requests) == calls
        assert len(llm.observation_requests) == 6
        assert [
            event["id"] for event in (await friend.get("/api/v1/schedule", params=week())).json()
        ] == [event["id"] for event in events]
    # Login verifies the private entry before the explorer opens its fresh browser context.
    assert deep.STATE.visits[:4] == [
        "/deep-portal",
        "/deep-portal",
        "/deep-courses",
        "/deep-course",
    ]
    assert deep.STATE.panels[:2] == ["courses", "schedule"]
    assert deep.STATE.writes == 0
    assert stack.gateway.active_for(account.id) is None
    await check_nothing_leaked(env, wiretap, secrets, llm)


async def test_vault_storage_and_cookies_restore_after_browser_restart(
    stack: Stack, wiretap: WireTap
) -> None:
    """An API probe works without storage, but the browser still asks for login in that state."""
    env = stack.env
    client = env.new_client()
    account = await env.account(client)
    _, connection_id = await env.connection(account.id, origin=data.ORIGIN)
    gateway = stack.gateway
    handle = await gateway.open(
        user_id=account.id, connection_id=connection_id, allow_hosts=["fakeuni.test"]
    )
    try:
        await handle.goto(data.ORIGIN + "/deep-storage-login")
        captured = await handle.export_session()
    finally:
        await handle.close()
    assert captured.browser_storage
    await env.services.vault.put(
        account.id, connection_id, SecretKind.SESSION, encode_session(captured)
    )
    await gateway.aclose()  # Drop the process and its in-memory contexts, as at a worker restart.
    sealed = await env.services.vault.get(account.id, connection_id, SecretKind.SESSION)
    assert sealed is not None
    restored = decode_session(sealed.get_secret_value())
    restarted = PlaywrightGateway(get_egress(env.settings), sandbox=False, ignore_https_errors=True)
    try:
        no_storage = restored.model_copy(update={"browser_storage": []})
        assert await probe_ok(
            no_storage,
            {"method": "GET", "url": data.ORIGIN + "/api/v2/me", "expectStatus": 200},
            ["fakeuni.test"],
            egress=get_egress(env.settings),
        )
        without = await restarted.open(
            user_id=account.id,
            connection_id=connection_id,
            allow_hosts=["fakeuni.test"],
            session=no_storage,
            read_only=True,
            background=True,
        )
        try:
            refused = await without.goto(data.ORIGIN + "/deep-storage-page")
            assert requires_authentication(refused)
        finally:
            await without.close()
        with_storage = await restarted.open(
            user_id=account.id,
            connection_id=connection_id,
            allow_hosts=["fakeuni.test"],
            session=restored,
            read_only=True,
            background=True,
        )
        try:
            page = await with_storage.goto(data.ORIGIN + "/deep-storage-page")
            assert "Сессия браузера восстановлена" in page.text
            assert not requires_authentication(page)
            assert deep.STATE.storage_reads == 1
        finally:
            await with_storage.close()
    finally:
        await restarted.aclose()
    persisted = await dump_database(env.sm)
    model_requests = sent_to_models(env.services.llm)
    for marker in (deep.STORAGE_LOCAL, deep.STORAGE_SESSION):
        assert marker not in persisted
        assert marker not in wiretap.text
        assert marker not in model_requests


async def test_captcha_waits_for_the_human_and_cancellation_cleans_up(
    stack: Stack, wiretap: WireTap, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real screen registry opens, but no fixture or model solves the challenge."""
    env = stack.env
    llm = ScriptedLlm()
    monkeypatch.setattr(fakeuni_login, "ROOT_TARGET", "/captcha/login")
    monkeypatch.setattr(fakeuni_login, "HUMANS", set())
    screens = ScreenRegistry(
        secret=b"captcha-screen-fixture-0123456789abcdef",
        public_base_url="https://testserver",
        clock=env.clock,
    )
    env.services.sessions = LoginFlow(
        gateway=stack.gateway,
        vault=env.services.vault,
        store=PgLoginStore(env.sm),
        egress=get_egress(env.settings),
        clock=env.clock,
        timing=FAST,
        screens=screens,
    )
    start_worker(env, llm)
    learner = await Campus(env).enrol("challenge@example.com")
    secrets = Secrets(env, learner)
    try:
        async with live_http(env.app) as http:
            waiting = await Student(http, learner.headers()).attend(
                learner.connect_job, walk_away_at=lambda block: block["type"] == "login"
            )
            assert waiting.waiting_on is not None
            assert "Капчу решает только человек" in waiting.waiting_on["hint"]
            assert (await env.job(learner.connect_job)).state == "waiting_user"
            assert screens.find(learner.user_id, learner.connection_id) is not None
            assert set() == fakeuni_login.HUMANS
            assert fakeuni_login.ATTEMPTS == []  # No form submission bypassed the challenge.
            assert llm.requests == []
            assert llm.observation_requests == []
            cancelled = await http.post(
                f"/api/v1/jobs/{learner.connect_job}/cancel", headers=learner.headers()
            )
            assert cancelled.status_code == 204
        await env.wait_for(learner.connect_job, {"cancelled"})
        for _ in range(200):
            if stack.gateway.active_for(learner.user_id) is None:
                break
            await asyncio.sleep(0.05)
        assert stack.gateway.active_for(learner.user_id) is None
        assert screens.find(learner.user_id, learner.connection_id) is None
        assert (await learner.client.get("/api/v1/schedule", params=week())).json() == []
        for kind in SecretKind:
            assert not await env.services.vault.has(learner.user_id, learner.connection_id, kind)
        await check_no_marker_anywhere(env, wiretap, secrets, llm)
    finally:
        await screens.close_all()
