"""The whole backend in one process against the synthetic university (W7).

Every stream tested its part with doubles of the others. Here nothing is a double except the
university and the language model: the API, the job runner on a committed database, the handler
table of the worker, the real Chromium logging in through the real EgressProxy over HTTPS, the
Explorer authoring a spec from a ScriptedLlm, the sync Runner and its scheduler, the domain API and
the chat.

The test plays the student exactly as the PWA does: it follows the SSE stream of a job and
answers its questions over `POST /api/v1/jobs/{id}/answers`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from botai.browser.gateway import PlaywrightGateway
from botai.browser.proxy import EgressProxy
from botai.browser.session import decode_session
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.jobs.runner import schedule_syncs
from botai.worker import handler_table
from tests.agent.rig import call, final, step
from tests.auth.support import PASSWORD, make_user
from tests.e2e.support import (
    Attendance,
    Student,
    WireTap,
    dump_database,
    leaks,
    sent_to_models,
)
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import data
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, live_http, session_header
from tests.learn import support as ls

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


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def week() -> dict[str, str]:
    return {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


class Secrets:
    """What must never leave the Vault, collected as the run produces it: the university's
    password and code, and every session cookie the university ever issued."""

    def __init__(self, env: Env, user_id: str, connection_id: str) -> None:
        self._env, self.user_id, self._connection = env, user_id, connection_id
        self.needles = {"password": data.PASSWORD, "code": data.OTP}

    async def session(self, label: str) -> str:
        """The cookie of the session in the Vault now; it joins the things that must not leak."""
        raw = await self._env.services.vault.get(self.user_id, self._connection, SecretKind.SESSION)
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


async def test_a_student_connects_a_university_and_asks_about_it(
    stack: Env,
    gateway: PlaywrightGateway,
    egress: EgressProxy,
    wiretap: WireTap,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = stack
    env.clock.advance(MONDAY_MORNING - env.clock.now())
    learning_llm = ScriptedLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    env.services.llm = learning_llm
    env.start(env.worker(handler_table()))  # the real handlers: connect, sync, chat, ...

    # === 1. the API: the owner invites a friend, the friend registers and adds a university =====
    owner_id = await make_user(env.sm, "owner@example.com", is_owner=True, password=PASSWORD)
    env.users.append(owner_id)
    owner = env.new_client()
    login = await owner.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200
    invite = (await owner.post("/api/v1/auth/invites")).json()["code"]

    friend = env.new_client()
    registered = await friend.post(
        "/api/v1/auth/register",
        json={
            "inviteCode": invite,
            "email": "anna@example.com",
            "displayName": "Анна",
            "password": PASSWORD,
        },
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["user"]["id"]
    env.users.append(user_id)

    space = (await friend.post("/api/v1/spaces", json={"title": "Фейкуни"})).json()
    started = await friend.post(
        f"/api/v1/spaces/{space['id']}/connections",
        json={"kind": "portal", "url": "https://fakeuni.test/"},
    )
    assert started.status_code == 201, started.text
    connection_id = started.json()["connection"]["id"]
    connect_job = started.json()["jobId"]
    assert started.json()["connection"]["status"] == "draft"
    queued = await env.job(connect_job)
    assert (queued.kind, queued.connection_id) == ("connect", connection_id)
    secrets = Secrets(env, user_id, connection_id)

    async with live_http(env.app) as http:
        student = Student(http, session_header(friend))

        statuses: list[str] = []

        async def connection_status(_block: dict[str, Any]) -> None:
            status = (await friend.get(f"/api/v1/connections/{connection_id}")).json()["status"]
            statuses.append(status)

        # === 2-4. connect: Chromium logs in through the proxy, the Explorer learns, sync runs ===
        connect = await student.attend(connect_job, on_question=connection_status)
        await check_connect_job(env, connect, connection_id)
        assert statuses == ["learning"] * 4  # every question came while the connection was set up
        assert len(learning_llm.requests) == 2  # one authoring call per kind of data
        assert gateway.active_for(user_id) is None  # no browser context outlives its job
        first_session = await secrets.session("first")

        # === 5. what the student sees in the app ================================================
        await check_the_app_shows_fakeuni(env, friend, connection_id, space["id"])
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
        assert gateway.active_for(user_id) is None

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
        thread = (await friend.post("/api/v1/chat/threads", json={"spaceId": space["id"]})).json()
        posted = await friend.post(
            f"/api/v1/chat/threads/{thread['id']}/messages", json={"text": QUESTION}
        )
        assert posted.status_code == 202, posted.text
        chat = await student.attend(posted.json()["jobId"])
        await check_chat_answer(env, friend, thread["id"], chat, chat_llm)

    # === the whole run ============================================================================
    await check_nothing_leaked(env, wiretap, secrets, learning_llm, chat_llm, console=capfd)
    check_egress(egress, wiretap)
    await check_jobs(env, user_id)
    await check_registry(env)
    await check_isolation(env, connection_id, connect_job, thread["id"])


async def check_connect_job(env: Env, connect: Attendance, connection_id: str) -> None:
    # the form of the university became native fields, in the order the university asked them
    assert [a.label for a in connect.asked] == [
        "input:email",
        "input:password (secret)",
        "input:code (secret)",
        "confirm",
    ]
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
    assert [b["type"] for b in connect.blocks() if b["type"] in {"summary", "confirm"}] == [
        "summary",
        "confirm",
    ]


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
    assert source["title"] == "fakeuni.test"
    assert source["url"].startswith("https://fakeuni.test/api/v2/timetable")
    assert when(source["fetchedAt"]) == env.clock.now()
    assert chat.result["sources"][0]["title"] == "fakeuni.test"
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
    needles = secrets.needles
    dump = await dump_database(env.sm)  # every table, every column: job_events, job_answers, ...
    assert data.USERNAME in dump  # the dump does hold the answers it is searched for secrets
    assert leaks(dump, needles) == []
    assert "egress host=" in wiretap.text  # the tap does hear the process it is listening to
    assert leaks(wiretap.text, needles) == []
    assert leaks(sent_to_models(*models), needles) == []
    if console is not None:  # what the process wrote to its stdout and stderr, from the start
        out = console.readouterr()
        assert leaks(out.out + out.err, needles) == []

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


def check_egress(egress: EgressProxy, wiretap: WireTap) -> None:
    """Everything the browser and the sync client sent to the university went through the proxy,
    and the proxy let through exactly one host."""
    hosts = {
        line.split("host=")[1].split()[0]
        for line in wiretap.lines
        if "egress host=" in line and "decision=allow" in line
    }
    assert hosts == {"fakeuni.test"}
    assert egress.stats["allow:ok"] > 10
    # Chromium offers no credentials until the proxy challenges it (407), then retries with them
    assert {k for k in egress.stats if k.startswith("deny")} <= {"deny:auth_failed"}


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
