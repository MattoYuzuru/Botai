"""Owner: W3. iCal export of the synthetic LMS (`/calendar/export.ics?token=...`)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()
TOKEN = "ICAL-FIXTURE-TOKEN-91bc"  # marker grepped by security check C1
URL = f"https://lms.fakeuni.test/calendar/export.ics?token={TOKEN}"

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


@router.get("/calendar/export.ics", response_model=None)
async def export(request: Request) -> Response:
    if _state["revoked"]:
        return Response(status_code=404)
    if request.query_params.get("token") != TOKEN:
        return Response(status_code=403)
    return Response(FEED.replace("\n", "\r\n"), media_type="text/calendar; charset=utf-8")
