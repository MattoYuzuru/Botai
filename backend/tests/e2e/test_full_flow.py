"""The whole backend in one process against the synthetic university (W7).

Every stream tested its part with doubles of the others. Here nothing is a double except the
university and the language model: the API, the job runner on a committed database, the handler
table of the worker, the real Chromium logging in through the real EgressProxy over HTTPS, the
Explorer authoring a spec from a ScriptedLlm, the sync Runner and its scheduler, the repair, the
domain API and the chat.

The tests play the student exactly as the PWA does: they follow the SSE stream of a job and
answer its questions over `POST /api/v1/jobs/{id}/answers`.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from botai.browser.session import decode_session
from botai.core.ids import new_id
from botai.core.llm import LlmResponse, ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.jobs.runner import schedule_syncs
from botai.sources import runner as sources_runner
from botai.worker import handler_table
from tests.agent.rig import call, final, step
from tests.e2e.support import (
    SOURCE_TITLE,
    WRONG_PASSWORD,
    Attendance,
    Campus,
    Learner,
    Stack,
    Student,
    WireTap,
    connect_in_thread,
    dump_database,
    leaks,
    sent_to_models,
)
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import data
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, live_http, session_header
from tests.learn import support as ls
from tests.sources import recipes
from tests.sources.helpers import FAST

pytestmark = [pytest.mark.db, pytest.mark.browser]

# The first morning of the fakeuni week: 08:00 in Moscow, so "today" holds two classes.
MONDAY_MORNING = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
QUESTION = "Какая у меня первая пара в понедельник?"
CLASSES = [
    ("Математический анализ", "А-101", datetime(2026, 10, 26, 6, 30, tzinfo=UTC)),
    ("Алгоритмы и структуры данных", "Б-204", datetime(2026, 10, 26, 8, 10, tzinfo=UTC)),
    ("Базы данных", "В-310", datetime(2026, 10, 28, 10, 0, tzinfo=UTC)),
    ("Английский язык", "online", datetime(2026, 10, 30, 12, 10, tzinfo=UTC)),
]
DEADLINES = ["ДЗ 3: деревья отрезков", "Лабораторная 2: индексы"]
CONNECT_QUESTIONS = ["input:email", "input:password (secret)", "input:code (secret)"]


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def week() -> dict[str, str]:
    return {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


class Secrets:
    """What must never leave the Vault, collected as the run produces it: the university's
    password and code, and every session cookie the university ever issued."""

    def __init__(self, env: Env, learner: Learner) -> None:
        self._env, self._learner = env, learner
        self.user_id = learner.user_id
        self.needles = {
            "password": data.PASSWORD,
            "code": data.OTP,
            "mistyped password": WRONG_PASSWORD,
        }

    async def session(self, label: str) -> str:
        """The cookie of the session in the Vault now; it joins the things that must not leak."""
        learner = self._learner
        raw = await self._env.services.vault.get(
            learner.user_id, learner.connection_id, SecretKind.SESSION
        )
        assert raw is not None
        cookie = decode_session(raw.get_secret_value()).cookies[0].value.get_secret_value()
        assert cookie not in self.needles.values(), "the same session twice"
        self.needles[f"{label} session"] = cookie
        return cookie


async def latest_job(env: Env, connection_id: str, kind: str) -> str:
    async with env.sm() as s:
        job_id = await s.scalar(
            select(m.Job.id)
            .where(m.Job.connection_id == connection_id, m.Job.kind == kind)
            .order_by(m.Job.id.desc())
            .limit(1)
        )
    assert job_id is not None
    return job_id


def start_worker(env: Env, llm: ScriptedLlm) -> None:
    """The first Monday morning, a model that knows the university, the worker of production."""
    env.clock.advance(MONDAY_MORNING - env.clock.now())
    env.services.llm = llm
    env.start(env.worker(handler_table()))  # the real handlers: connect, sync, repair, chat, ...


# =================================================================================================
# The story of the brief: a student connects a university and asks about it
# =================================================================================================


async def test_a_student_connects_a_university_and_asks_about_it(
    stack: Stack,
    wiretap: WireTap,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, gateway = stack.env, stack.gateway
    learning_llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    start_worker(env, learning_llm)

    # === 1. the API: the owner invites a friend, the friend registers and adds a university =====
    anna = await Campus(env).enrol("anna@example.com")
    friend, connection_id = anna.client, anna.connection_id
    secrets = Secrets(env, anna)

    async with live_http(env.app) as http:
        student = Student(http, anna.headers())
        statuses: list[str] = []

        async def connection_status(_block: dict[str, Any]) -> None:
            status = (await friend.get(f"/api/v1/connections/{connection_id}")).json()["status"]
            statuses.append(status)

        # === 2-4. connect: Chromium logs in through the proxy, the Explorer learns, sync runs ===
        connect = await student.attend(anna.connect_job, on_question=connection_status)
        await check_connect_job(env, connect, connection_id)
        assert statuses == ["learning"] * 4  # every question came while the connection was set up
        assert len(learning_llm.requests) == 2  # one authoring call per kind of data
        assert gateway.active_for(anna.user_id) is None  # no browser context outlives its job
        first_session = await secrets.session("first")

        # === 5. what the student sees in the app ================================================
        await check_the_app_shows_fakeuni(env, friend, connection_id, anna.space_id)
        await check_nothing_leaked(env, wiretap, secrets, learning_llm)

        # === 6a. the student presses "sync"; the university has forgotten the session: login and
        #         password come from the Vault, only the SMS code is asked ======================
        env.advance(hours=3)
        fakeuni_api.expire_sessions()
        statuses.clear()
        job = (await friend.post(f"/api/v1/connections/{connection_id}/sync")).json()["jobId"]
        relogin = await student.attend(job, on_question=connection_status)
        assert [a.label for a in relogin.asked] == ["input:code (secret)"]
        assert statuses == ["needs_reauth"]  # what the app shows while the code is awaited
        assert relogin.result["status"] == "ok"
        second_session = await secrets.session("second")
        assert second_session != first_session

        # === 6b. the scheduler's turn, the code is not asked this time: nobody is bothered ======
        connection = (await friend.get(f"/api/v1/connections/{connection_id}")).json()
        assert when(connection["lastSyncAt"]) == env.clock.now()
        env.clock.advance(when(connection["nextSyncAt"]) - env.clock.now() + timedelta(minutes=1))
        fakeuni_api.expire_sessions()
        monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
        assert await schedule_syncs(env.services) == 1
        silent = await student.attend(await latest_job(env, connection_id, "sync"))
        assert silent.asked == []
        assert "waiting_user" not in silent.states()
        assert silent.result["status"] == "ok"
        assert await secrets.session("third") not in {first_session, second_session}
        assert gateway.active_for(anna.user_id) is None

        refreshed = (await friend.get(f"/api/v1/connections/{connection_id}")).json()
        assert (refreshed["status"], refreshed["errorCode"]) == ("active", None)
        assert when(refreshed["lastSyncAt"]) == env.clock.now()
        events = (await friend.get("/api/v1/schedule", params=week())).json()
        assert [e["title"] for e in events] == [title for title, _, _ in CLASSES]  # still there
        assert {when(e["provenance"]["fetchedAt"]) for e in events} == {env.clock.now()}
        assert len(learning_llm.requests) == 2  # a sync never calls a model

        # === 7. the chat answers from the synced data, with a source ============================
        chat_llm = ScriptedLlm(
            queue=[
                step(call("get_schedule", from_date="2026-10-26", to_date="2026-10-26")),
                final(
                    "Первая пара в понедельник — «Математический анализ» в 09:30, ауд. А-101 [S1]."
                ),
            ]
        )
        env.services.llm = chat_llm
        thread = (await friend.post("/api/v1/chat/threads", json={"spaceId": anna.space_id})).json()
        posted = await friend.post(
            f"/api/v1/chat/threads/{thread['id']}/messages", json={"text": QUESTION}
        )
        assert posted.status_code == 202, posted.text
        chat = await student.attend(posted.json()["jobId"])
        await check_chat_answer(env, friend, thread["id"], chat, chat_llm)

    # === the whole run ============================================================================
    await check_nothing_leaked(env, wiretap, secrets, learning_llm, chat_llm, console=capfd)
    check_egress(stack)
    await check_jobs(env, anna.user_id)
    await check_registry(env)
    await check_isolation(env, connection_id, anna.connect_job, thread["id"])


async def test_chat_source_url_native_login_clarification_sync_and_reply_share_one_thread(
    stack: Stack,
    wiretap: WireTap,
) -> None:
    """Real TCP API/worker/Chromium/proxy; only fakeuni and the model are synthetic."""
    env = stack.env
    entry = "https://fakeuni.test/app?begin_date=2026-10-26"
    clarification = "Ссылка уже правильная. Дедлайнов тут нет."
    classifier = LlmResponse(
        content=json.dumps(
            {
                "linkIndices": [],
                "kinds": ["scheduleEvents"],
                "absentKinds": ["deadlines"],
                "note": clarification,
            },
            ensure_ascii=False,
        )
    )
    learning_llm = ls.EvidenceLlm(queue=[classifier, ls.timetable_answer()])
    start_worker(env, learning_llm)
    friend = env.new_client()
    account = await env.account(friend)
    response = await friend.post("/api/v1/chat/threads", json={})
    assert response.status_code == 201, response.text
    thread = response.json()["id"]
    # The student sends the link in an ordinary thread; its agent connects it right here.
    connect_job, connection_id = await connect_in_thread(env, account.id, thread, entry)
    initial = (await friend.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert initial["activeJob"]["id"] == connect_job
    assert [task["id"] for task in initial["tasks"]] == [connect_job]
    accepted_ids: list[str] = []
    clarified = False

    async def on_question(block: dict[str, Any]) -> None:
        nonlocal clarified
        if not clarified and block.get("autocomplete") == "username":
            runtime = (await friend.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
            assert runtime["pending"][0]["channel"] == "native"
            assert runtime["pending"][0]["secret"] is False
            posted = await friend.post(
                f"/api/v1/chat/threads/{thread}/messages",
                json={
                    "text": clarification,
                    "targetJobId": connect_job,
                    "clientMessageId": new_id(),
                },
            )
            assert posted.status_code == 202, posted.text
            assert posted.json()["jobId"] == connect_job
            accepted_ids.append(posted.json()["message"]["id"])
            clarified = True

    async with live_http(env.app) as http:
        # The source URL is never asked again: the first question is the university's login.
        student = Student(http, session_header(friend))
        connect = await student.attend(connect_job, on_question=on_question)
        assert clarified
        # The URL came with the task, so only the clarification needs the classifier.
        assert [request.purpose for request in learning_llm.requests] == ["classify", "learn"]
        assert json.loads(learning_llm.requests[0].messages[-1].content)["message"] == clarification
        assert [asked.label for asked in connect.asked] == [*CONNECT_QUESTIONS, "confirm"]
        assert connect.result["status"] == "ok"
        assert connect.result["kinds"] == ["scheduleEvents"]
        assert [request.purpose for request in learning_llm.requests] == ["classify", "learn"]
        runtime = (await friend.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
        assert runtime["pending"] == []
        assert runtime["activeJob"] is None  # a finished task is not active; it stays in tasks
        assert [(task["id"], task["state"]) for task in runtime["tasks"]] == [(connect_job, "done")]
        connection = (await friend.get(f"/api/v1/connections/{connection_id}")).json()
        assert connection["status"] == "active"
        assert connection["title"] == SOURCE_TITLE
        space_id = connection["spaceId"]
        learner = Learner(friend, account.id, space_id, connection_id, connect_job)
        secrets = Secrets(env, learner)
        await secrets.session("chat source")
        assert {
            receipt["messageId"]
            for receipt in runtime["receipts"]
            if receipt["status"] == "applied"
        } >= set(accepted_ids)
        async with env.sm() as session:
            job = await session.get(m.Job, connect_job)
            assert job is not None
            assert job.thread_id == thread
            # The clarification narrowed what the task learns; the task keeps it, not the thread.
            assert job.payload["learning_intent"]["entry_urls"] == [entry]
            assert job.payload["learning_intent"]["absent_kinds"] == ["deadlines"]
            assert job.payload["learning_intent"]["user_note"] == clarification
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(m.Job)
                    .where(m.Job.user_id == account.id, m.Job.kind == "connect")
                )
                == 1
            )
        events = (await friend.get("/api/v1/schedule", params=week())).json()
        assert [event["title"] for event in events] == [title for title, _, _ in CLASSES]
        assert (await friend.get("/api/v1/deadlines")).json() == []
        history = await friend.get(f"/api/v1/chat/threads/{thread}/messages")
        source_ids = {message["id"] for message in history.json()}
        assert all(message["jobId"] == connect_job for message in history.json())
        assert data.USERNAME not in history.text
        assert leaks(history.text, secrets.needles) == []
        assert all(
            block["type"] != "login" for message in history.json() for block in message["blocks"]
        )

        sync_job = (await friend.post(f"/api/v1/connections/{connection_id}/sync")).json()["jobId"]
        bound = await friend.post(
            f"/api/v1/connections/{connection_id}/conversation", json={"jobId": sync_job}
        )
        assert bound.status_code == 200, bound.text
        assert bound.json()["id"] == thread
        sync = await student.attend(sync_job)
        assert sync.result["status"] == "ok"
        assert len(learning_llm.requests) == 2  # Sync is deterministic; it does not ask a model.

        chat_llm = ScriptedLlm(
            queue=[
                step(call("get_schedule", from_date="2026-10-26", to_date="2026-10-26")),
                final(
                    "Первая пара в понедельник — «Математический анализ» в 09:30, ауд. А-101 [S1]."
                ),
            ]
        )
        env.services.llm = chat_llm
        posted = await friend.post(
            f"/api/v1/chat/threads/{thread}/messages",
            json={"text": QUESTION, "clientMessageId": new_id()},
        )
        assert posted.status_code == 202, posted.text
        chat = await student.attend(posted.json()["jobId"])
        assert chat.result["tools"] == ["get_schedule"]
        history = (await friend.get(f"/api/v1/chat/threads/{thread}/messages")).json()
        assert source_ids <= {message["id"] for message in history}
        reply = next(
            message
            for message in history
            if message["jobId"] == chat.job_id and message["role"] == "agent"
        )
        assert "Математический анализ" in reply["blocks"][0]["markdown"]
        cited = reply["blocks"][0]["sources"][0]["url"]
        assert "/api/" not in cited  # the page the student opens, never the API (FR-042)
    await check_nothing_leaked(env, wiretap, secrets, learning_llm, chat_llm)
    assert stack.gateway.active_for(account.id) is None


# =================================================================================================
# The university changes its API: the sync notices, the repair is queued and done by the runner
# =================================================================================================


async def test_a_changed_university_is_repaired_and_the_data_come_back(stack: Stack) -> None:
    env, gateway = stack.env, stack.gateway
    moved = recipes.timetable_endpoint(path="/api/v3/schedule")
    llm = ls.EvidenceLlm(
        queue=[ls.timetable_answer(), ls.deadlines_answer(), ls.answer([moved])]  # ..., repair
    )
    start_worker(env, llm)
    anna = await Campus(env).enrol("anna@example.com")

    async with live_http(env.app) as http:
        student = Student(http, anna.headers())
        connect = await student.attend(anna.connect_job)
        assert connect.result["status"] == "ok"
        before = (await anna.client.get("/api/v1/schedule", params=week())).json()
        assert len(before) == 4

        # the university moves the timetable to another address; its cabinet follows
        fakeuni_api.STATE.timetable_version = "v3"
        env.advance(hours=3)
        sync = await student.attend(
            (await anna.client.post(f"/api/v1/connections/{anna.connection_id}/sync")).json()[
                "jobId"
            ]
        )
        assert sync.result["status"] == "repairing"
        assert sync.result["drift"][0]["kind"] == "scheduleEvents"
        shown = (await anna.client.get(f"/api/v1/connections/{anna.connection_id}")).json()
        assert (shown["status"], shown["errorCode"]) == ("repairing", "recipe_drift")
        # while it is repaired the old data stay on screen
        kept = (await anna.client.get("/api/v1/schedule", params=week())).json()
        assert [e["id"] for e in kept] == [e["id"] for e in before]
        today = (await anna.client.get("/api/v1/today")).json()
        assert today["attention"] == [anna.connection_id]

        # the sync queued the repair; the runner took it, nobody had to ask
        repair = await student.attend(await latest_job(env, anna.connection_id, "repair"))
        assert repair.asked == []
        assert repair.result["status"] == "repaired"
        assert [r.purpose for r in llm.requests] == ["learn", "learn", "repair"]
        told = [b["markdown"] for b in repair.blocks("text")]
        assert any("`/api/v2/timetable` → `/api/v3/schedule`" in line for line in told)
        assert gateway.active_for(anna.user_id) is None

        # the data are back, read by the new revision from the new address
        connection = (await anna.client.get(f"/api/v1/connections/{anna.connection_id}")).json()
        assert (connection["status"], connection["errorCode"]) == ("active", None)
        after = (await anna.client.get("/api/v1/schedule", params=week())).json()
        assert [e["id"] for e in after] == [e["id"] for e in before]  # the same classes
        assert {e["provenance"]["sourceUrl"].split("?")[0] for e in after} == {
            "https://fakeuni.test/api/v3/schedule"
        }
        assert (await anna.client.get("/api/v1/today")).json()["attention"] == []
        async with env.sm() as s:
            rows = (
                await s.execute(
                    select(m.AdapterSpecRow.revision, m.AdapterSpecRow.state)
                    .where(m.AdapterSpecRow.origin == data.ORIGIN)
                    .order_by(m.AdapterSpecRow.revision)
                )
            ).all()
        assert [tuple(r) for r in rows] == [(1, "deprecated"), (2, "active")]

        # and the next sync, on the new revision, calls no model
        env.advance(hours=3)
        again = await student.attend(
            (await anna.client.post(f"/api/v1/connections/{anna.connection_id}/sync")).json()[
                "jobId"
            ]
        )
        assert again.result["status"] == "ok"
        assert len(llm.requests) == 3


# =================================================================================================
# A second student of the same university: what the first one taught is reused
# =================================================================================================


async def test_a_second_student_of_the_university_reuses_what_the_first_one_taught(
    stack: Stack,
) -> None:
    env = stack.env
    llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    start_worker(env, llm)
    campus = Campus(env)
    anna = await campus.enrol("anna@example.com")

    async with live_http(env.app) as http:
        assert (await Student(http, anna.headers()).attend(anna.connect_job)).result[
            "status"
        ] == "ok"
        assert len(llm.requests) == 2

        boris = await campus.enrol("boris@example.com")
        connect = await Student(http, boris.headers()).attend(boris.connect_job)
        # no description to write, nothing to confirm: he is only asked for his own credentials
        assert [a.label for a in connect.asked] == CONNECT_QUESTIONS
        assert connect.result["status"] == "ok"
        assert connect.result["reused"] is True
        assert len(llm.requests) == 2

    # each sees his own rows, read from the same learned description
    for who in (anna, boris):
        events = (await who.client.get("/api/v1/schedule", params=week())).json()
        assert [e["title"] for e in events] == [title for title, _, _ in CLASSES]
        assert {e["provenance"]["connectionId"] for e in events} == {who.connection_id}
        assert {e["provenance"]["spaceId"] for e in events} == {who.space_id}
    async with env.sm() as s:
        connections = (
            await s.execute(
                select(m.Connection.user_id, m.Connection.spec_id, m.Connection.status).where(
                    m.Connection.id.in_([anna.connection_id, boris.connection_id])
                )
            )
        ).all()
        events = await s.scalar(select(func.count()).select_from(m.ScheduleEvent))
    assert {c.status for c in connections} == {"active"}
    assert len({c.spec_id for c in connections}) == 1  # one description, two students
    assert events == 8
    await check_registry(env)


# =================================================================================================
# The unhappy paths of the same journey: a typo in the password, and walking away
# =================================================================================================


async def test_a_mistyped_password_is_asked_again_and_only_the_right_one_is_kept(
    stack: Stack, wiretap: WireTap
) -> None:
    env = stack.env
    llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    start_worker(env, llm)
    anna = await Campus(env).enrol("anna@example.com")
    secrets = Secrets(env, anna)

    async with live_http(env.app) as http:
        connect = await Student(http, anna.headers(), typos=1).attend(anna.connect_job)

    # the university refused once; the app showed the form again with what went wrong
    assert [a.label for a in connect.asked] == [
        "input:email",
        "input:password (secret)",
        "input:email",
        "input:password (secret)",
        "input:code (secret)",
        "confirm",
    ]
    for again in connect.asked[2:4]:
        assert "не принял" in again.question
        assert "попытка 2 из 3" in again.question
    assert fakeuni_login.ATTEMPTS == [("auth", "user")]  # one wrong try reached the university
    assert connect.result["status"] == "ok"
    kept = await env.services.vault.get(anna.user_id, anna.connection_id, SecretKind.PASSWORD)
    assert kept is not None
    assert kept.get_secret_value() == data.PASSWORD  # not the typo
    await secrets.session("only")
    await check_nothing_leaked(env, wiretap, secrets, llm)  # the typo leaked nowhere either


async def test_an_outage_is_retried_by_the_runner_while_the_old_data_stay(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    start_worker(env, llm)
    monkeypatch.setattr(sources_runner, "RetryPolicy", lambda: FAST)  # not 0.5 + 2 + 8 s of sleep
    anna = await Campus(env).enrol("anna@example.com")

    async with live_http(env.app) as http:
        student = Student(http, anna.headers())
        assert (await student.attend(anna.connect_job)).result["status"] == "ok"
        before = (await anna.client.get("/api/v1/schedule", params=week())).json()

        # the university goes down: the sync cannot read it, and tells the runner to come back
        fakeuni_api.STATE.fail_status = 503
        env.advance(hours=3)
        job = (await anna.client.post(f"/api/v1/connections/{anna.connection_id}/sync")).json()[
            "jobId"
        ]
        for _ in range(400):
            waiting = await env.job(job)
            if waiting.stage and "повторю через 1 мин" in waiting.stage:
                break
            await asyncio.sleep(0.05)
        assert (waiting.state, waiting.attempts) == ("queued", 1)
        assert waiting.run_after == env.clock.now() + timedelta(minutes=1)
        shown = (await anna.client.get(f"/api/v1/connections/{anna.connection_id}")).json()
        assert (shown["status"], shown["errorCode"]) == ("active", "source_unavailable")
        # the student keeps seeing the last good data, and the app does not call it a failure
        assert (await anna.client.get("/api/v1/schedule", params=week())).json() == before
        assert (await anna.client.get("/api/v1/today")).json()["attention"] == []

        # the university is back; a minute later the runner takes the job again
        fakeuni_api.STATE.fail_status = None
        env.advance(minutes=2)
        retried = await student.attend(job)
    assert retried.result["status"] == "ok"
    assert (await env.job(job)).attempts == 2
    shown = (await anna.client.get(f"/api/v1/connections/{anna.connection_id}")).json()
    assert (shown["status"], shown["errorCode"]) == ("active", None)
    assert len(llm.requests) == 2  # the model was not asked about an outage


async def test_a_student_who_walks_away_leaves_no_browser_and_no_secret_behind(
    stack: Stack, wiretap: WireTap
) -> None:
    env, gateway = stack.env, stack.gateway
    llm = ScriptedLlm()  # nothing is learned: no model call is expected
    start_worker(env, llm)
    anna = await Campus(env).enrol("anna@example.com")
    secrets = Secrets(env, anna)

    async with live_http(env.app) as http:
        student = Student(http, anna.headers())
        left = await student.attend(
            anna.connect_job, walk_away_at=lambda block: block.get("inputKind") == "code"
        )
        assert [a.label for a in left.asked] == ["input:email", "input:password (secret)"]
        assert left.waiting_on is not None
        assert gateway.active_for(anna.user_id) is not None  # the code page is open in Chromium

        stop = await http.post(f"/api/v1/jobs/{anna.connect_job}/cancel", headers=anna.headers())
        assert stop.status_code == 204

    await env.wait_for(anna.connect_job, {"cancelled"})
    for _ in range(200):  # the handler is unwound by the runner: the page is closed with it
        if gateway.active_for(anna.user_id) is None:
            break
        await asyncio.sleep(0.05)
    assert gateway.active_for(anna.user_id) is None

    connection = (await anna.client.get(f"/api/v1/connections/{anna.connection_id}")).json()
    assert (connection["status"], connection["hasPassword"]) == ("draft", False)
    for kind in SecretKind:  # nothing of the student's was kept: not the password, not a session
        assert not await env.services.vault.has(anna.user_id, anna.connection_id, kind)
    async with env.sm() as s:
        left_over = await s.scalar(
            select(func.count()).select_from(m.SecretRow).where(m.SecretRow.user_id == anna.user_id)
        )
    assert left_over == 0  # the answer to the secret question was taken and deleted
    await check_no_marker_anywhere(env, wiretap, secrets, llm)
    assert llm.requests == []


# =================================================================================================
# What each step looks like from the app
# =================================================================================================


async def check_connect_job(env: Env, connect: Attendance, connection_id: str) -> None:
    # the form of the university became native fields, in the order the university asked them
    assert [a.label for a in connect.asked] == [*CONNECT_QUESTIONS, "confirm"]
    assert "waiting_user" in connect.states()
    assert connect.names()[-1] == "done"
    result = connect.result
    assert (result["status"], result["revision"], result["kinds"]) == (
        "ok",
        1,
        ["deadlines", "scheduleEvents"],
    )
    assert result["sync"]["kinds"]["scheduleEvents"]["inserted"] == 4
    assert result["sync"]["kinds"]["deadlines"]["inserted"] == 2
    job = await env.job(connect.job_id)
    assert (job.state, job.connection_id) == ("done", connection_id)
    stages = [b["stage"] for b in connect.blocks("thinking")]
    assert "Смотрю страницы кабинета" in stages
    assert any(s.startswith("Разбираю:") for s in stages)
    # The student confirms once, with the summary in the same message as the question.
    assert [b["type"] for b in connect.blocks() if b["type"] in {"summary", "confirm"}] == [
        "confirm"
    ]
    (confirmation,) = [b for b in connect.blocks() if b["type"] == "confirm"]
    assert "4 занятия" in confirmation["details"]


async def check_the_app_shows_fakeuni(
    env: Env, client: httpx.AsyncClient, connection_id: str, space_id: str
) -> None:
    connection = (await client.get(f"/api/v1/connections/{connection_id}")).json()
    assert connection["status"] == "active"
    assert connection["adapter"] == "recipe"
    assert connection["hasPassword"] is True
    assert connection["errorCode"] is None
    assert when(connection["lastSyncAt"]) == env.clock.now()
    assert connection["nextSyncAt"] is not None

    schedule = await client.get("/api/v1/schedule", params=week())
    assert schedule.status_code == 200, schedule.text
    events = schedule.json()
    assert [(e["title"], e["location"], when(e["startsAt"])) for e in events] == CLASSES
    assert [e["kind"] for e in events] == ["lecture", "seminar", "lab", "seminar"]
    for e in events:  # provenance: where it comes from and how fresh it is
        p = e["provenance"]
        assert (p["connectionId"], p["spaceId"], p["stale"]) == (connection_id, space_id, False)
        assert p["sourceUrl"].startswith("https://fakeuni.test/api/v2/timetable")
        assert when(p["fetchedAt"]) == env.clock.now()

    today = (await client.get("/api/v1/today")).json()
    assert today["date"] == "2026-10-26"
    assert [e["title"] for e in today["events"]] == [c[0] for c in CLASSES[:2]]
    assert [d["title"] for d in today["deadlines"]] == DEADLINES
    assert today["attention"] == []

    deadlines = (await client.get("/api/v1/deadlines")).json()
    assert [(d["title"], d["state"]) for d in deadlines] == [(t, "open") for t in DEADLINES]
    assert [when(d["dueAt"]) for d in deadlines] == [
        datetime(2026, 10, 28, 20, 0, tzinfo=UTC),
        datetime(2026, 10, 31, 20, 0, tzinfo=UTC),
    ]
    for d in deadlines:
        p = d["provenance"]
        assert (p["connectionId"], p["spaceId"]) == (connection_id, space_id)
        assert p["sourceUrl"] == "https://fakeuni.test/api/v2/tasks/deadlines"

    sections = (await client.get(f"/api/v1/spaces/{space_id}/sections")).json()
    assert {s["kind"]: s["count"] for s in sections} == {"scheduleEvents": 4, "deadlines": 2}
    await check_registry(env)


async def check_registry(env: Env) -> None:
    """What the connection learned is kept for the next student of this university, once."""
    async with env.sm() as s:
        specs = (
            await s.execute(
                select(m.AdapterSpecRow.revision, m.AdapterSpecRow.state).where(
                    m.AdapterSpecRow.origin == data.ORIGIN
                )
            )
        ).all()
        presets = (
            await s.execute(
                select(m.LoginPresetRow.revision, m.LoginPresetRow.state).where(
                    m.LoginPresetRow.origin == data.ORIGIN
                )
            )
        ).all()
    assert [tuple(r) for r in specs] == [(1, "active")]
    assert [tuple(r) for r in presets] == [(1, "active")]


async def check_chat_answer(
    env: Env, client: httpx.AsyncClient, thread_id: str, chat: Attendance, llm: ScriptedLlm
) -> None:
    assert chat.asked == []
    messages = (await client.get(f"/api/v1/chat/threads/{thread_id}/messages")).json()
    assert [msg["role"] for msg in messages] == ["user", "agent"]
    (reply,) = messages[1]["blocks"]
    assert reply["type"] == "text"
    assert reply["markdown"] == (
        "Первая пара в понедельник — «Математический анализ» в 09:30, ауд. А-101 [1]."
    )
    (source,) = reply["sources"]  # a citation the model could not invent: the tool recorded it
    assert source["title"] == "Личный кабинет"  # a name, not the host
    assert source["url"] == "https://fakeuni.test/app"  # the page, never the API (FR-042)
    assert when(source["fetchedAt"]) == env.clock.now()
    assert chat.result["sources"][0]["title"] == "Личный кабинет"
    assert chat.result["tools"] == ["get_schedule"]

    # the model was given the synced timetable of Monday from the database, and nothing else
    tool_result = llm.requests[1].messages[-1]
    assert tool_result.role == "tool"
    assert "09:30–10:50 | Математический анализ" in tool_result.content
    assert "А-101" in tool_result.content
    assert "Английский язык" not in tool_result.content  # Friday is not Monday
    async with env.sm() as s:
        job = await s.get(m.Job, chat.job_id)
    assert job is not None
    assert (job.kind, job.state, job.thread_id) == ("chat", "done", thread_id)


async def check_nothing_leaked(
    env: Env,
    wiretap: WireTap,
    secrets: Secrets,
    *models: ScriptedLlm,
    console: pytest.CaptureFixture[str] | None = None,
) -> None:
    """Security check C1 for the whole run: the university's password and code exist only as
    ciphertext in `secrets`; nowhere else, and never in what a model was sent."""
    await check_no_marker_anywhere(env, wiretap, secrets, *models, console=console)
    needles = secrets.needles
    async with env.sm() as s:
        stored = (
            await s.execute(
                text("select kind, ciphertext, nonce, wrapped_dek from secrets where user_id = :u"),
                {"u": secrets.user_id},
            )
        ).all()
        answers = (await s.execute(select(m.JobAnswer))).scalars().all()
    # one of each, no transient left; a new session replaced the old one instead of piling up
    assert Counter(row.kind for row in stored) == {"session": 1, "login": 1, "password": 1}
    for row in stored:
        for blob in (row.ciphertext, row.nonce, row.wrapped_dek):
            assert isinstance(blob, bytes)
            for needle in needles.values():
                assert needle.encode() not in blob
    # the answers to secret questions are recorded as given, not as what was given
    assert all(a.values is None for a in answers if a.is_secret)
    assert sum(a.is_secret for a in answers) >= 2  # the password and the code of the first login


async def check_no_marker_anywhere(
    env: Env,
    wiretap: WireTap,
    secrets: Secrets,
    *models: ScriptedLlm,
    console: pytest.CaptureFixture[str] | None = None,
) -> None:
    needles = secrets.needles
    dump = await dump_database(env.sm)  # every table, every column: job_events, job_answers, ...
    assert data.USERNAME in dump  # the dump does hold the answers it is searched for secrets
    assert leaks(dump, needles) == []
    assert "worker" in wiretap.text  # the tap does hear the process it is listening to
    assert leaks(wiretap.text, needles) == []
    assert leaks(sent_to_models(*models), needles) == []
    if console is not None:  # what the process wrote to its stdout and stderr, from the start
        out = console.readouterr()
        assert leaks(out.out + out.err, needles) == []


def check_egress(stack: Stack) -> None:
    """Everything the browser and the sync client sent to the university went through the proxy
    process, and it let through exactly one host."""
    tunnels = [line.split("egress host=")[1].split() for line in stack.proxy_log()]
    allowed = {fields[0] for fields in tunnels if "decision=allow" in fields}
    refused = {fields[2] for fields in tunnels if fields[1] == "decision=deny"}
    assert allowed == {"fakeuni.test"}
    assert sum("decision=allow" in fields for fields in tunnels) > 10
    # Chromium offers no credentials until the proxy challenges it (407), then retries with them
    assert refused <= {"reason=auth_failed"}


async def check_jobs(env: Env, user_id: str) -> None:
    async with env.sm() as s:
        rows = (
            await s.execute(select(m.Job.kind, m.Job.state).where(m.Job.user_id == user_id))
        ).all()
    assert Counter(tuple(r) for r in rows) == {
        ("connect", "done"): 1,
        ("sync", "done"): 2,
        ("chat", "done"): 1,
    }


async def check_isolation(env: Env, connection_id: str, job_id: str, thread_id: str) -> None:
    """Another student sees nothing of the first one's university, jobs or chat (NFR-001)."""
    stranger = env.new_client()
    await env.account(stranger, email="eve@example.com")
    assert (await stranger.get("/api/v1/schedule", params=week())).json() == []
    today = (await stranger.get("/api/v1/today")).json()
    assert (today["events"], today["deadlines"]) == ([], [])
    assert (await stranger.get("/api/v1/deadlines")).json() == []
    for path in (
        f"/api/v1/connections/{connection_id}",
        f"/api/v1/jobs/{job_id}/events",
        f"/api/v1/chat/threads/{thread_id}/messages",
    ):
        assert (await stranger.get(path)).status_code == 404, path
    assert (await stranger.post(f"/api/v1/connections/{connection_id}/sync")).status_code == 404
