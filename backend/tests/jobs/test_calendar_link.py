"""A calendar by the export link of its owner (FR-018, Q11): the tool, the protected field, the
Vault, the check, the first sync and the summary, on real Postgres and the fakeuni calendar (L1).

The link is a secret: it must not reach the database outside `secrets`, a log line, a task event,
the history of the thread or a request to the model, whatever the student typed on the way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from botai.agent.chat import handle_chat
from botai.agent.env import ChatEnv
from botai.agent.tools_sources import (
    connect_calendar,
    connect_source,
    request_login,
    request_repair,
    sync_source,
)
from botai.contracts.agent_ui import TaskBlock
from botai.core import diagnostics
from botai.core.http import NO_RETRY
from botai.core.jobs import JobKind
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn.handlers import make_handlers
from botai.settings import Settings
from botai.sources.runner import make_sync_handler
from botai.spaces import service as spaces_service
from tests.agent.rig import call, final, step
from tests.e2e.support import WireTap, dump_database, leaks, sent_to_models
from tests.fakeuni import ical as fakeuni
from tests.jobs.env import Env
from tests.jobs.test_connect_source import count, jobs_of, no_ulid

pytestmark = pytest.mark.db
UNIVERSITY = "Фейкуни"
HOST = "calendar.fakeuni.test"
ORIGIN = f"https://{HOST}"
MARKERS = {
    "query key": fakeuni.SERVICE_TOKEN,
    "path key": fakeuni.PATH_TOKEN,
    "feed key": fakeuni.TOKEN,
}


class Directory(httpx.AsyncBaseTransport):
    """The network of the test: fakeuni answers for every host, `down.fakeuni.test` does not."""

    def __init__(self, app: object) -> None:
        self._fakeuni = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "down.fakeuni.test":
            raise httpx.ConnectError("no route to host", request=request)
        if request.url.host == "moved.fakeuni.test":  # a link that leads to another site
            return httpx.Response(
                302,
                headers={"location": "https://elsewhere.fakeuni.test/export.ics"},
                request=request,
            )
        return await self._fakeuni.handle_async_request(request)


@pytest.fixture(autouse=True)
def _fresh_calendar() -> Iterator[None]:
    fakeuni.reset()
    yield
    fakeuni.reset()


@pytest.fixture(autouse=True)
async def _shared_rows(env: Env) -> AsyncIterator[None]:
    """The spec of a calendar service and its audit trail are shared by the pool, not owned by a
    student: what a test taught the registry goes away with it."""
    async with env.sm() as session:
        specs = set(await session.scalars(select(m.AdapterSpecRow.id)))
        audit = (await session.scalar(select(func.coalesce(func.max(m.AuditLog.id), 0)))) or 0
    yield
    async with env.sm() as session:
        await session.execute(delete(m.AdapterSpecRow).where(m.AdapterSpecRow.id.not_in(specs)))
        await session.execute(delete(m.AuditLog).where(m.AuditLog.id > audit))
        await session.commit()


@pytest.fixture
def traces(tmp_path: Path) -> Iterator[Path]:
    """The operator's diagnostics switched on, writing to a directory of this test."""
    diagnostics.configure_diagnostics(
        Settings(botai_diagnostics=True, botai_diagnostics_dir=str(tmp_path))
    )
    try:
        yield tmp_path
    finally:
        diagnostics.configure_diagnostics(
            Settings(botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path))
        )


@pytest.fixture
def wiretap() -> Iterator[WireTap]:
    tap = WireTap()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(tap)
    root.setLevel(logging.DEBUG)
    try:
        yield tap
    finally:
        root.removeHandler(tap)
        root.setLevel(previous)


@dataclass(frozen=True, slots=True)
class Student:
    client: httpx.AsyncClient
    user_id: str
    space_id: str
    thread_id: str

    def chat(
        self, env: Env, *, space_id: str | None = None, thread_id: str | None = None
    ) -> ChatEnv:
        return ChatEnv(
            services=env.services,
            user_id=self.user_id,
            thread_id=thread_id or self.thread_id,
            job_id=None,
            space_id=space_id,
        )


async def student(env: Env, *, university: str = UNIVERSITY) -> Student:
    """A registered student of one university with an empty thread."""
    client = env.new_client()
    account = await env.account(client)
    space = await client.post("/api/v1/spaces", json={"title": university})
    assert space.status_code in (200, 201), space.text
    thread = await client.post("/api/v1/chat/threads", json={})
    return Student(client, account.id, space.json()["id"], thread.json()["id"])


def start_worker(env: Env, app: object, llm: ScriptedLlm | None = None) -> Directory:
    """The worker of production for this task: connect (with the calendar), sync, and the agent."""
    network = Directory(app)
    learn = make_handlers(transport=network, retry=NO_RETRY)
    env.services.llm = llm or ScriptedLlm()
    env.start(
        env.worker(
            {
                JobKind.CONNECT: learn[JobKind.CONNECT],
                JobKind.SYNC: make_sync_handler(transport=network, retry=NO_RETRY),
                JobKind.CHAT: handle_chat,
            }
        )
    )
    return network


async def field_of(who: Student, seen: set[str]) -> dict[str, Any]:
    """The protected field the task shows next (a block id not met before)."""
    async with asyncio.timeout(10):
        while True:
            runtime = (await who.client.get(f"/api/v1/chat/threads/{who.thread_id}/runtime")).json()
            for item in runtime["pending"]:
                if item["blockId"] not in seen:
                    assert (item["channel"], item["secret"]) == ("native", True)
                    seen.add(item["blockId"])
                    return item
            await asyncio.sleep(0.02)


async def hand_over(who: Student, link: str, seen: set[str]) -> str:
    """The student types the link into the protected field. Returns the id of the task."""
    field = await field_of(who, seen)
    response = await who.client.post(
        f"/api/v1/jobs/{field['jobId']}/answers", json={"blockId": field["blockId"], "values": link}
    )
    assert response.status_code == 204, response.text
    return str(field["jobId"])


async def history(who: Student) -> list[dict[str, Any]]:
    response = await who.client.get(f"/api/v1/chat/threads/{who.thread_id}/messages")
    assert response.status_code == 200
    return response.json()


def said(messages: list[dict[str, Any]]) -> str:
    """Everything a thread shows, as one text."""
    return "\n".join(json.dumps(message["blocks"], ensure_ascii=False) for message in messages)


async def calendar_of(env: Env, user: str) -> m.Connection:
    async with env.sm() as session:
        found = list(
            await session.scalars(
                select(m.Connection).where(
                    m.Connection.user_id == user, m.Connection.adapter == "ical"
                )
            )
        )
    assert len(found) == 1, found
    return found[0]


async def make_working(env: Env, who: Student, *, status: str = "active") -> m.Connection:
    """The calendar of the student after its link was handed over and read: a spec, an origin."""
    conn = await calendar_of(env, who.user_id)
    async with env.sm() as session:
        spec = await session.scalar(
            select(m.AdapterSpecRow).where(
                m.AdapterSpecRow.origin == ORIGIN, m.AdapterSpecRow.adapter == "ical"
            )
        )
        if spec is None:
            spec = m.AdapterSpecRow(
                origin=ORIGIN,
                adapter="ical",
                revision=1,
                state="active",
                spec={"adapter": "ical", "origin": ORIGIN},
            )
            session.add(spec)
            await session.flush()
        stored = await session.get(m.Connection, conn.id)
        assert stored is not None
        stored.origin, stored.allow_hosts, stored.status = ORIGIN, [HOST], status
        stored.spec_id, stored.spec_revision = spec.id, spec.revision
        await session.execute(
            m.Job.__table__.update()
            .where(m.Job.connection_id == conn.id)
            .values(state="done", finished_at=env.clock.now())
        )
        await session.commit()
    return await calendar_of(env, who.user_id)


def trace_records(directory: Path) -> str:
    return "\n".join(path.read_text() for path in directory.glob("*.jsonl*"))


async def no_leak(
    env: Env,
    wiretap: WireTap,
    traces: Path,
    *models: ScriptedLlm,
    stored: bool = True,
) -> None:
    """The link of the fixture is nowhere but in the Vault: not in a dump of the database (the
    `secrets` table holds ciphertext only), not in a log line, not in the operator diagnostics,
    not in a request to the model."""
    dump = await dump_database(env.sm)
    assert ("secrets:" in dump) is stored
    trace_text = trace_records(traces)
    assert "learn.calendar" in trace_text  # the switch was on, and the task wrote to it
    for text in (dump, wiretap.text, trace_text, sent_to_models(*models) if models else ""):
        assert leaks(text, MARKERS) == []
        assert "private_token" not in text
        assert "feed.ics?token" not in text


# --- the tool -------------------------------------------------------------------------------------


async def test_the_tool_makes_the_calendar_of_the_university_and_a_task_in_this_thread(
    env: Env,
) -> None:
    who = await student(env)
    chat = who.chat(env)
    told = await connect_calendar(chat, {"university_query": UNIVERSITY})

    assert told.startswith("Результат: запущено"), told
    assert "в этом чате" in told
    assert "в чат её присылать нельзя" in told
    no_ulid(told)
    (job,) = await jobs_of(env, who.user_id, "connect")
    assert (job.thread_id, job.payload, job.state) == (
        who.thread_id,
        {"calendar": True, "source": "chat"},
        "queued",
    )
    conn = await calendar_of(env, who.user_id)
    assert (conn.kind, conn.adapter, conn.title, conn.status) == (
        "calendar",
        "ical",
        "Календарь",
        "draft",
    )
    assert (conn.origin, conn.allow_hosts, conn.space_id) == ("", [], who.space_id)
    assert job.connection_id == conn.id
    (card,) = chat.blocks
    assert isinstance(card, TaskBlock)
    assert (card.kind, card.thread_id, card.connection_id, card.title) == (
        "connect",
        who.thread_id,
        conn.id,
        "Подключение: Календарь",
    )
    assert job.id not in told


async def test_without_a_university_the_tool_takes_the_one_of_the_chat_and_otherwise_asks(
    env: Env,
) -> None:
    who = await student(env)
    other = await who.client.post("/api/v1/spaces", json={"title": "Второй институт"})
    assert other.status_code in (200, 201)
    unclear = await connect_calendar(who.chat(env), {})
    assert unclear.startswith("Результат: нужно уточнение")
    assert await count(env, m.Connection, m.Connection.user_id == who.user_id) == 0
    assert await jobs_of(env, who.user_id) == []

    scoped = who.chat(env, space_id=other.json()["id"])  # the university of the thread decides
    assert (await connect_calendar(scoped, {})).startswith("Результат: запущено")
    assert (await calendar_of(env, who.user_id)).space_id == other.json()["id"]


async def test_a_university_named_in_words_is_made_like_for_any_source(env: Env) -> None:
    who = await student(env)
    told = await connect_calendar(who.chat(env), {"university_query": "Совсем новый институт"})
    assert told.startswith("Результат: запущено")
    conn = await calendar_of(env, who.user_id)
    assert conn.space_id != who.space_id
    async with env.sm() as session:
        space = await session.get(m.Space, conn.space_id)
    assert space is not None
    assert space.title == "Совсем новый институт"


async def test_a_calendar_is_one_per_university_and_a_second_ask_is_not_a_second_task(
    env: Env,
) -> None:
    who = await student(env)
    elsewhere = (await who.client.post("/api/v1/chat/threads", json={})).json()["id"]
    first = await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    assert first.startswith("Результат: запущено")
    again = who.chat(env)
    second = await connect_calendar(again, {"university_query": UNIVERSITY, "title": "Другой"})
    assert second.startswith("Результат: уже идёт, второй раз не запущено")
    assert "в этом чате" in second
    assert len(await jobs_of(env, who.user_id, "connect")) == 1
    (card,) = again.blocks
    assert isinstance(card, TaskBlock)

    # the same ask from another thread leads to the thread that owns the task
    pointer = await connect_calendar(
        who.chat(env, thread_id=elsewhere), {"university_query": UNIVERSITY}
    )
    assert f"botai://threads/{who.thread_id}" in pointer
    assert len(await jobs_of(env, who.user_id, "connect")) == 1
    assert await count(env, m.Connection, m.Connection.user_id == who.user_id) == 1


async def test_a_working_calendar_is_left_alone_until_a_new_link_is_asked_for(env: Env) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    conn = await make_working(env, who)

    exists = await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    assert exists.startswith("Результат: календарь уже подключён, ничего не запущено")
    assert "replace_link=true" in exists
    assert len(await jobs_of(env, who.user_id, "connect")) == 1

    replace = await connect_calendar(
        who.chat(env), {"university_query": UNIVERSITY, "replace_link": True}
    )
    assert replace.startswith("Результат: запущено")
    assert len(await jobs_of(env, who.user_id, "connect")) == 2
    again = await calendar_of(env, who.user_id)
    assert (again.id, again.status, again.origin) == (conn.id, "active", ORIGIN)  # not torn down


async def test_a_calendar_whose_link_stopped_working_asks_for_a_new_one_by_itself(env: Env) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    await make_working(env, who, status="needs_reauth")
    told = await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    assert told.startswith("Результат: запущено")
    assert len(await jobs_of(env, who.user_id, "connect")) == 2


async def test_a_calendar_the_student_switched_off_is_not_started_again(env: Env) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    await make_working(env, who, status="disabled")
    for replace in (False, True):
        told = await connect_calendar(
            who.chat(env), {"university_query": UNIVERSITY, "replace_link": replace}
        )
        assert told.startswith("Результат: календарь «Календарь»")
        assert "отключён студентом" in told
    assert len(await jobs_of(env, who.user_id, "connect")) == 1


async def test_users_do_not_share_calendars_or_see_each_others_tasks(env: Env) -> None:
    alice, bob = await student(env), await student(env)
    await connect_calendar(alice.chat(env), {"university_query": UNIVERSITY})
    (alices,) = await jobs_of(env, alice.user_id, "connect")
    mine = await connect_calendar(bob.chat(env), {"university_query": UNIVERSITY})
    assert mine.startswith("Результат: запущено")
    assert len(await jobs_of(env, bob.user_id, "connect")) == 1
    assert (await calendar_of(env, bob.user_id)).id != (await calendar_of(env, alice.user_id)).id
    # Bob cannot answer Alice's task, ask about her source or look at her thread
    stolen = await bob.client.post(
        f"/api/v1/jobs/{alices.id}/answers",
        json={"blockId": "0" * 26, "values": fakeuni.SERVICE_URL},
    )
    assert stolen.status_code == 404
    foreign = await request_login(bob.chat(env), {"connection_id": str(alices.connection_id)})
    assert foreign.startswith("Такого подключения у студента нет")
    runtime = await bob.client.get(f"/api/v1/chat/threads/{alice.thread_id}/runtime")
    assert runtime.status_code == 404


async def test_a_calendar_is_never_logged_into_or_repaired_through_a_browser(env: Env) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    conn = await calendar_of(env, who.user_id)
    before = len(await jobs_of(env, who.user_id))
    for tool, args in (
        (request_login, {"connection_id": conn.id}),
        (request_repair, {"connection_id": conn.id, "reason": "не обновляется"}),
    ):
        told = await tool(who.chat(env), args)
        assert "connect_calendar" in told
        assert "replace_link=true" in told
    asked = await sync_source(who.chat(env), {"connection_id": conn.id})
    assert "ссылка на календарь ещё не передана" in asked
    assert len(await jobs_of(env, who.user_id)) == before

    # the REST doors lead to the same place: a login or a repair of a calendar asks for the link
    async with env.sm() as session:
        job_id = await spaces_service.enqueue_for_connection(
            session,
            env.services.queue,
            who.user_id,
            conn.id,
            JobKind.REPAIR,
            thread_id=who.thread_id,
            payload={"manual_login": True, "reason": "x"},
        )
    job = await env.job(job_id)
    assert (job.kind, job.payload.get("calendar")) == ("connect", True)
    assert "manual_login" not in job.payload
    assert (
        len(await jobs_of(env, who.user_id, "connect")) == 1
    )  # the task of the tool, deduplicated


async def test_connect_source_leaves_the_calendar_of_the_university_alone(env: Env) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    told = await connect_source(
        who.chat(env),
        {
            "url": "https://lk.fakeuni.test/schedule",
            "title": "Личный кабинет",
            "university_query": UNIVERSITY,
        },
    )
    assert told.startswith("Результат: запущено")
    assert await count(env, m.Connection, m.Connection.user_id == who.user_id) == 2


# --- the task -------------------------------------------------------------------------------------


async def test_the_student_hands_the_link_over_and_the_calendar_is_read(
    env: Env, fakeuni_app: object, wiretap: WireTap, traces: Path
) -> None:
    """The whole story through the agent: «my timetable is in a calendar» -> the tool -> the
    protected field -> the Vault -> the check -> the first sync -> what is in it."""
    who = await student(env)
    llm = ScriptedLlm(
        queue=[
            step(call("connect_calendar", university_query=UNIVERSITY)),
            final("Открыл защищённое поле для ссылки: инструкция уже в чате."),
        ]
    )
    network = start_worker(env, fakeuni_app, llm)
    posted = await who.client.post(
        f"/api/v1/chat/threads/{who.thread_id}/messages",
        json={"text": "Моё расписание лежит в Яндекс.Календаре"},
    )
    assert posted.status_code == 202, posted.text

    seen: set[str] = set()
    task = await hand_over(who, fakeuni.SERVICE_WEBCAL, seen)
    done = await env.wait_for(task, {"done"})
    assert done.result == {"status": "ok", "events": 7, "deadlines": 2}

    # what the field asked, and what the thread kept of the answer
    blocks = [e.data for e in await env.events(task) if e.type == "block"]
    (field,) = [b for b in blocks if b["type"] == "input"]
    assert (field["inputKind"], field["secret"], field["required"]) == ("url", True, True)
    assert field["maxLength"] == 2000
    guide = next(b for b in blocks if b["type"] == "text")["markdown"]
    for service in ("Яндекс", "Google", "Outlook"):
        assert service in guide
    assert "не вставляйте в чат" in guide
    thread_text = said(await history(who))
    assert "Ссылка передана" in thread_text
    # what the calendar holds, in the words of every connection (learn/summary.py), then the closing
    assert "Нашла в календаре:" in thread_text
    assert "7 занятий с 28 октября по 30 ноября — 6 недель" in thread_text
    assert "2 задания со сроками" in thread_text
    assert "Ближайшие пары:" in thread_text
    assert "13:10–14:30 · Базы данных · В-310" in thread_text
    assert "Среда, 28 октября" in thread_text
    assert "Готово! Данные из «Фейкуни» подключены" in thread_text
    assert "Дальше обновляется само каждые 3 часа" in thread_text
    assert "Открыл защищённое поле" in thread_text  # the agent's own answer is there too

    # what became of the source
    conn = await calendar_of(env, who.user_id)
    assert (conn.status, conn.origin, conn.allow_hosts, conn.kind) == (
        "active",
        ORIGIN,
        [HOST],
        "calendar",
    )
    assert conn.last_sync_at == env.clock.now()
    assert conn.next_sync_at is not None
    assert conn.entry_url is None  # an address with a key is no page to open
    stored = await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL)
    assert stored is not None
    assert (
        stored.get_secret_value() == fakeuni.SERVICE_URL
    )  # webcal:// is the https:// it stands for
    async with env.sm() as session:
        specs = list(
            await session.scalars(select(m.AdapterSpecRow).where(m.AdapterSpecRow.origin == ORIGIN))
        )
        events = await session.scalar(
            select(func.count())
            .select_from(m.ScheduleEvent)
            .where(m.ScheduleEvent.connection_id == conn.id)
        )
    assert [(s.adapter, s.state, s.spec) for s in specs] == [
        ("ical", "active", {"adapter": "ical", "origin": ORIGIN})
    ]
    assert events == 7
    shown = (await who.client.get(f"/api/v1/connections/{conn.id}")).json()
    assert shown["storedSecrets"] == ["ical_url"]
    assert fakeuni.SERVICE_TOKEN not in json.dumps(shown)

    # the data reach the app, and the next refresh needs nobody
    schedule = await who.client.get(
        "/api/v1/schedule", params={"start": "2026-10-26T00:00:00Z", "end": "2026-12-01T00:00:00Z"}
    )
    assert {e["title"] for e in schedule.json()} == {
        "Базы данных",
        "Проектная защита",
        "Математический анализ",
        "День народного единства",
    }
    # recurrence rules, the skipped week (EXDATE), the moved class, one-off events and deadlines
    lectures = sorted(
        e["startsAt"] for e in schedule.json() if e["title"] == "Математический анализ"
    )
    assert [s[:16] for s in lectures] == [
        "2026-11-02T08:00",  # moved by the calendar (RECURRENCE-ID), not 06:30
        "2026-11-16T06:30",  # 9 November is skipped (EXDATE)
        "2026-11-23T06:30",
        "2026-11-30T06:30",
    ]
    assert "Отменённая консультация" not in {e["title"] for e in schedule.json()}
    deadlines = await who.client.get("/api/v1/deadlines", params={"include_done": "true"})
    assert {(d["title"], d["state"]) for d in deadlines.json()} == {
        ("ДЗ 4: графы", "open"),  # an event of zero length is a deadline
        ("Прочитать главу 5", "submitted"),  # a finished task of the calendar
    }
    sync = (await who.client.post(f"/api/v1/connections/{conn.id}/sync")).json()["jobId"]
    assert (await env.wait_for(sync, {"done"})).result["status"] == "ok"  # type: ignore[index]

    # the link went where it belongs: the host it names, the check and the refresh, no cookies
    asked = [r for r in network.requests if r.url.host == HOST]
    assert len(asked) == 2
    assert {r.headers.get("cookie") for r in asked} == {None}
    assert all(r.url.params["private_token"] == fakeuni.SERVICE_TOKEN for r in asked)
    await no_leak(env, wiretap, traces, llm)
    assert fakeuni.SERVICE_URL not in said(await history(who))


async def test_an_empty_calendar_is_connected_and_says_so(env: Env, fakeuni_app: object) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    start_worker(env, fakeuni_app)
    task = await hand_over(who, fakeuni.EMPTY_URL, set())
    done = await env.wait_for(task, {"done"})
    assert done.result == {"status": "ok", "events": 0, "deadlines": 0}
    text = said(await history(who))
    assert "данных в нём пока не нашлось" in text
    assert "Нашла в календаре" not in text
    assert (await calendar_of(env, who.user_id)).status == "active"


async def test_a_wrong_link_is_asked_again_with_the_reason_and_nothing_is_kept(
    env: Env, fakeuni_app: object, wiretap: WireTap, traces: Path
) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    start_worker(env, fakeuni_app)
    seen: set[str] = set()
    task = await hand_over(who, fakeuni.PAGE_URL, seen)  # the calendar as the browser shows it
    bad_key = fakeuni.SERVICE_URL.replace(fakeuni.SERVICE_TOKEN, "wrong-key-0000")
    for wrong in (bad_key, "это не ссылка"):
        assert await hand_over(who, wrong, seen) == task  # the same task asks again
        conn = await calendar_of(env, who.user_id)
        assert await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL) is None
    # the third wrong one ends the try
    ended = await env.wait_for(task, {"done"})
    assert ended.result == {"status": "error", "detail": "validation"}
    text = said(await history(who))
    assert "не календарь, а обычная страница" in text
    assert "не принял эту ссылку" in text
    assert "В поле должна быть одна ссылка на календарь" in text
    assert "Календарь пока не подключён" in text
    assert "Подключение не завершено" in text
    conn = await calendar_of(env, who.user_id)
    assert (conn.status, conn.error_code) == ("error", "validation")
    assert conn.last_sync_at is None
    assert await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL) is None
    await no_leak(env, wiretap, traces, stored=False)
    assert "wrong-key-0000" not in await dump_database(env.sm) + wiretap.text

    # asking again opens the field again, and a good link finishes what was left
    again = await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    assert again.startswith("Результат: запущено")
    second = await hand_over(who, fakeuni.PATH_URL, seen)
    assert (await env.wait_for(second, {"done"})).result["status"] == "ok"  # type: ignore[index]
    assert (await calendar_of(env, who.user_id)).status == "active"
    assert await count(env, m.Connection, m.Connection.user_id == who.user_id) == 1


async def test_a_calendar_that_does_not_answer_or_is_not_a_link_is_told_apart(
    env: Env, fakeuni_app: object
) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    start_worker(env, fakeuni_app)
    seen: set[str] = set()
    await hand_over(who, "https://down.fakeuni.test/export/ics.xml?private_token=abc123", seen)
    await hand_over(who, "ftp://calendar.fakeuni.test/week.ics", seen)
    fakeuni.revoke_link()
    task = await hand_over(who, fakeuni.URL, seen)
    await env.wait_for(task, {"done"})
    text = said(await history(who))
    assert "Сайт календаря не отвечает" in text
    assert "Нужна ссылка на экспорт календаря: https:// или webcal://" in text
    assert "возможно, она скопирована не целиком или уже отозвана" in text


async def test_a_link_that_leads_to_another_site_is_refused_and_that_site_is_never_asked(
    env: Env, fakeuni_app: object
) -> None:
    who = await student(env)
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    network = start_worker(env, fakeuni_app)
    seen: set[str] = set()
    await hand_over(who, "https://moved.fakeuni.test/export/ics.xml?private_token=abc123", seen)
    task = await hand_over(who, fakeuni.SERVICE_URL, seen)  # the second try is a good link
    await env.wait_for(task, {"done"})
    assert "Ссылка ведёт на другой адрес, чем календарь" in said(await history(who))
    assert (await calendar_of(env, who.user_id)).status == "active"
    hosts = {request.url.host for request in network.requests}
    assert hosts == {"moved.fakeuni.test", HOST}  # elsewhere.fakeuni.test was never reached


async def test_the_link_can_be_replaced_and_a_refused_one_keeps_the_old_calendar(
    env: Env, fakeuni_app: object, wiretap: WireTap, traces: Path
) -> None:
    who = await student(env)
    start_worker(env, fakeuni_app)
    seen: set[str] = set()
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    first = await hand_over(who, fakeuni.SERVICE_URL, seen)
    await env.wait_for(first, {"done"})
    conn = await calendar_of(env, who.user_id)
    assert conn.status == "active"

    # the service regenerated the link: the old one is dead, the data stay, the student is asked
    fakeuni.revoke_link()
    stale = (await who.client.post(f"/api/v1/connections/{conn.id}/sync")).json()["jobId"]
    await env.wait_for(stale, {"done"})
    dead = await calendar_of(env, who.user_id)
    assert (dead.status, dead.error_code) == ("needs_reauth", "source_auth_required")
    async with env.sm() as session:
        kept = await session.scalar(
            select(func.count())
            .select_from(m.ScheduleEvent)
            .where(m.ScheduleEvent.connection_id == conn.id)
        )
    assert kept == 7

    fakeuni.reset()
    told = await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    assert told.startswith("Результат: запущено")
    second = await hand_over(who, fakeuni.PATH_URL, seen)
    await env.wait_for(second, {"done"})
    renewed = await calendar_of(env, who.user_id)
    assert (renewed.id, renewed.status, renewed.error_code) == (conn.id, "active", None)
    stored = await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL)
    assert stored is not None
    assert stored.get_secret_value() == fakeuni.PATH_URL  # the new link replaced the old one
    await no_leak(env, wiretap, traces)

    # a replacement that fails changes nothing about the calendar that works
    told = await connect_calendar(
        who.chat(env), {"university_query": UNIVERSITY, "replace_link": True}
    )
    assert told.startswith("Результат: запущено")
    third = ""
    for _ in range(3):
        third = await hand_over(who, fakeuni.PAGE_URL, seen)
    await env.wait_for(third, {"done"})
    after = await calendar_of(env, who.user_id)
    assert (after.status, after.error_code) == ("active", None)
    stored = await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL)
    assert stored is not None
    assert stored.get_secret_value() == fakeuni.PATH_URL


async def test_a_task_waiting_for_a_link_does_not_hold_the_source_slot(env: Env) -> None:
    """It drives no browser: it neither waits for another source task nor makes others wait."""
    who = await student(env)
    queue = env.services.queue
    _, other = await env.connection(who.user_id)
    worker = env.worker({})
    sync = await queue.enqueue(JobKind.SYNC, user_id=who.user_id, connection_id=other, dedupe=False)
    claimed = await worker.claim()
    assert claimed is not None
    assert claimed.id == sync
    await connect_calendar(who.chat(env), {"university_query": UNIVERSITY})
    (calendar_job,) = await jobs_of(env, who.user_id, "connect")
    # the sync holds the slot, yet the calendar task starts at once
    second = await worker.claim()
    assert second is not None
    assert second.id == calendar_job.id
    await worker._finish(claimed, "done")

    # and while the calendar task waits for the student, a refresh of another source starts
    async with env.sm() as session:
        row = await session.get(m.Job, calendar_job.id)
        assert row is not None
        row.state = "waiting_user"
        await session.commit()
    later = await queue.enqueue(
        JobKind.SYNC, user_id=who.user_id, connection_id=other, dedupe=False
    )
    third = await worker.claim()
    assert third is not None
    assert third.id == later
