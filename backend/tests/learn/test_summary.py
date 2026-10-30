"""What a connection tells the student: counts in words, the nearest classes, the closing line (L0)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from botai.contracts.specs import Kind
from botai.learn.summary import (
    UNITS,
    confirm_details,
    counted,
    day_title,
    every_text,
    finished_text,
    found_text,
    kind_line,
    plain,
    plural,
    upcoming_days,
    weeks_to,
)
from botai.learn.ui import kinds_text
from botai.sources.adapter import Record

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday, 15:00 in Moscow


def event(
    number: int, day: str, start: str, end: str, title: str, room: str | None = "А-101"
) -> Record:
    fields = {
        "title": title,
        "startsAt": f"{day}T{start}:00+03:00",
        "endsAt": f"{day}T{end}:00+03:00",
        **({"location": room} if room else {}),
    }
    return Record("scheduleEvents", f"e{number}", fields, "https://u.test/api")


def schedule(*days: str) -> list[Record]:
    return [event(i, day, "09:30", "10:50", f"Пара {i}") for i, day in enumerate(days)]


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "занятие"),
        (2, "занятия"),
        (5, "занятий"),
        (11, "занятий"),
        (21, "занятие"),
        (24, "занятия"),
    ],
)
def test_plural_follows_the_russian_rules(n: int, word: str) -> None:
    assert plural(n, UNITS["scheduleEvents"]) == word


def test_counts_are_said_in_the_words_of_their_kind() -> None:
    assert counted(12, UNITS["deadlines"]) == "12 заданий со сроками"
    assert counted(1, UNITS["deadlines"]) == "1 задание со сроком"
    assert counted(8, UNITS["courses"]) == "8 курсов"
    assert found_text("scheduleEvents", 35) == "Найдено занятий: 35"
    assert found_text("deadlines", 12) == "Найдено заданий со сроками: 12"
    assert (
        kinds_text(
            {
                "scheduleEvents": {"inserted": 30, "updated": 3, "unchanged": 2},
                "deadlines": {"inserted": 12, "updated": 0, "unchanged": 0},
            }
        )
        == "расписание — 35 занятий, дедлайны — 12 заданий со сроками"
    )


def test_the_schedule_says_how_much_and_for_how_long() -> None:
    # 26 October (Monday) to 6 December (Sunday): six whole weeks
    days = ["2026-10-26"] * 34 + ["2026-12-06"]
    assert (
        kind_line("scheduleEvents", schedule(*days), MSK)
        == "35 занятий с 26 октября по 6 декабря — 6 недель"
    )
    assert kind_line("scheduleEvents", schedule("2026-10-28", "2026-10-28"), MSK) == (
        "2 занятия на 28 октября"
    )
    # Wednesday to the next Tuesday touches two calendar weeks
    assert kind_line("scheduleEvents", schedule("2026-10-28", "2026-11-03"), MSK).endswith(
        "— 2 недели"
    )
    assert kind_line("scheduleEvents", schedule("2026-10-26", "2026-10-30"), MSK).endswith(
        "— 1 неделя"
    )
    others = [Record("courses", f"c{i}", {"title": f"Курс {i}"}, "u") for i in range(8)]
    assert kind_line("courses", others, MSK) == "8 курсов"


def test_the_preview_is_the_two_nearest_days_that_still_have_classes() -> None:
    records = [
        event(1, "2026-10-26", "09:30", "10:50", "Давно прошло"),  # Monday: over
        event(2, "2026-10-27", "09:30", "10:50", "Утром сегодня"),  # today, already over
        event(3, "2026-10-27", "17:00", "18:20", "Вечером сегодня", "Б-2"),  # today, ahead
        event(4, "2026-10-30", "11:10", "12:30", "Пятница"),
        event(5, "2026-11-03", "11:10", "12:30", "Дальше"),
    ]
    days = upcoming_days(records, NOW, MSK)
    assert [day_title(day) for day, _ in days] == ["Вторник, 27 октября", "Пятница, 30 октября"]
    assert [[r.fields["title"] for r in events] for _, events in days] == [
        ["Вечером сегодня"],
        ["Пятница"],
    ]


def test_a_free_week_still_gives_the_next_two_days_with_classes() -> None:
    records = schedule("2026-11-09", "2026-11-09", "2026-11-12", "2026-11-13")
    days = upcoming_days(records, NOW, MSK)
    assert [day_title(day) for day, _ in days] == ["Понедельник, 9 ноября", "Четверг, 12 ноября"]


def test_the_confirmation_names_what_was_found_and_shows_the_nearest_classes() -> None:
    records: dict[Kind, list[Record]] = {
        "scheduleEvents": [
            event(1, "2026-10-28", "13:00", "14:20", "Базы данных", "В-310"),
            event(2, "2026-10-28", "09:30", "10:50", "Алгоритмы", None),
            event(3, "2026-10-30", "15:10", "16:30", "Английский язык", "online"),
            event(4, "2026-11-02", "09:30", "10:50", "Позже"),
        ],
        "deadlines": [Record("deadlines", f"d{i}", {"title": "ДЗ"}, "u") for i in range(12)],
    }
    text = confirm_details(records, NOW, MSK, heading="Нашла в «Личный кабинет»:")
    assert text == (
        "Нашла в «Личный кабинет»:\n"
        "\n"
        "- **Расписание:** 4 занятия с 28 октября по 2 ноября — 2 недели\n"
        "- **Дедлайны:** 12 заданий со сроками\n"
        "\n"
        "Ближайшие пары:\n"
        "\n"
        "**Среда, 28 октября**\n"
        "- 09:30–10:50 · Алгоритмы\n"
        "- 13:00–14:20 · Базы данных · В-310\n"
        "\n"
        "**Пятница, 30 октября**\n"
        "- 15:10–16:30 · Английский язык · online"
    )


def test_without_classes_ahead_the_confirmation_says_so_instead_of_inventing_a_preview() -> None:
    text = confirm_details({"scheduleEvents": schedule("2026-10-20")}, NOW, MSK)
    assert "Ближайших пар в этих данных нет" in text
    assert "**" not in text.split("Ближайших")[1]


def test_a_long_day_is_cut_and_the_whole_text_fits_the_block() -> None:
    day = [event(i, "2026-10-28", "09:30", "10:50", f"Занятие {i}") for i in range(12)]
    text = confirm_details({"scheduleEvents": day}, NOW, MSK)
    assert "- …и ещё 4" in text
    assert len(text) <= 2000


def test_source_text_cannot_make_links_or_markup_in_the_confirmation() -> None:
    hostile = event(
        1,
        "2026-10-28",
        "09:30",
        "10:50",
        "[Нажми](https://evil.test/x) **жирно** `код` botai://threads/01JAAAAAAAAAAAAAAAAAAAAAAA",
        "<b>А-1</b>",
    )
    text = confirm_details({"scheduleEvents": [hostile]}, NOW, MSK)
    line = next(row for row in text.splitlines() if row.startswith("- 09:30"))
    for mark in ("[", "]", "**", "`", "<", ">", "botai://"):
        assert mark not in line
    assert plain("# заголовок\n\n- пункт") == "заголовок - пункт"
    assert len(plain("я" * 300)) == 100


def test_weeks_to_counts_calendar_weeks_from_this_one() -> None:
    assert weeks_to(datetime(2026, 12, 6, 10, tzinfo=MSK), NOW, MSK) == 6
    assert weeks_to(datetime(2026, 10, 30, 10, tzinfo=MSK), NOW, MSK) == 1
    assert weeks_to(datetime(2026, 10, 20, 10, tzinfo=MSK), NOW, MSK) is None  # all over
    assert weeks_to(None, NOW, MSK) is None


def test_every_is_said_in_hours_when_it_is_whole_hours() -> None:
    assert every_text(180) == "каждые 3 часа"
    assert every_text(60) == "каждый час"
    assert every_text(120) == "каждые 2 часа"
    assert every_text(300) == "каждые 5 часов"
    assert every_text(30) == "каждые 30 минут"
    assert every_text(1440) == "раз в сутки"


def test_the_closing_message_is_friendly_and_says_what_to_ask() -> None:
    assert finished_text(
        {"scheduleEvents": 35}, university="НИУ ВШЭ", every_minutes=180, weeks=6
    ) == (
        "Готово! Расписание НИУ ВШЭ подключено: 35 занятий на 6 недель вперёд. "
        "Обновляю само каждые 3 часа — спросите, например, «что у меня завтра?»"
    )
    assert finished_text({"deadlines": 12}, university=None, every_minutes=60) == (
        "Готово! Дедлайны подключены: 12 заданий со сроками. "
        "Обновляю само каждый час — спросите, например, «какие у меня сроки на этой неделе?»"
    )
    assert finished_text(
        {"scheduleEvents": 3, "courses": 8}, university="НИУ ВШЭ", every_minutes=0, weeks=1
    ) == (
        "Готово! Данные НИУ ВШЭ подключены: расписание — 3 занятия на 1 неделю вперёд; "
        "курсы — 8 курсов. Спросите, например, «что у меня завтра?»"
    )
    assert "данных в нём пока не нашлось" in finished_text({}, university=None, every_minutes=180)


def test_the_moment_does_not_depend_on_the_server_zone() -> None:
    late = event(1, "2026-10-28", "23:30", "23:50", "Поздно")
    early_utc = Record(
        "scheduleEvents",
        "e2",
        {
            "title": "Ночью по UTC",
            "startsAt": (datetime(2026, 10, 28, 22, 0, tzinfo=UTC)).isoformat(),
        },
        "u",
    )
    ((day, events),) = upcoming_days([late, early_utc], NOW, MSK)[:1]
    assert day_title(day) == "Среда, 28 октября"
    # 22:00 UTC is already 01:00 of the 29th in Moscow, a day of its own
    assert [r.fields["title"] for r in events] == ["Поздно"]
    assert (NOW + timedelta(hours=1)).tzinfo is not None
