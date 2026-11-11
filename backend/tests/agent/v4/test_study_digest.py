"""The study digest of a chat turn (D-044 §5.2, round 3): enough to answer without a tool.

Live run of 2026-11-12: the model called `study_data` (often twice) for questions the digest could
have answered, at ~5 s a step. The digest now carries, per university: the sources with their age,
the nearest not-done tasks with states and scores, the recently done ones, the latest grades with
layers, today's and tomorrow's classes, and the changes of the week — capped, deterministic and
sanitised. These tests pin each section, the cap and its priority, and print the size of a
realistic student (~15 tasks, ~40 grades).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from botai.agent.v4.study_context import CUT_LINE, DIGEST_CHARS, HEAD_RULE, study_digest
from botai.db.models import Deadline, Grade, ScheduleEvent
from tests.agent.v4.test_tools_study import FOREIGN_VALUE, NOW, Seeded, _row, _seed
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm
from tests.panels.support import make_source

pytestmark = pytest.mark.db


@pytest.fixture(name="seeded")
async def _seeded(tx_sessionmaker: Sm) -> Seeded:
    return await _seed(tx_sessionmaker)


async def _digest(sm: Sm, seeded: Seeded, **kwargs: object) -> str:
    async with sm() as session:
        return await study_digest(
            session,
            seeded.account.user_id,
            now=NOW,
            space_id=seeded.account.space_id,
            **kwargs,  # type: ignore[arg-type]
        )


async def test_every_section_is_there_in_order(tx_sessionmaker: Sm, seeded: Seeded) -> None:
    digest = await _digest(tx_sessionmaker, seeded)

    assert len(digest) <= DIGEST_CHARS
    assert HEAD_RULE in digest.splitlines()[1]
    assert f"space_id={seeded.account.space_id} — к нему относится этот чат" in digest
    assert (
        f"источник «ЛК» (site, grades) — active, обновлено 20 мин назад; "
        f"source_id={seeded.source_id}"
    ) in digest
    lines = digest.splitlines()
    heads = [line.strip() for line in lines if line.startswith("  ") and not line.startswith("   ")]
    order = [
        "не сдано (ближайшие):",
        "пары сегодня (чт 12.11):",
        "пары завтра (пт 13.11): нет",
        "оценки (последние):",
        "оценок по слоям: item 2, current 1",
        "сдано недавно:",
        "новое за 7 дн:",
    ]
    found = [head for head in heads if any(head.startswith(name) for name in order)]
    assert [next(n for n in order if h.startswith(n)) for h in found] == order
    # Not done, nearest first; a task overdue for a month is left to study_data.
    open_part = digest.split("не сдано (ближайшие):")[1].split("  пары сегодня")[0]
    assert open_part.index("«ДЗ 8»") < open_part.index("«ДЗ 9»")
    assert "«ДЗ 8» (Алгебра) до 14.11 12:00 — open «Не начато»" in open_part
    assert "ДЗ 0 просрочено" not in open_part, "long-overdue tasks are left to study_data"
    assert "«ДЗ 6»" not in open_part
    assert "   · 14:00–15:30 «Алгебра: семинар», ауд. 305" in digest
    assert "Алгебра: лекция" not in digest, "only today and tomorrow"
    assert "   · Алгебра — «ДЗ 7»: 8 из 10, 11.11" in digest
    assert "   · Алгебра — «Накопленная»: 7,5, current" in digest
    done_part = digest.split("сдано недавно:")[1]
    assert done_part.index("«ДЗ 6»") < done_part.index("«ДЗ 1 старое»")
    assert "оценка «ДЗ 7» (Алгебра): 8 из 10, 12.11 11:00" in digest
    assert FOREIGN_VALUE not in digest
    assert "Чужое ДЗ" not in digest


async def test_a_realistic_student_fits_and_needs_no_tool(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    user, source = seeded.account.user_id, seeded.source_id
    async with tx_sessionmaker() as session:
        for index in range(15):
            session.add(
                Deadline(
                    title=f"Домашнее задание {index + 10}: численные методы",
                    course_external_id="alg",
                    due_at=NOW + timedelta(days=index % 9, hours=index),
                    state=("open", "submitted", "unknown")[index % 3],
                    state_text=("Не начато", "Решение прикреплено", "На проверке")[index % 3],
                    **_row(user, source, f"real-d-{index}"),
                )
            )
        for index in range(40):
            session.add(
                Grade(
                    title=f"Работа {index + 1}",
                    value=str(4 + index % 7),
                    value_num=float(4 + index % 7),
                    scale_max=10,
                    layer=("item", "item", "item", "current")[index % 4],
                    course_external_id=("alg", "hist")[index % 2],
                    graded_at=NOW - timedelta(days=index),
                    **_row(user, source, f"real-g-{index}"),
                )
            )
        for index in range(4):
            starts = NOW + timedelta(days=1, hours=index * 2 - 3)
            session.add(
                ScheduleEvent(
                    title=f"Пара {index + 1}",
                    starts_at=starts,
                    ends_at=starts + timedelta(minutes=90),
                    kind="seminar",
                    location_text=f"ауд. {300 + index}",
                    **_row(user, source, f"real-e-{index}"),
                )
            )
        await session.commit()
    digest = await _digest(tx_sessionmaker, seeded)
    print(f"\ndigest of ~15 tasks / ~40 grades: {len(digest)} chars")

    assert len(digest) <= DIGEST_CHARS
    assert digest.count("\n   · ") >= 12 + 20, "12 open tasks and 20 grades made it"
    assert "«Работа 1»: 4 из 10" in digest, "the newest grade is listed"
    assert "пары завтра (пт 13.11):" in digest
    assert "«Пара 1» семинар, ауд. 300" in digest
    assert f"source_id={source}" in digest


async def test_the_cap_cuts_details_but_keeps_the_sources(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    user = seeded.account.user_id
    for index in range(12):
        await make_source(
            tx_sessionmaker,
            user_id=user,
            space_id=seeded.account.space_id,
            title=f"Источник {index} " + "y" * 50,
            url=f"https://fakeuni.test/extra/{index}",
        )
    async with tx_sessionmaker() as session:
        for index in range(40):
            session.add(
                Deadline(
                    title=f"Очень длинное название задания номер {index} " + "x" * 60,
                    due_at=NOW + timedelta(hours=index + 1),
                    state="open",
                    state_text="В работе",
                    **_row(user, seeded.source_id, f"bulk-{index}"),
                )
            )
        await session.commit()

    full = await _digest(tx_sessionmaker, seeded)
    assert len(full) <= DIGEST_CHARS
    small = await _digest(tx_sessionmaker, seeded, limit=2500)
    assert len(small) <= 2500
    assert small.endswith(CUT_LINE)
    assert f"source_id={seeded.source_id}" in small, "the ids come before the details"
    assert "не сдано (ближайшие):" in small, "the open tasks get the budget first"
    assert "x" * 60 not in full, "long titles are cut"
    assert await _digest(tx_sessionmaker, seeded) == full, "deterministic"
