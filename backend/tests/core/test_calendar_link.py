"""Personal calendar links: which messages carry one, and what is kept of a typed link (L0).

The cases of recognition are the shared vectors (`contracts/calendar-link-vectors.json`): the client
reads the same file, so the two implementations cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from botai.core.calendar_link import (
    calendar_link_in,
    hide_calendar_links,
    is_calendar_link,
    is_calendar_service_page,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.calendar_url import normalize_calendar_link, origin_of_link

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "calendar-link-vectors.json").read_text(
        "utf-8"
    )
)
KEY = "k9Xq2LmZp7Vb4NwR8sTd"


@pytest.mark.parametrize("case", VECTORS["secret"], ids=lambda case: case["name"])
def test_a_personal_calendar_link_is_recognised_in_any_shape(case: dict[str, str]) -> None:
    assert calendar_link_in(case["text"])
    assert calendar_link_in(f"смотри {case['text']} и подключи")


@pytest.mark.parametrize("case", VECTORS["ordinary"], ids=lambda case: case["name"])
def test_a_link_without_a_key_or_a_page_of_a_service_is_an_ordinary_message(
    case: dict[str, str],
) -> None:
    assert not calendar_link_in(case["text"])
    assert not calendar_link_in(f"смотри {case['text']} и всё")


def test_a_public_export_stays_public_and_the_same_export_with_a_key_does_not() -> None:
    assert not is_calendar_link("https://uni.edu/events?format=ics")
    assert is_calendar_link(f"https://uni.edu/events?format=ics&token={KEY}")
    assert not is_calendar_link("https://uni.edu/ical/")
    assert is_calendar_link(f"https://uni.edu/ical/{KEY}")
    assert not is_calendar_link("https://it.uni.edu/help/ical/how-to-subscribe")
    assert not is_calendar_link("https://uni.edu/export/ics/semester-2026-autumn-schedule.ics")


def test_every_link_of_a_message_is_looked_at() -> None:
    assert calendar_link_in(
        f"сначала https://www.uni.edu/ical/ а потом webcal://cal.example.org/feed?key={KEY}"
    )


@pytest.mark.parametrize(
    "line",
    [
        'HTTP Request: GET https://calendar.example.org/export/ics.xml?private_token=k9Xq2LmZp7Vb "HTTP/1.1 200 OK"',
        "fetch https://calendar.example.org/ical/k9Xq2LmZp7Vb4NwR8sTd/basic.ics failed",
        "fetch https://p52-caldav.icloud.com/published/2/k9Xq2LmZp7Vb4NwR8sTd failed",
        "url=https://outlook.office365.com/owa/calendar/x@y/k9Xq2LmZp7Vb4NwR8sTd/calendar.html",
    ],
)
def test_a_personal_link_is_hidden_in_a_log_line_and_a_public_one_is_not(line: str) -> None:
    assert "k9Xq2LmZp7Vb" not in hide_calendar_links(line, "[hidden]")
    public = "fetch https://www.officeholidays.com/ics/russia.ics ok"
    assert hide_calendar_links(public, "[hidden]") == public


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
        ("http://cal.example.org:80/feed.ics", "https://cal.example.org/feed.ics"),
        ("webcal://cal.example.org:80/feed.ics", "https://cal.example.org/feed.ics"),
        ("https://cal.example.org", "https://cal.example.org/"),
        ("https://cal.example.org./feed.ics", "https://cal.example.org/feed.ics"),
        (
            "https://календарь.example.org/feed.ics",
            "https://xn--80aaldxhp0a2i.example.org/feed.ics",
        ),
    ],
)
def test_a_link_is_kept_as_the_https_address_it_stands_for(raw: str, kept: str) -> None:
    assert normalize_calendar_link(raw) == kept
    assert origin_of_link(kept).startswith("https://")
    assert "/" not in origin_of_link(kept)[len("https://") :]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        f"ftp://cal.example.org/feed.ics?k={KEY}",
        f"file:///etc/passwd?k={KEY}",
        f"javascript:alert('{KEY}')",
        f"https://student:{KEY}@cal.example.org/feed.ics",
        # only the port the scheme means by itself
        f"https://cal.example.org:8443/feed.ics?k={KEY}",
        f"https://cal.example.org:80/feed.ics?k={KEY}",
        f"http://cal.example.org:443/feed.ics?k={KEY}",
        # no address of a network, in any spelling
        f"https://127.0.0.1/feed.ics?k={KEY}",
        f"https://127.1/feed.ics?k={KEY}",
        f"https://0x7f.1/feed.ics?k={KEY}",
        f"https://2130706433/feed.ics?k={KEY}",
        f"https://localhost/feed.ics?k={KEY}",
        f"https://localhost./feed.ics?k={KEY}",
        f"https://[::1]/feed.ics?k={KEY}",
        f"https://intranet/feed.ics?k={KEY}",
        f"https://printer.local/feed.ics?k={KEY}",
        f"https://cal.example.org/feed.ics?k={KEY} и ещё одна",
        f"https://cal.example.org/{'a' * 2100}",
        f"https://{'a' * 64}.example.org/feed.ics?k={KEY}",
        f"https://cal_example.example.org/feed.ics?k={KEY}",
    ],
)
def test_what_is_not_a_public_calendar_link_is_refused_without_repeating_it(raw: str) -> None:
    with pytest.raises(BotaiError) as refused:
        normalize_calendar_link(raw)
    assert refused.value.code == ErrorCode.VALIDATION
    assert KEY not in refused.value.detail
    assert "example.org" not in refused.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "https://calendar.yandex.ru/week?uid=123",
        "https://calendar.yandex.com/event?event_id=123",
        "calendar.yandex.ru/",
        "https://calendar.google.com/calendar/u/0/r",
        "https://calendar.google.com/calendar/embed?src=uni%40group.calendar.google.com",
        "https://www.google.com/calendar",
        "https://outlook.office365.com/calendar/view/week",
        "https://outlook.office.com/calendar/view/month?topics=1",
        "https://outlook.live.com/owa/calendar/abc/reachcalendar.html",
        "https://www.icloud.com/calendar/",
        "https://calendar.proton.me/u/0",
    ],
)
def test_a_page_of_a_calendar_service_is_recognised_as_one(url: str) -> None:
    assert is_calendar_service_page(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://lk.fakeuni.test/calendar",  # the calendar of a cabinet of a university is its own page
        "https://calendar.fakeuni.test/week",
        "https://outlook.office.com/mail/deeplink/compose?subject=logistics",
        "https://outlook.office365.com/book/PhysicsDept@uni.edu/",
        "https://www.google.com/search?q=calendar",
        "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit",
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0",
        "https://uni.edu/events?format=ics",
        "not an address",
        "",
    ],
)
def test_the_calendar_of_a_university_and_other_pages_are_not_a_calendar_service(url: str) -> None:
    assert not is_calendar_service_page(url)
