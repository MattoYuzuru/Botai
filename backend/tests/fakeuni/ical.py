"""Owner: W3. iCal exports of the synthetic university.

Two shapes of a personal export link, as the big calendar services make them: the key in the query
of an `.ics` file (`/calendar/export.ics?token=...`) and the key in the query of a service address
(`/export/ics.xml?private_token=...`, on a host of its own), and one with the key in the path
(`/ical/<key>/basic.ics`). All serve the same feed: a weekly lecture with a skipped week and a
moved one, one-off seminars, a deadline, a holiday, a cancelled event and a task.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()
TOKEN = "ICAL-FIXTURE-TOKEN-91bc"  # marker grepped by security check C1
URL = f"https://lms.fakeuni.test/calendar/export.ics?token={TOKEN}"
# The calendar service of the university: a host of its own, a personal key in the query.
SERVICE_TOKEN = "CALENDAR-FIXTURE-TOKEN-6e9d"  # marker grepped by security check C1
SERVICE_URL = f"https://calendar.fakeuni.test/export/ics.xml?private_token={SERVICE_TOKEN}"
SERVICE_WEBCAL = f"webcal://calendar.fakeuni.test/export/ics.xml?private_token={SERVICE_TOKEN}"
PATH_TOKEN = "CALENDAR-PATH-TOKEN-b41f"  # marker grepped by security check C1
PATH_URL = f"https://calendar.fakeuni.test/ical/{PATH_TOKEN}/basic.ics"
PAGE_URL = "https://calendar.fakeuni.test/week"  # the calendar in the browser: HTML, not iCal

_state = {"revoked": False}

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//fakeuni//EN
BEGIN:VEVENT
UID:lec-1@fakeuni.test
DTSTART:20261026T063000Z
DTEND:20261026T075000Z
RRULE:FREQ=WEEKLY;COUNT=6
EXDATE:20261109T063000Z
SUMMARY:Математический анализ
LOCATION:А-101
CATEGORIES:Lecture
END:VEVENT
BEGIN:VEVENT
UID:lec-1@fakeuni.test
RECURRENCE-ID:20261102T063000Z
DTSTART:20261102T080000Z
DTEND:20261102T092000Z
SUMMARY:Математический анализ
LOCATION:Б-204
CATEGORIES:Lecture
END:VEVENT
BEGIN:VEVENT
UID:sem-1@fakeuni.test
DTSTART;TZID=Europe/Moscow:20261028T131000
DTEND;TZID=Europe/Moscow:20261028T143000
SUMMARY:Базы данных
LOCATION:В-310
CATEGORIES:Seminar
END:VEVENT
BEGIN:VEVENT
UID:due-1@fakeuni.test
DTSTART:20261029T170000Z
DTEND:20261029T170000Z
SUMMARY:ДЗ 4: графы
URL:https://lms.fakeuni.test/mod/assign/view.php?id=901
END:VEVENT
BEGIN:VEVENT
UID:proj-1@fakeuni.test
DTSTART:20261030T090000Z
DTEND:20261030T100000Z
SUMMARY:Проектная защита
CATEGORIES:Deadline
END:VEVENT
BEGIN:VEVENT
UID:holiday-1@fakeuni.test
DTSTART;VALUE=DATE:20261104
SUMMARY:День народного единства
END:VEVENT
BEGIN:VEVENT
UID:cancel-1@fakeuni.test
DTSTART:20261031T100000Z
DTEND:20261031T113000Z
SUMMARY:Отменённая консультация
STATUS:CANCELLED
END:VEVENT
BEGIN:VTODO
UID:todo-1@fakeuni.test
SUMMARY:Прочитать главу 5
DUE:20261030T200000Z
STATUS:COMPLETED
END:VTODO
END:VCALENDAR
"""


def reset() -> None:
    _state["revoked"] = False


def revoke_link() -> None:
    """The user regenerated the export link: the old one stops working."""
    _state["revoked"] = True


def _feed() -> Response:
    return Response(FEED.replace("\n", "\r\n"), media_type="text/calendar; charset=utf-8")


@router.get("/calendar/export.ics", response_model=None)
async def export(request: Request) -> Response:
    if _state["revoked"]:
        return Response(status_code=404)
    if request.query_params.get("token") != TOKEN:
        return Response(status_code=403)
    return _feed()


@router.get("/export/ics.xml", response_model=None)
async def export_service(request: Request) -> Response:
    if _state["revoked"]:
        return Response(status_code=404)
    if request.query_params.get("private_token") != SERVICE_TOKEN:
        return Response(status_code=403)
    return _feed()


@router.get("/ical/{key}/basic.ics", response_model=None)
async def export_by_path(key: str) -> Response:
    if _state["revoked"]:
        return Response(status_code=404)
    return _feed() if key == PATH_TOKEN else Response(status_code=404)


@router.get("/week", response_model=None)
async def calendar_page() -> Response:
    """The calendar as the browser shows it: a page, not an export."""
    return Response("<html><body><h1>Календарь</h1></body></html>", media_type="text/html")
