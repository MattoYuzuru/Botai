"""Links to a calendar export: which messages carry one, and what is kept of a pasted link (L0)."""

from __future__ import annotations

import pytest

from botai.core.calendar_link import (
    calendar_link_in,
    is_calendar_link,
    normalize_calendar_link,
    origin_of_link,
)
from botai.core.errors import BotaiError, ErrorCode

KEY = "k9Xq2LmZp7Vb4NwR8sTd"
EXPORTS = [
    f"webcal://calendar.example.org/feed?key={KEY}",
    f"webcals://cal.example.org/u/{KEY}",
    f"https://calendar.yandex.ru/export/ics.xml?private_token={KEY}&tz_id=Europe/Moscow",
    f"https://calendar.yandex.com/export/ics.xml?private_token={KEY}",
    f"https://calendar.google.com/calendar/ical/student%40example.org/private-{KEY}/basic.ics",
    f"https://outlook.office365.com/owa/calendar/{KEY}@example.org/{KEY}/calendar.ics",
    f"https://outlook.live.com/owa/calendar/{KEY}/reachcalendar.ics",
    f"https://lms.example.org/calendar/export.ics?token={KEY}",
    f"https://lms.example.org/export/ics/{KEY}",
    f"https://lms.example.org/ical/{KEY}",
    f"https://example.org/feed?format=ics&u={KEY}",
    f"calendar.yandex.ru/export/ics.xml?private_token={KEY}",
    "HTTPS://CAL.EXAMPLE.ORG/Timetable.ICS",
]
NOT_EXPORTS = [
    "https://calendar.yandex.ru/week?uid=123",
    "https://calendar.google.com/calendar/u/0/r",
    "https://outlook.office365.com/calendar/view/week",
    "https://example.org/physics",
    "https://example.org/topics/magic-tricks?x=1",
    "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit",
    "https://example.org/schedule.pdf",
    "example.org",
]


@pytest.mark.parametrize("url", EXPORTS)
def test_a_calendar_export_link_is_recognised_in_any_shape(url: str) -> None:
    assert is_calendar_link(url)
    assert calendar_link_in(f"вот моя ссылка {url} — подключи")
    assert calendar_link_in(url.upper() if url.isascii() else url)


@pytest.mark.parametrize("url", NOT_EXPORTS)
def test_a_page_of_a_calendar_service_or_any_other_link_is_not(url: str) -> None:
    assert not is_calendar_link(url)
    assert not calendar_link_in(f"смотри {url}")


@pytest.mark.parametrize(
    "text",
    [
        "Расписание у меня в Яндекс Календаре, как подключить?",
        "Подключи календарь вуза",
        "Мои пары в physics.ics нет, это просто слово",
        "Что такое ICS?",
        "",
    ],
)
def test_talking_about_calendars_is_not_a_link(text: str) -> None:
    assert not calendar_link_in(text)


def test_trailing_punctuation_does_not_hide_a_link() -> None:
    assert calendar_link_in(f"(https://calendar.yandex.ru/export/ics.xml?private_token={KEY}).")
    assert calendar_link_in(f"«webcal://cal.example.org/feed?key={KEY}»")


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        (f"webcal://cal.example.org/feed?key={KEY}", f"https://cal.example.org/feed?key={KEY}"),
        (f"webcals://cal.example.org/u/{KEY}", f"https://cal.example.org/u/{KEY}"),
        (f"http://cal.example.org/feed.ics?k={KEY}", f"https://cal.example.org/feed.ics?k={KEY}"),
        (
            f"  https://Cal.Example.org/feed.ics?k={KEY}#frag  ",
            f"https://cal.example.org/feed.ics?k={KEY}",
        ),
        (
            f"<https://cal.example.org/feed.ics?k={KEY}>",
            f"https://cal.example.org/feed.ics?k={KEY}",
        ),
        (f"cal.example.org/feed.ics?k={KEY}", f"https://cal.example.org/feed.ics?k={KEY}"),
        ("https://cal.example.org:443/feed.ics", "https://cal.example.org/feed.ics"),
        ("https://cal.example.org", "https://cal.example.org/"),
    ],
)
def test_a_link_is_kept_as_the_https_address_it_stands_for(raw: str, kept: str) -> None:
    assert normalize_calendar_link(raw) == kept
    assert origin_of_link(kept) == "https://cal.example.org"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        f"ftp://cal.example.org/feed.ics?k={KEY}",
        f"file:///etc/passwd?k={KEY}",
        f"javascript:alert('{KEY}')",
        f"https://student:{KEY}@cal.example.org/feed.ics",
        f"https://cal.example.org:8443/feed.ics?k={KEY}",
        f"https://127.0.0.1/feed.ics?k={KEY}",
        f"https://localhost/feed.ics?k={KEY}",
        f"https://[::1]/feed.ics?k={KEY}",
        f"https://intranet/feed.ics?k={KEY}",
        f"https://printer.local/feed.ics?k={KEY}",
        f"https://cal.example.org/feed.ics?k={KEY} и ещё одна",
        f"https://cal.example.org/{'a' * 2100}",
    ],
)
def test_what_is_not_a_public_calendar_link_is_refused_without_repeating_it(raw: str) -> None:
    with pytest.raises(BotaiError) as refused:
        normalize_calendar_link(raw)
    assert refused.value.code == ErrorCode.VALIDATION
    assert KEY not in refused.value.detail
    assert "example.org" not in refused.value.detail
