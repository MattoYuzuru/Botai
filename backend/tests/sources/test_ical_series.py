"""How the `ical` adapter expands the series of real calendars (L0, no network).

Calendar services write the end of a series in different ways; every way must give the whole series,
not the first class only.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest
from icalendar import Calendar

from botai.contracts.specs import IcalSpec
from botai.sources.adapter import Record
from botai.sources.adapters.ical import IcalAdapter
from tests.fakeuni.app import create_fakeuni
from tests.sources.helpers import make_ctx

NOW = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
HEAD = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//fixture//EN\n"
TAIL = "END:VCALENDAR\n"


def event(uid: str, start: str, rule: str, summary: str, *, end: str | None = None) -> str:
    return (
        f"BEGIN:VEVENT\nUID:{uid}@fixture\n{start}\n{end or ''}\n"
        f"RRULE:{rule}\nSUMMARY:{summary}\nEND:VEVENT\n"
    ).replace("\n\n", "\n")


MOSCOW = "DTSTART;TZID=Europe/Moscow:20261026T093000"
MOSCOW_END = "DTEND;TZID=Europe/Moscow:20261026T105000"


def read(*events: str, now: datetime = NOW) -> list[Record]:
    ctx = make_ctx(
        create_fakeuni(),
        allow_hosts=("calendar.fakeuni.test",),
        origin="https://calendar.fakeuni.test",
        logged_in=False,
    )
    ctx.now = now
    text = (HEAD + "".join(events) + TAIL).replace("\n", "\r\n")
    spec = IcalSpec(adapter="ical", origin="https://calendar.fakeuni.test")
    return IcalAdapter(spec).result_for(Calendar.from_ical(text.encode()), ctx).records


def starts(records: list[Record], title: str) -> list[str]:
    return sorted(str(r.fields["startsAt"])[:16] for r in records if r.fields["title"] == title)


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        # UTC: the usual form; 06:00Z on 16 November is before the class of that day (06:30Z)
        ("FREQ=WEEKLY;UNTIL=20261116T060000Z", 3),
        ("FREQ=WEEKLY;UNTIL=20261116T070000Z", 4),
        # a date: the whole last day counts
        ("FREQ=WEEKLY;UNTIL=20261116", 4),
        # a local time in the zone of DTSTART
        ("FREQ=WEEKLY;UNTIL=20261116T093000", 4),
        ("FREQ=WEEKLY;UNTIL=20261116T093000;INTERVAL=1;WKST=MO", 4),
        ("FREQ=WEEKLY;COUNT=4", 4),
        ("FREQ=WEEKLY;INTERVAL=2;UNTIL=20261130", 3),
    ],
)
def test_a_series_ends_where_its_calendar_says_whatever_the_form_of_the_end(
    rule: str, expected: int
) -> None:
    records = read(event("a", MOSCOW, rule, "Лекция", end=MOSCOW_END))
    assert len(starts(records, "Лекция")) == expected


def test_all_day_and_floating_series_are_whole_too() -> None:
    all_day = event(
        "b", "DTSTART;VALUE=DATE:20261028", "FREQ=WEEKLY;UNTIL=20261118", "Выходной",
        end="DTEND;VALUE=DATE:20261029",
    )  # fmt: skip
    floating = event(
        "c", "DTSTART:20261026T093000", "FREQ=WEEKLY;UNTIL=20261116T093000", "Без зоны",
        end="DTEND:20261026T105000",
    )  # fmt: skip
    records = read(all_day, floating)
    assert len(starts(records, "Выходной")) == 4  # 28 Oct, 4, 11 and 18 Nov
    assert len(starts(records, "Без зоны")) == 4


def test_a_skipped_week_and_a_moved_class_still_apply_to_a_series_with_a_date_end() -> None:
    series = (
        "BEGIN:VEVENT\nUID:s@fixture\n" + MOSCOW + "\n" + MOSCOW_END + "\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20261130\n"
        "EXDATE;TZID=Europe/Moscow:20261109T093000\n"
        "SUMMARY:Семинар\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:s@fixture\nRECURRENCE-ID;TZID=Europe/Moscow:20261102T093000\n"
        "DTSTART;TZID=Europe/Moscow:20261102T110000\nDTEND;TZID=Europe/Moscow:20261102T122000\n"
        "SUMMARY:Семинар\nEND:VEVENT\n"
    )
    records = read(series)
    assert starts(records, "Семинар") == [
        "2026-10-26T06:30",
        "2026-11-02T08:00",  # moved: 11:00 in Moscow
        "2026-11-16T06:30",  # 9 November skipped
        "2026-11-23T06:30",
        "2026-11-30T06:30",
    ]


def test_a_rule_that_cannot_be_read_costs_one_class_not_the_calendar() -> None:
    broken = event("d", MOSCOW, "FREQ=WEEKLY;UNTIL=20261316", "Сломанное правило", end=MOSCOW_END)
    fine = event("e", MOSCOW, "FREQ=WEEKLY;COUNT=2", "Целое", end=MOSCOW_END)
    records = read(broken, fine)
    assert len(starts(records, "Сломанное правило")) == 1
    assert len(starts(records, "Целое")) == 2
    assert Counter(r.kind for r in records) == {"scheduleEvents": 3}


def test_a_series_keeps_its_local_hour_across_the_end_of_daylight_saving_time() -> None:
    """Berlin turns the clocks back on 25 October: 09:00 stays 09:00, only the UTC hour moves."""
    series = event(
        "dst",
        "DTSTART;TZID=Europe/Berlin:20261019T090000",
        "FREQ=WEEKLY;UNTIL=20261109",
        "Vorlesung",
        end="DTEND;TZID=Europe/Berlin:20261019T100000",
    )
    assert starts(read(series, now=datetime(2026, 10, 18, tzinfo=UTC)), "Vorlesung") == [
        "2026-10-19T07:00",  # summer time, UTC+2
        "2026-10-26T08:00",  # winter time, UTC+1
        "2026-11-02T08:00",
        "2026-11-09T08:00",
    ]


def test_a_skipped_day_written_as_a_date_skips_the_timed_class_of_that_day() -> None:
    series = (
        "BEGIN:VEVENT\nUID:x@fixture\n" + MOSCOW + "\n" + MOSCOW_END + "\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20261130\nEXDATE;VALUE=DATE:20261109\n"
        "SUMMARY:Практика\nEND:VEVENT\n"
    )
    assert starts(read(series), "Практика") == [
        "2026-10-26T06:30",
        "2026-11-02T06:30",
        "2026-11-16T06:30",
        "2026-11-23T06:30",
        "2026-11-30T06:30",
    ]


def test_a_skipped_class_and_a_moved_one_are_found_in_utc_as_well_as_in_local_time() -> None:
    series = (
        "BEGIN:VEVENT\nUID:u@fixture\n" + MOSCOW + "\n" + MOSCOW_END + "\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\nEXDATE:20261109T063000Z\nSUMMARY:Лекция\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:u@fixture\nRECURRENCE-ID:20261102T063000Z\n"
        "DTSTART;TZID=Europe/Moscow:20261102T120000\nDTEND;TZID=Europe/Moscow:20261102T130000\n"
        "SUMMARY:Лекция\nEND:VEVENT\n"
    )
    assert starts(read(series), "Лекция") == [
        "2026-10-26T06:30",
        "2026-11-02T09:00",  # moved to 12:00 in Moscow
        "2026-11-16T06:30",
    ]


def test_a_cancelled_class_of_a_series_is_gone_and_the_others_stay() -> None:
    series = (
        "BEGIN:VEVENT\nUID:c@fixture\n" + MOSCOW + "\n" + MOSCOW_END + "\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\nSUMMARY:Консультация\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:c@fixture\nRECURRENCE-ID;TZID=Europe/Moscow:20261102T093000\n"
        "STATUS:CANCELLED\nDTSTART;TZID=Europe/Moscow:20261102T093000\nSUMMARY:Консультация\nEND:VEVENT\n"
    )
    assert starts(read(series), "Консультация") == ["2026-10-26T06:30", "2026-11-09T06:30"]


def test_a_zone_the_calendar_defines_itself_is_understood() -> None:
    zone = (
        "BEGIN:VTIMEZONE\nTZID:Russian Standard Time\nBEGIN:STANDARD\nDTSTART:16010101T000000\n"
        "TZOFFSETFROM:+0300\nTZOFFSETTO:+0300\nEND:STANDARD\nEND:VTIMEZONE\n"
    )
    series = event(
        "z",
        "DTSTART;TZID=Russian Standard Time:20261026T093000",
        "FREQ=WEEKLY;UNTIL=20261116T093000",
        "Пара",
        end="DTEND;TZID=Russian Standard Time:20261026T105000",
    )
    assert len(starts(read(zone + series), "Пара")) == 4
