"""L2: the real DeepSeek behind the offer of a calendar.

A student pastes a personal calendar link, the thread answers with the offer block and its buttons
(`sources/calendar.py`), and the student taps «Да». The agent must start the calendar task
(`connect_calendar`), never log in or connect a site for it (`connect_source`, `request_login`,
`request_repair`), never ask the student again, and never ask for the link in the chat: the link
goes into the protected field of the task.

Opt-in and paid (a cent or two per turn): `BOTAI_ENV_FILE=... BOTAI_LIVE_LLM=1 pytest -m live_llm -s`.
Everything but the model is the real code on the test database: the offer is written by the same
function the API calls, the tool is the one the agent has. The key is read by pydantic-settings only.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.bench import NullGate
from botai.agent.chat import handle_chat
from botai.agent.llm import DeepSeekProvider, MeteredLlm
from botai.contracts.agent_ui import TextBlock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import LlmRequest, LlmResponse, LlmToolCall
from botai.core.queue import InMemoryJobQueue
from botai.db.models import ChatMessage, ChatThread, Connection, Job
from botai.settings import Settings
from botai.sources import calendar
from tests.agent.seed import FakeReader, add_connection, add_space
from tests.agent.support import NOW, add_message, add_thread, make_services

Sm = async_sessionmaker[AsyncSession]

# what the student must never be sent into for a calendar, and what must not be asked again
NOT_FOR_A_CALENDAR = {"connect_source", "request_login", "request_repair", "sync_source"}
NO_SECOND_QUESTION = {"offer_choices"}
ASKS_FOR_A_SEND = re.compile(r"пришл|отправ|вставь|вставит|вставл|скин|присл|пересл|напиш")
ALLOWED_WORDS = re.compile(r"защищ|поле|\bне\b|нельзя|никогда")


class Watched:
    """Remembers every tool call the model asked for; the model is the metered real one."""

    def __init__(self, inner: MeteredLlm) -> None:
        self.inner = inner
        self.tool_calls: list[LlmToolCall] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = await self.inner.complete(request)
        self.tool_calls.extend(response.tool_calls)
        return response

    @property
    def names(self) -> list[str]:
        return [call.name for call in self.tool_calls]


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


async def university(sm: Sm, user_id: str, title: str) -> str:
    """A university of the student with the site of its portal already connected, like a student
    who has used the app for a while."""
    space = await add_space(sm, user_id, title)
    await add_connection(sm, user_id, space, "Личный кабинет")
    return space


async def offer_and_tap(
    sm: Sm,
    user_id: str,
    thread_id: str,
    pick: str | None,
) -> str:
    """The server writes the offer (as it does for a refused link), the student taps a button: the
    one named `pick`, else the first. Returns the id of the student's message."""
    async with sm() as session:
        thread = await session.get(ChatThread, thread_id)
        assert thread is not None
        await calendar.offer_calendar(session, user_id, thread, NOW - timedelta(minutes=2))
    async with sm() as session:
        offered = await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id, ChatMessage.role == "agent")
            .order_by(ChatMessage.created_at)
        )
        (message,) = offered
    block = TextBlock.model_validate(message.blocks[0])
    buttons = block.quick_replies or []
    assert buttons, "the offer has buttons"
    tap = next((b for b in buttons if pick is None or pick in b), None)
    assert tap is not None, (pick, buttons)
    return await add_message(sm, user_id, thread_id, "user", tap, at=NOW - timedelta(minutes=1))


async def run_turn(
    sm: Sm,
    user_id: str,
    thread_id: str,
    message_id: str,
    live: Watched,
    settings: Settings,
) -> tuple[InMemoryJobContext, str]:
    services = make_services(live, sm=sm, queue=InMemoryJobQueue(), settings=settings)
    ctx = InMemoryJobContext(user_id=user_id, job_id=new_id())
    await handle_chat(
        ctx, services, {"thread_id": thread_id, "message_id": message_id}, reader=FakeReader()
    )
    reply = "\n".join(b.markdown for b in ctx.blocks if isinstance(b, TextBlock))
    return ctx, reply


def asks_for_the_link_in_chat(reply: str) -> bool:
    """A sentence that asks the student to send the link, without sending it to the protected field
    and without forbidding it («в сообщение её отправлять не нужно» is the right thing to say)."""
    if re.search(r"https?://|webcal", reply, re.IGNORECASE):
        return True
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply):
        low = sentence.lower()
        if "ссылк" in low and ASKS_FOR_A_SEND.search(low) and not ALLOWED_WORDS.search(low):
            return True
    return False


async def calendars(sm: Sm, user_id: str) -> list[Connection]:
    async with sm() as session:
        rows = await session.scalars(
            select(Connection).where(Connection.user_id == user_id, Connection.kind == "calendar")
        )
        return list(rows)


async def connect_jobs(sm: Sm, user_id: str) -> list[Job]:
    async with sm() as session:
        rows = await session.scalars(
            select(Job).where(Job.user_id == user_id, Job.kind == "connect")
        )
        return list(rows)


def report(name: str, live: Watched, reply: str) -> None:
    meter = live.inner
    print(
        f"[L2] {name}: tools={live.names} spent=${meter.spent_usd:.5f} calls={len(meter.calls)}"
        f" reply={reply!r}"
    )


def check_the_turn(live: Watched, reply: str) -> None:
    names = set(live.names)
    assert "connect_calendar" in names, (live.names, reply)
    assert names.isdisjoint(NOT_FOR_A_CALENDAR), live.names
    assert names.isdisjoint(NO_SECOND_QUESTION), live.names
    assert not asks_for_the_link_in_chat(reply), reply
    assert reply.strip(), "the student is told what happens next"
    for call in live.tool_calls:
        if call.name == "connect_calendar":
            assert call.arguments.get("replace_link") is not True, call.arguments


@pytest.mark.live_llm
async def test_the_student_taps_yes_and_the_agent_starts_the_calendar(
    sm: Sm, user_id: str, live: Watched, settings: Settings
) -> None:
    space = await university(sm, user_id, "Университет Заря")
    thread = await add_thread(sm, user_id, space_id=space)
    tapped = await offer_and_tap(sm, user_id, thread, calendar.YES)

    _, reply = await run_turn(sm, user_id, thread, tapped, live, settings)
    report("one university", live, reply)

    check_the_turn(live, reply)
    (made,) = await calendars(sm, user_id)
    assert made.space_id == space
    (job,) = await connect_jobs(sm, user_id)
    assert (job.connection_id, job.thread_id) == (made.id, thread)
    assert job.payload == {"calendar": True, "source": "chat"}


@pytest.mark.live_llm
async def test_the_student_taps_the_button_of_one_of_two_universities(
    sm: Sm, user_id: str, live: Watched, settings: Settings
) -> None:
    first = await university(sm, user_id, "Университет Заря")
    second = await university(sm, user_id, "Институт Север")
    thread = await add_thread(sm, user_id)  # no hint: the offer asks which one
    tapped = await offer_and_tap(sm, user_id, thread, "Север")

    _, reply = await run_turn(sm, user_id, thread, tapped, live, settings)
    report("two universities", live, reply)

    check_the_turn(live, reply)
    (made,) = await calendars(sm, user_id)
    assert made.space_id == second != first, "the calendar is of the university the button names"
    (job,) = await connect_jobs(sm, user_id)
    assert (job.connection_id, job.thread_id) == (made.id, thread)


@pytest.mark.parametrize(
    ("reply", "asks"),
    [
        ("Вставляйте ссылку только туда — в сообщение её отправлять не нужно.", False),
        ("Ссылку вставьте в защищённое поле, которое откроется в чате.", False),
        ("Ссылку берут в настройках календаря: раздел «Экспорт».", False),
        ("Пришлите мне ссылку на календарь.", True),
        ("Отправьте ссылку сюда, пожалуйста.", True),
        ("Вставьте ссылку: https://calendar.test/export.ics", True),
    ],
)
def test_the_check_of_the_reply_tells_a_request_for_the_link_from_a_ban(
    reply: str, asks: bool
) -> None:
    """Not live: the checker the paid tests lean on must not pass everything or flag a ban."""
    assert asks_for_the_link_in_chat(reply) is asks
