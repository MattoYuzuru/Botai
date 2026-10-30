"""L2: the real DeepSeek asked again to connect a source (kinds, resume, extension).

Two things a student says to a source that is already there, and what the agent must do:

- «Переподключи ЦУ: прочитай расписание и задания заново» to a source whose connection never
  finished: `connect_source` with `kinds` holding the schedule and the tasks (`deadlines`, or
  `assignments`), which resumes that very source with that ask.
- «добавь ещё задания» to a source that works and reads the schedule only: `connect_source` with the
  tasks in `kinds`, which starts a task that learns the tasks alone and keeps the recipe.

Opt-in and paid (cents per turn): `BOTAI_ENV_FILE=... BOTAI_LIVE_LLM=1 pytest -m live_llm -s`.
Everything but the model is the real code on the test database. The key is read by
pydantic-settings only and never printed.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.bench import NullGate
from botai.agent.chat import handle_chat
from botai.agent.llm import DeepSeekProvider, MeteredLlm
from botai.contracts.agent_ui import TextBlock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import LlmToolCall
from botai.core.queue import InMemoryJobQueue
from botai.db.models import AdapterSpecRow, Connection, Job, Space, University
from botai.settings import Settings
from tests.agent.seed import FakeReader
from tests.agent.support import NOW, add_message, add_thread, make_services
from tests.agent.test_live_calendar import Watched
from tests.sources import recipes

Sm = async_sessionmaker[AsyncSession]
SITE = "https://my.cu-fake.test"
TASKS = {"deadlines", "assignments"}


@pytest.fixture
def settings() -> Settings:
    found = Settings(llm_provider="deepseek")  # reads BOTAI_ENV_FILE
    if not found.deepseek_api_key.get_secret_value():
        pytest.skip("DEEPSEEK_API_KEY is not configured (BOTAI_ENV_FILE)")
    return found


@pytest.fixture
def live(settings: Settings) -> Watched:
    cap = float(os.environ.get("BOTAI_LIVE_LLM_BUDGET_USD", "0.50"))
    return Watched(MeteredLlm(DeepSeekProvider(settings, gate=NullGate()), cap_usd=cap))


async def source_of(sm: Sm, user_id: str, *, working: bool) -> tuple[Connection, str]:
    """The Central University of a student with one source: its connection never finished (a first
    task cancelled half-way), or it works and reads the schedule only."""
    async with sm() as session:
        university = University(
            id="cu-fixture",
            title="Центральный университет",
            short_title="ЦУ",
            origins=[],
            accent_hue=250.0,
        )
        session.add(university)
        await session.flush()
        space = Space(
            id=new_id(),
            user_id=user_id,
            university_id=university.id,
            title="Центральный университет",
            time_zone="Europe/Moscow",
            profile={},
        )
        session.add(space)
        await session.flush()
        conn = Connection(
            id=new_id(),
            user_id=user_id,
            space_id=space.id,
            origin=SITE,
            kind="lms",
            title="Личный кабинет ЦУ",
            status="active" if working else "draft",
            entry_url=f"{SITE}/learn/timetable",
            allow_hosts=["my.cu-fake.test"],
            last_sync_at=NOW if working else None,
        )
        session.add(conn)
        await session.flush()
        if working:
            spec = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
            spec["recipe"]["origin"] = SITE
            row = AdapterSpecRow(
                origin=SITE, adapter="recipe", revision=1, state="active", spec=spec
            )
            session.add(row)
            await session.flush()
            conn.adapter, conn.spec_id, conn.spec_revision = "recipe", row.id, 1
            conn.data_scope = ["scheduleEvents"]
        old = Job(
            id=new_id(),
            user_id=user_id,
            kind="connect",
            state="done" if working else "cancelled",
            connection_id=conn.id,
            payload={
                "source": "chat",
                "url": f"{SITE}/learn/timetable",
                "learning_intent": {
                    "entry_urls": [f"{SITE}/learn/timetable"],
                    "requested_kinds": ["scheduleEvents"],
                    "absent_kinds": [],
                    "user_note": "Подключение по ссылке на расписание",
                    "discovery_mode": "auto",
                },
            },
            run_after=NOW,
        )
        session.add(old)
        await session.commit()
        return conn, space.id


async def ask(sm: Sm, user_id: str, text: str, live: Watched, settings: Settings) -> str:
    thread = await add_thread(sm, user_id)
    message = await add_message(sm, user_id, thread, "user", text)
    services = make_services(live, sm=sm, queue=InMemoryJobQueue(), settings=settings)
    ctx = InMemoryJobContext(user_id=user_id, job_id=new_id())
    await handle_chat(
        ctx, services, {"thread_id": thread, "message_id": message}, reader=FakeReader()
    )
    return "\n".join(b.markdown for b in ctx.blocks if isinstance(b, TextBlock))


def connect_calls(live: Watched) -> list[LlmToolCall]:
    return [call for call in live.tool_calls if call.name == "connect_source"]


def kinds_of(call: LlmToolCall) -> set[str]:
    raw: Any = call.arguments.get("kinds")
    return set(raw) if isinstance(raw, list) else set()


async def connect_jobs(sm: Sm, user_id: str) -> list[Job]:
    async with sm() as session:
        rows = await session.scalars(
            select(Job)
            .where(Job.user_id == user_id, Job.kind == "connect")
            .order_by(Job.created_at)
        )
        return list(rows)


def report(name: str, live: Watched, reply: str) -> None:
    meter = live.inner
    calls = [
        (c.name, c.arguments.get("kinds")) for c in live.tool_calls if c.name == "connect_source"
    ]
    print(
        f"[L2] {name}: tools={live.names} connect_source={calls} "
        f"spent=${meter.spent_usd:.5f} calls={len(meter.calls)} reply={reply!r}"
    )


@pytest.mark.live_llm
@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_reconnect_with_schedule_and_tasks_resumes_the_source_with_both_kinds(
    sm: Sm, user_id: str, live: Watched, settings: Settings, attempt: int
) -> None:
    conn, _ = await source_of(sm, user_id, working=False)

    reply = await ask(
        sm, user_id, "Переподключи ЦУ: прочитай расписание и задания заново", live, settings
    )
    report(f"reconnect #{attempt}", live, reply)

    calls = connect_calls(live)
    assert calls, (live.names, reply)
    assert any("scheduleEvents" in kinds_of(c) and kinds_of(c) & TASKS for c in calls), [
        c.arguments for c in calls
    ]
    jobs = await connect_jobs(sm, user_id)
    assert len(jobs) == 2, "the unfinished source is resumed by a new task"
    resumed = jobs[-1]
    assert resumed.connection_id == conn.id
    intent = resumed.payload["learning_intent"]
    assert "scheduleEvents" in intent["requested_kinds"]
    assert set(intent["requested_kinds"]) & TASKS
    assert reply.strip()


@pytest.mark.live_llm
@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_adding_tasks_to_a_schedule_only_source_starts_the_extension(
    sm: Sm, user_id: str, live: Watched, settings: Settings, attempt: int
) -> None:
    conn, _ = await source_of(sm, user_id, working=True)

    reply = await ask(sm, user_id, "добавь ещё задания", live, settings)
    report(f"extend #{attempt}", live, reply)

    calls = connect_calls(live)
    assert calls, (live.names, reply)
    assert any(kinds_of(c) & TASKS for c in calls), [c.arguments for c in calls]
    jobs = await connect_jobs(sm, user_id)
    extensions = [j for j in jobs if j.payload.get("extend") is True]
    assert len(extensions) == 1, [j.payload for j in jobs]
    assert extensions[0].connection_id == conn.id
    assert set(extensions[0].payload["learning_intent"]["requested_kinds"]) <= TASKS
    async with sm() as session:  # the source that works is not touched by the ask
        stored = await session.get(Connection, conn.id)
        assert stored is not None
        assert (stored.status, stored.spec_revision) == ("active", 1)
        assert stored.data_scope == ["scheduleEvents"]
    assert reply.strip()
