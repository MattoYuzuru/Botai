"""A real factory-v2 journey: observed auth, nested data, fresh runtime and another student."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import contextlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.browser.egress import get_egress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.browser.session import decode_session
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.core.vault import CipherVault, SealedSecret, SecretKind
from botai.db import models as m
from botai.jobs.research import ResearchStore
from botai.jobs.runner import Worker
from botai.worker import handler_table
from tests.browser.test_login import FAST
from tests.e2e.support import (
    Attendance,
    Campus,
    Learner,
    Stack,
    Student,
    WireTap,
    dump_database,
    leaks,
)
from tests.fakeuni import data
from tests.fakeuni import factory as factory_uni
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, live_http

pytestmark = [pytest.mark.db, pytest.mark.browser]
ENTRY = data.ORIGIN + factory_uni.ENTRY_PATH
WINDOW = {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


@pytest.fixture(autouse=True)
def _fresh_factory() -> Iterator[None]:
    factory_uni.reset()
    yield
    factory_uni.reset()


def endpoint(reference: str) -> dict[str, Any]:
    def instant(clock: str) -> dict[str, Any]:
        return {
            "compose": {
                "type": "dateTime",
                "date": {"from": "day", "path": "/date"},
                "time": {"from": "lesson", "path": "/" + clock},
                "timeZone": "Europe/Moscow",
            },
            "required": True,
        }

    return {
        "id": "schedule",
        "kind": "scheduleEvents",
        # The model selects an available observation reference, never an auth header or token.
        "requestEvidenceId": reference,
        "request": {"method": "GET", "path": factory_uni.DATA_PATH},
        "response": {
            "format": "json",
            "collections": [
                {"as": "day", "path": "/days"},
                {"as": "lesson", "path": "/lessons"},
            ],
            "fields": {
                "externalId": {"from": "lesson", "path": "/id"},
                "title": {"from": "lesson", "path": "/title", "required": True},
                "startsAt": instant("begin"),
                "endsAt": instant("end"),
                "location": {"from": "lesson", "path": "/room"},
            },
            "identity": ["externalId", "startsAt"],
        },
    }


class FactoryLlm(ScriptedLlm):
    """Fixture answers depend on the actual observation refs supplied by the executor."""

    async def complete_observation(self, request: LlmRequest) -> LlmResponse:
        self.observation_requests.append(request)
        observation = json.loads(request.messages[1].content)["observation"]
        target = next(
            row
            for row in observation["responses"]
            if row["url"].split("?", 1)[0] == factory_uni.DATA_PATH
        )
        return LlmResponse(
            content=json.dumps(
                {
                    "stateRef": observation["stateRef"],
                    "action": "finish",
                    "ref": None,
                    "discoveries": ["scheduleEvents"],
                    "evidenceRefs": [target["ref"]],
                }
            )
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        assert request.purpose == "learn"
        assert len(self.requests) == 1, "the valid observed candidate must not need author retries"
        text = "\n".join(message.content for message in request.messages)
        references = re.findall(
            r"^\[request\] GET (?:https://fakeuni[.]test)?/api/factory-week"
            r"(?:[?][^\s]*)? -> 200 [^;\n]+; evidenceRef=(r[0-9]+)$",
            text,
            flags=re.MULTILINE,
        )
        assert references, "the author must receive a current successful protected request"
        return LlmResponse(
            content=json.dumps(
                {
                    "found": True,
                    "reasoning": "Вложенные дни содержат занятия, дата родителя объединяется со временем.",
                    "doubts": [],
                    # An authored auth guess is deliberately irrelevant to the observed binding.
                    "auth": {"type": "none"},
                    "bootstrap": [],
                    "endpoints": [endpoint(references[-1])],
                },
                ensure_ascii=False,
            )
        )


async def requested_schedule(env: Env, learner: Learner, *, entry: str = ENTRY) -> None:
    async with env.sm() as session, session.begin():
        job = await session.get(m.Job, learner.connect_job)
        assert job is not None
        job.payload = {**job.payload, "url": entry, "kinds": ["scheduleEvents"]}


async def connect(
    env: Env, worker: Worker, learner: Learner, http: httpx.AsyncClient
) -> Attendance:
    claim = await worker.claim()
    assert claim is not None
    assert claim.id == learner.connect_job
    attendance = asyncio.create_task(Student(http, learner.headers()).attend(claim.id, within=180))
    try:
        await asyncio.wait_for(worker.run_job(claim), 150)
        return await asyncio.wait_for(attendance, 10)
    finally:
        if not attendance.done():
            attendance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await attendance


async def stored_session(env: Env, learner: Learner) -> tuple[str, str]:
    raw = await env.services.vault.get(learner.user_id, learner.connection_id, SecretKind.SESSION)
    assert raw is not None
    state = decode_session(raw.get_secret_value())
    cookie = next(
        c.value.get_secret_value() for c in state.cookies if c.name == fakeuni_login.COOKIE
    )
    bearer = next(
        scope.token.get_secret_value()
        for scope in state.bearer_scopes
        if scope.origin == data.ORIGIN
    )
    assert bearer == factory_uni.bearer_for_cookie(cookie)
    storage = next(store for store in state.browser_storage if store.origin == data.ORIGIN)
    assert storage.session_storage[factory_uni.STORAGE_KEY].get_secret_value() == bearer
    return cookie, bearer


async def records(env: Env, learner: Learner) -> dict[str, tuple[str, str | None, bool]]:
    async with env.sm() as session:
        rows = list(
            await session.scalars(
                select(m.ScheduleEvent).where(
                    m.ScheduleEvent.connection_id == learner.connection_id
                )
            )
        )
        return {row.external_id: (row.id, row.location_text, row.changed) for row in rows}


async def assert_private(
    env: Env,
    wiretap: WireTap,
    stack: Stack,
    llms: list[ScriptedLlm],
    attendances: list[Attendance],
    needles: dict[str, str],
    console: str,
) -> None:
    # Include discovery requests too: their response bodies and scripts share the same firewall.
    model_payloads = [
        request.model_dump(mode="json")
        for llm in llms
        for request in [*llm.requests, *llm.observation_requests]
    ]
    clear_checkpoints: list[str] = []
    assert isinstance(env.services.vault, CipherVault)
    async with env.sm() as session:
        checkpoints = list(await session.scalars(select(m.JobResearchCheckpoint)))
        assert checkpoints
        for row in checkpoints:
            # Private codec verification: no decrypted material is printed by the test.
            clear = env.services.vault.open_value(
                row.user_id,
                ResearchStore._scope(row),
                SecretKind.RESEARCH,
                SealedSecret(
                    row.kek_id, row.wrapped_dek, row.nonce, row.ciphertext, row.aad_version
                ),
            )
            clear_checkpoints.append(clear.get_secret_value())
    haystacks = {
        "model requests": json.dumps(model_payloads, ensure_ascii=False),
        "public events": json.dumps([a.events for a in attendances], ensure_ascii=False),
        "plaintext database": await dump_database(env.sm),
        "checkpoint plaintext": "\n".join(clear_checkpoints),
        "application log": wiretap.text,
        "proxy log": "\n".join(stack.proxy.lines[stack.proxy_mark :]),
        "console": console,
    }
    for label, haystack in haystacks.items():
        assert leaks(haystack, needles) == [], label


async def test_observed_cookie_bearer_nested_schedule_replays_for_new_runtime_and_another_student(
    stack: Stack,
    wiretap: WireTap,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = stack.env
    env.settings.botai_research_enabled = True
    env.settings.botai_factory_v2_enabled = True
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    monkeypatch.setattr(fakeuni_login, "ROOT_TARGET", factory_uni.ENTRY_PATH)
    author = FactoryLlm()
    env.services.llm = author
    campus = Campus(env)
    alice = await campus.enrol("factory-alice@example.com", url=ENTRY)
    await requested_schedule(env, alice)
    worker = env.worker(handler_table(), name="factory-v2-author")
    new_gateway: PlaywrightGateway | None = None
    attendances: list[Attendance] = []
    try:
        async with live_http(env.app) as http:
            learned = await connect(env, worker, alice, http)
            attendances.append(learned)
            assert learned.result["status"] == "ok"
            assert len(learned.blocks("summary")) == 1
            assert len([q for q in learned.asked if q.type == "confirm"]) == 1
            assert len(author.requests) == 1
            assert stack.gateway.active_for(alice.user_id) is None
            cookie_a, bearer_a = await stored_session(env, alice)
            before = await records(env, alice)
            assert len(before) == 4
            assert all(not values[2] for values in before.values())
            visible = (await alice.client.get("/api/v1/schedule", params=WINDOW)).json()
            assert [row["title"] for row in visible] == ["Алгебра", "Теория графов"] * 2
            assert datetime.fromisoformat(visible[0]["startsAt"]) == datetime(
                2026, 10, 28, 6, 30, tzinfo=UTC
            )
            assert datetime.fromisoformat(visible[0]["endsAt"]) == datetime(
                2026, 10, 28, 7, 50, tzinfo=UTC
            )
            assert {row["provenance"]["sourceUrl"] for row in visible} == {
                data.ORIGIN + factory_uni.DATA_PATH
            }
            async with env.sm() as session:
                connection = await session.get(m.Connection, alice.connection_id)
                assert connection is not None
                spec_id = connection.spec_id
                stored = await session.get(m.AdapterSpecRow, spec_id)
                assert stored is not None
                recipe = stored.spec["recipe"]
                assert recipe["version"] == 2
                assert recipe["auth"] == {
                    "type": "session",
                    "version": 1,
                    "bindings": {
                        "bCookiesBearer": {"cookies": True, "bearer": "observedAuthorization"}
                    },
                }
                assert recipe["endpoints"][0]["request"]["authBinding"] == "bCookiesBearer"
                assert "requestEvidenceId" not in recipe["endpoints"][0]

            # New runtime components have no access to the previous browser or author state.
            new_gateway = PlaywrightGateway(
                get_egress(env.settings), sandbox=False, ignore_https_errors=True
            )
            env.services.browser = new_gateway
            env.services.sessions = LoginFlow(
                gateway=new_gateway,
                vault=env.services.vault,
                store=PgLoginStore(env.sm),
                egress=get_egress(env.settings),
                clock=env.clock,
                timing=FAST,
            )
            no_model = ScriptedLlm()
            env.services.llm = no_model
            replay = env.worker(handler_table(), name="factory-v2-replay")
            env.advance(minutes=15)
            factory_uni.STATE.version = 2
            queued = await alice.client.post(f"/api/v1/connections/{alice.connection_id}/sync")
            assert queued.status_code == 202
            claim = await replay.claim()
            assert claim is not None
            assert claim.id == queued.json()["jobId"]
            await asyncio.wait_for(replay.run_job(claim), 60)
            synced = await env.job(claim.id)
            assert synced.state == "done"
            assert synced.result is not None
            assert synced.result["status"] == "ok"
            after = await records(env, alice)
            assert {key: row[0] for key, row in after.items()} == {
                key: row[0] for key, row in before.items()
            }
            changed = [row for row in after.values() if row[2]]
            assert len(changed) == 1
            assert changed[0][1] == "А-205"
            assert no_model.requests == no_model.observation_requests == []
            assert new_gateway.active_for(alice.user_id) is None

            # The shared recipe reuses policy only; the second student's credentials are new.
            # An explicit deep page intentionally requires observation. Origin-level connection
            # exercises ordinary shared-recipe reuse; LoginFlow follows its learned entry.
            bob = await campus.enrol("factory-bob@example.com", url=data.ORIGIN + "/")
            await requested_schedule(env, bob, entry=data.ORIGIN + "/")
            reused = await connect(env, replay, bob, http)
            attendances.append(reused)
            assert reused.result["status"] == "ok"
            assert reused.result["reused"] is True
            assert reused.blocks("summary") == []
            assert not any(question.type == "confirm" for question in reused.asked)
            cookie_b, bearer_b = await stored_session(env, bob)
            assert cookie_a != cookie_b
            assert bearer_a != bearer_b
            assert no_model.requests == no_model.observation_requests == []
            assert len(await records(env, bob)) == 4
            assert factory_uni.STATE.successful_reads >= 5
            assert len(factory_uni.STATE.session_fingerprints) == 2
            async with env.sm() as session:
                connection = await session.get(m.Connection, bob.connection_id)
                assert connection is not None
                assert connection.spec_id == spec_id
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.AdapterSpecRow)
                        .where(m.AdapterSpecRow.origin == data.ORIGIN)
                    )
                    == 1
                )
            for learner in (alice, bob):
                visible = (await learner.client.get("/api/v1/schedule", params=WINDOW)).json()
                assert len(visible) == 4
                assert {row["provenance"]["connectionId"] for row in visible} == {
                    learner.connection_id
                }
            captured = capfd.readouterr()
            await assert_private(
                env,
                wiretap,
                stack,
                [author, no_model],
                attendances,
                {
                    "password": data.PASSWORD,
                    "alice cookie": cookie_a,
                    "alice bearer": bearer_a,
                    "bob cookie": cookie_b,
                    "bob bearer": bearer_b,
                },
                captured.out + captured.err,
            )
    finally:
        if new_gateway is not None:
            await new_gateway.aclose()
