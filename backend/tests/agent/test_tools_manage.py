"""Tools that manage several sources of one university: the primary one, switching one off, comparing
two (FR-054, L1: real Postgres, the real tools; the model is not involved).

Synthetic `fakeuni` data: a cabinet that knows rooms and a calendar that knows teachers and links.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.reader import RepoReader
from botai.agent.tools import MUTATING, build_chat_tools
from botai.agent.turn_context import turn_context
from botai.core.ids import new_id
from botai.db.models import ChatMessage, ChatThread, Connection, ScheduleEvent, Space
from tests.agent.conftest import add_user
from tests.agent.seed import add_connection, add_space
from tests.agent.support import NOW, add_message
from tests.agent.test_tools import Rig, rig

Sm = async_sessionmaker[AsyncSession]
DAY = datetime(2026, 10, 28, tzinfo=UTC)  # the fakeuni week of NOW; 28.10 is a Wednesday


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


async def add_event(
    sm: Sm,
    user_id: str,
    connection_id: str,
    title: str,
    start: datetime,
    end: datetime | None = None,
    **kw: Any,
) -> str:
    event_id = new_id()
    async with sm() as session:
        session.add(
            ScheduleEvent(
                id=event_id,
                user_id=user_id,
                connection_id=connection_id,
                external_id=event_id,
                source_url="https://fakeuni.test/schedule",
                fetched_at=NOW,
                seen_at=NOW,
                title=title,
                starts_at=start,
                ends_at=end or start + timedelta(minutes=80),
                **kw,
            )
        )
        await session.commit()
    return event_id


async def age(sm: Sm, connection_id: str, days: int) -> None:
    async with sm() as session:
        await session.execute(
            update(Connection)
            .where(Connection.id == connection_id)
            .values(created_at=NOW - timedelta(days=days))
        )
        await session.commit()


class University:
    def __init__(self, space: str, cabinet: str, calendar: str) -> None:
        self.space, self.cabinet, self.calendar = space, cabinet, calendar


async def university(sm: Sm, user_id: str) -> University:
    """One university whose cabinet is older than its calendar, both active."""
    space = await add_space(sm, user_id, "Вуз А")
    cabinet = await add_connection(sm, user_id, space, "Кабинет", kind="portal")
    calendar = await add_connection(sm, user_id, space, "Календарь", kind="calendar")
    await age(sm, cabinet, 5)
    await age(sm, calendar, 1)
    return University(space, cabinet, calendar)


async def with_classes(sm: Sm, user_id: str, uni: University) -> None:
    """Wednesday 28.10: two classes in both sources (one with another room), one only in each."""
    await add_event(sm, user_id, uni.cabinet, "Алгоритмы", at(28, 9), location_text="305")
    await add_event(sm, user_id, uni.cabinet, "Физика", at(28, 11), location_text="101")
    await add_event(sm, user_id, uni.cabinet, "Право", at(28, 13))
    await add_event(
        sm,
        user_id,
        uni.calendar,
        "Алгоритмы",
        at(28, 9),
        location_text="405",
        teacher_text="Иванов",
    )
    await add_event(sm, user_id, uni.calendar, "Физика", at(28, 11), location_text="101")
    await add_event(sm, user_id, uni.calendar, "Йога", at(28, 18))


async def chat(sm: Sm, user_id: str, space: str) -> Rig:
    r = await rig(sm, user_id, space_id=space, reader=RepoReader())  # type: ignore[arg-type]
    return r


async def agent_asks(
    sm: Sm, user_id: str, thread_id: str, text: str, buttons: list[str], at_: datetime
) -> None:
    async with sm() as session:
        session.add(
            ChatMessage(
                id=new_id(),
                user_id=user_id,
                thread_id=thread_id,
                role="agent",
                blocks=[{"type": "text", "markdown": text, "quickReplies": buttons}],
                created_at=at_,
            )
        )
        await session.commit()


async def student_says(r: Rig, sm: Sm, text: str) -> None:
    """The student's message of this turn: what the agent reads as the question."""
    await add_message(
        sm, r.env.user_id, r.env.thread_id, "user", text, at=NOW - timedelta(minutes=1)
    )
    r.env.public_question = text


async def status_of(sm: Sm, connection_id: str) -> str:
    async with sm() as session:
        row = await session.scalar(select(Connection.status).where(Connection.id == connection_id))
    assert row is not None
    return row


# --- set_primary_source -------------------------------------------------------------------------


async def test_set_primary_source_records_the_choice_and_explains_the_merge(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    text = await r.call("set_primary_source", connection_id=uni.calendar)
    assert "основным источником расписания вуза «Вуз А» теперь «Календарь»" in text
    assert "Дополнительные: «Кабинет»" in text
    assert "в вуз ничего не отправлено" in text
    async with sm() as session:
        pointer = await session.scalar(
            select(Space.primary_schedule_connection_id).where(Space.id == uni.space)
        )
    assert pointer == uni.calendar
    assert "set_primary_source" in MUTATING
    # and the schedule the agent reads now takes the calendar as the base
    schedule = await r.call("get_schedule", from_date="2026-10-28", to_date="2026-10-28")
    assert "ауд./место: 405" in schedule


async def test_set_primary_source_says_when_the_source_has_no_schedule_yet(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await add_event(sm, user_id, uni.cabinet, "Алгоритмы", at(28, 9))
    r = await chat(sm, user_id, uni.space)
    text = await r.call("set_primary_source", connection_id=uni.calendar)
    assert "Данных расписания у этого источника пока нет" in text


async def test_set_primary_source_refuses_a_disabled_foreign_or_unknown_source(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    other = await add_user(sm, "other")
    stranger = await university(sm, other)
    r = await chat(sm, user_id, uni.space)
    assert "Такого подключения у студента нет" in await r.call(
        "set_primary_source", connection_id=stranger.cabinet
    )
    assert "Такого подключения у студента нет" in await r.call(
        "set_primary_source", connection_id="01J0000000000000000000NONE"
    )
    async with sm() as session:
        await session.execute(
            update(Connection).where(Connection.id == uni.calendar).values(status="disabled")
        )
        await session.commit()
    r.env.forget_meta()
    refused = await r.call("set_primary_source", connection_id=uni.calendar)
    assert refused.startswith("Не выполнено")
    assert "отключён" in refused
    async with sm() as session:
        assert (
            await session.scalar(
                select(Space.primary_schedule_connection_id).where(Space.id == uni.space)
            )
            is None
        )


# --- disable_source -----------------------------------------------------------------------------


async def test_disable_without_a_confirmation_changes_nothing_and_says_what_would_stop(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    await student_says(r, sm, "отключи старый кабинет")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Не выполнено: студент ещё не подтвердил отключение")
    assert "перестанет обновляться" in text
    assert "занятий 3" in text  # what leaves the views
    assert "Ничего не удаляется" in text
    assert "Основным расписанием станет «Календарь»" in text  # the cabinet is the primary
    assert "offer_choices" in text
    assert await status_of(sm, uni.cabinet) == "active"
    assert "disable_source" in MUTATING


async def test_disable_is_refused_for_a_typed_answer(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    await agent_asks(
        sm,
        user_id,
        r.env.thread_id,
        "Отключить «Кабинет»? Он перестанет обновляться.",
        ["Да, отключить «Кабинет»", "Оставить"],
        NOW - timedelta(minutes=3),
    )
    await student_says(r, sm, "да, отключай")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Не выполнено")
    assert "не одна из кнопок" in text
    assert await status_of(sm, uni.cabinet) == "active"


async def test_disable_is_refused_when_the_pressed_button_does_not_say_to_disable(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    await agent_asks(
        sm,
        user_id,
        r.env.thread_id,
        "Что делаем с «Кабинет»?",
        ["Сделать основным, старый отключить", "Оставить как есть"],
        NOW - timedelta(minutes=3),
    )
    await student_says(r, sm, "Оставить как есть")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Не выполнено")
    assert "не говорит об отключении" in text
    assert await status_of(sm, uni.cabinet) == "active"


async def test_disable_is_refused_when_the_question_never_named_the_source(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    await agent_asks(
        sm,
        user_id,
        r.env.thread_id,
        "Отключить старый источник?",
        ["Отключить старый источник", "Оставить"],
        NOW - timedelta(minutes=3),
    )
    await student_says(r, sm, "Отключить старый источник")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Не выполнено")
    assert "не названо «Кабинет»" in text
    assert await status_of(sm, uni.cabinet) == "active"


async def test_disable_needs_the_button_of_the_previous_answer_not_an_older_one(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    await agent_asks(
        sm,
        user_id,
        r.env.thread_id,
        "Отключить «Кабинет»?",
        ["Отключить «Кабинет»", "Оставить"],
        NOW - timedelta(minutes=9),
    )
    await agent_asks(
        sm, user_id, r.env.thread_id, "Хорошо, ничего не меняю.", [], NOW - timedelta(minutes=5)
    )
    await student_says(r, sm, "Отключить «Кабинет»")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Не выполнено")
    assert await status_of(sm, uni.cabinet) == "active"


async def test_disable_after_the_button_switches_the_source_off_reversibly(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    await agent_asks(
        sm,
        user_id,
        r.env.thread_id,
        "Календарь точнее. Если отключить «Кабинет», он перестанет обновляться, а его данные "
        "исчезнут из расписания. Что делаем?",
        ["Сделать основным, старый оставить для аудиторий", "Сделать основным, старый отключить"],
        NOW - timedelta(minutes=3),
    )
    await student_says(r, sm, "Сделать основным, старый отключить")
    text = await r.call("disable_source", connection_id=uni.cabinet)
    assert text.startswith("Готово: «Кабинет» отключён")
    assert "Включить обратно" in text
    assert "Источники" in text
    assert await status_of(sm, uni.cabinet) == "disabled"
    async with sm() as session:
        cabinet = await session.get(Connection, uni.cabinet)
        assert cabinet is not None
        assert cabinet.next_sync_at is None
        stored = (
            await session.scalars(
                select(ScheduleEvent.id).where(ScheduleEvent.connection_id == uni.cabinet)
            )
        ).all()
    assert len(stored) == 3  # nothing was deleted
    # the data leaves the schedule the agent reads, and the calendar's rooms are all that is left
    r.env.forget_meta()
    schedule = await r.call("get_schedule", from_date="2026-10-28", to_date="2026-10-28")
    assert "Право" not in schedule
    assert "Алгоритмы" in schedule
    again = await r.call("disable_source", connection_id=uni.cabinet)
    assert "уже отключён" in again


async def test_disable_refuses_a_foreign_source(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    stranger = await university(sm, await add_user(sm, "other"))
    r = await chat(sm, user_id, uni.space)
    text = await r.call("disable_source", connection_id=stranger.cabinet)
    assert "Такого подключения у студента нет" in text
    assert await status_of(sm, stranger.cabinet) == "active"


# --- compare_sources ----------------------------------------------------------------------------


async def test_compare_sources_reports_matches_differences_and_what_only_one_has(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    text = await r.call(
        "compare_sources",
        a=uni.cabinet,
        b=uni.calendar,
        from_date="2026-10-26",
        to_date="2026-11-01",
    )
    assert "«Кабинет» [S1], основной" in text
    assert "«Календарь» [S2], дополнительный" in text
    assert "Занятий: в «Кабинет» 3, в «Календарь» 3. Совпало (время ±10 мин и название): 2." in text
    assert "Есть только в «Кабинет» (1): Ср 28.10 16:00–17:20 «Право»" in text  # MSK
    assert "Есть только в «Календарь» (1):" in text
    assert "«Йога»" in text
    assert "Совпавшие, но с расхождениями (1):" in text
    assert "аудитория: «Кабинет» 305 / «Календарь» 405" in text
    assert "преподаватель — только у «Календарь» 1" in text  # fills, in the second one's favour
    assert "compare_sources" not in MUTATING


async def test_compare_sources_works_in_either_direction(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    text = await r.call(
        "compare_sources",
        a=uni.calendar,
        b=uni.cabinet,
        from_date="2026-10-28",
        to_date="2026-10-28",
    )
    assert "Совпало (время ±10 мин и название): 2." in text
    assert "Есть только в «Календарь» (1)" in text
    assert "Есть только в «Кабинет» (1)" in text


async def test_compare_sources_is_at_most_fourteen_days_and_says_so(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    text = await r.call(
        "compare_sources",
        a=uni.cabinet,
        b=uni.calendar,
        from_date="2026-10-01",
        to_date="2026-12-31",
    )
    assert "(14 дн.), период сокращён до 14 дней" in text
    assert "за 2026-10-01 — 2026-10-14" in text


async def test_compare_sources_defaults_to_a_week_from_today(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)
    text = await r.call("compare_sources", a=uni.cabinet, b=uni.calendar)
    assert "за 2026-10-28 — 2026-11-03 (7 дн.)" in text
    assert "Совпало (время ±10 мин и название): 2." in text


async def test_compare_sources_is_honest_about_an_empty_period(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    text = await r.call(
        "compare_sources",
        a=uni.cabinet,
        b=uni.calendar,
        from_date="2026-11-09",
        to_date="2026-11-10",
    )
    assert "В этот период в обоих источниках занятий нет" in text
    assert "данных расписания нет" in text


async def test_compare_sources_only_compares_two_live_sources_of_one_university(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    other = await add_space(sm, user_id, "Вуз Б")
    elsewhere = await add_connection(sm, user_id, other, "Расписание Б")
    stranger = await university(sm, await add_user(sm, "other"))
    r = await chat(sm, user_id, uni.space)
    assert "разным вузам" in await r.call("compare_sources", a=uni.cabinet, b=elsewhere)
    assert "Нужны два разных подключения" in await r.call(
        "compare_sources", a=uni.cabinet, b=uni.cabinet
    )
    assert "Нужны два разных подключения" in await r.call(
        "compare_sources", a=uni.cabinet, b=stranger.cabinet
    )
    async with sm() as session:
        await session.execute(
            update(Connection).where(Connection.id == uni.calendar).values(status="disabled")
        )
        await session.commit()
    r.env.forget_meta()
    assert "отключён" in await r.call("compare_sources", a=uni.cabinet, b=uni.calendar)


# --- what the agent reads -----------------------------------------------------------------------


async def test_get_schedule_marks_merged_classes_disagreements_and_missing_ones(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    # the cabinet has the class a few days earlier, the calendar has it on 28.10 and the cabinet not
    await add_event(sm, user_id, uni.cabinet, "Йога", at(26, 18))
    await add_event(sm, user_id, uni.cabinet, "Йога", at(30, 18))
    r = await chat(sm, user_id, uni.space)
    text = await r.call("get_schedule", from_date="2026-10-28", to_date="2026-10-28")
    lines = {
        line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("- ")
    }
    algorithms = next(v for k, v in lines.items() if k.startswith("Алгоритмы"))
    assert "склеено из источников: «Кабинет» + «Календарь»" in algorithms
    assert "источники расходятся («Календарь» — ауд. 405); верно основное" in algorithms
    assert "Иванов" in algorithms  # filled from the calendar
    assert "[S1] [S2]" in algorithms  # both sources are citable
    physics = next(v for k, v in lines.items() if k.startswith("Физика"))
    assert "склеено из источников" in physics
    assert "расходятся" not in physics
    yoga = next(v for k, v in lines.items() if k.startswith("Йога"))
    assert "НЕТ В ОСНОВНОМ ИСТОЧНИКЕ" in yoga
    assert "склеено" not in yoga
    law = next(v for k, v in lines.items() if k.startswith("Право"))
    assert "склеено" not in law
    assert "расходятся" not in law
    assert {ref.title for ref in r.env.sources.refs.values()} >= {"Кабинет", "Календарь"}


async def test_the_turn_context_shows_every_schedule_source_role_and_who_chose(
    sm: Sm, user_id: str
) -> None:
    uni = await university(sm, user_id)
    await with_classes(sm, user_id, uni)
    r = await chat(sm, user_id, uni.space)

    async def context() -> str:
        async with sm() as session:
            thread = await session.get(ChatThread, r.env.thread_id)
            assert thread is not None
            return (await turn_context(session, user_id, thread, NOW)).content

    before = await context()
    assert "расписание: основной; connection_id=" + uni.cabinet in before
    assert "расписание: дополнительный; connection_id=" + uni.calendar in before
    assert "основной по умолчанию — «Кабинет», студент основной не выбирал" in before
    await r.call("set_primary_source", connection_id=uni.calendar)
    after = await context()
    assert "расписание: основной; connection_id=" + uni.calendar in after
    assert "основной — «Календарь», его выбрал студент" in after
    assert "студент основной не выбирал" not in after


async def test_the_turn_context_is_quiet_about_a_single_schedule_source(
    sm: Sm, user_id: str
) -> None:
    space = await add_space(sm, user_id, "Вуз А")
    cabinet = await add_connection(sm, user_id, space, "Кабинет", kind="portal")
    docs = await add_connection(sm, user_id, space, "Список группы", kind="sheet")
    await add_event(sm, user_id, cabinet, "Алгоритмы", at(28, 9))
    r = await chat(sm, user_id, space)
    async with sm() as session:
        thread = await session.get(ChatThread, r.env.thread_id)
        assert thread is not None
        text = (await turn_context(session, user_id, thread, NOW)).content
    assert "расписание: основной; connection_id=" + cabinet in text
    sheet_line = next(line for line in text.splitlines() if docs in line)
    assert "расписание:" not in sheet_line  # a table has no schedule role
    assert "студент основной не выбирал" not in text


async def test_the_tools_are_in_the_agent_registry(sm: Sm, user_id: str) -> None:
    uni = await university(sm, user_id)
    r = await chat(sm, user_id, uni.space)
    names = {t.spec.name: t for t in build_chat_tools(r.env)}
    assert {"set_primary_source", "disable_source", "compare_sources"} <= set(names)
    assert names["set_primary_source"].mutates
    assert names["disable_source"].mutates
    assert not names["compare_sources"].mutates
