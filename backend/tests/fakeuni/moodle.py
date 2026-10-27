"""Owner: W3. Moodle Web Services of the synthetic LMS (`/webservice/rest/server.php`).

Only the response shapes the adapter reads. The token is a marker grepped by the security checks.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tests.fakeuni import data

router = APIRouter()
TOKEN = "MOODLE-FIXTURE-TOKEN-7f3a"  # marker grepped by security check C1
USER_ID = 42
COURSE_IDS = (11, 12)


def _ts(days: int, hours: int = 12) -> int:
    return int((data.WEEK_START + timedelta(days=days, hours=hours)).timestamp())


def _courses() -> list[dict[str, object]]:
    return [
        {"id": 11, "shortname": "ALG", "fullname": "Алгоритмы и структуры данных", "enddate": 0},
        {"id": 12, "shortname": "OLD", "fullname": "Архивный курс", "enddate": _ts(-60)},
    ]


def _assignments() -> list[dict[str, object]]:
    return [
        {
            "id": 501,
            "cmid": 901,
            "name": "ДЗ 4: графы",
            "duedate": _ts(3, 17),
            "intro": "<p>Решите задачи <b>1–3</b>.</p>",
            "grade": 10,
        },
        {
            "id": 502,
            "cmid": 902,
            "name": "ДЗ 5: потоки",
            "duedate": _ts(9, 17),
            "intro": "",
            "grade": 0,
        },
    ]


def _error(code: str) -> dict[str, object]:
    return {"exception": "moodle_exception", "errorcode": code, "message": "denied"}


@router.post("/webservice/rest/server.php", response_model=None)
async def server(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("wstoken") != TOKEN:
        return JSONResponse(_error("invalidtoken"))
    fn = str(form.get("wsfunction", ""))
    if fn == "core_webservice_get_site_info":
        return JSONResponse({"userid": USER_ID, "sitename": "Fake LMS", "fullname": "Иванова Анна"})
    if fn == "core_enrol_get_users_courses":
        return JSONResponse(_courses())
    if fn == "core_calendar_get_action_events_by_timesort":
        events = [
            {
                "id": 7001,
                "name": "ДЗ 4: графы closes",
                "timesort": _ts(3, 17),
                "timestart": _ts(3, 17),
                "modulename": "assign",
                "instance": 501,
                "eventtype": "due",
                "url": "https://lms.fakeuni.test/mod/assign/view.php?id=901",
                "course": {"id": 11, "fullname": "Алгоритмы и структуры данных"},
            },
            {
                "id": 7002,
                "name": "Тест 2",
                "timesort": _ts(4, 10),
                "timestart": _ts(4, 10),
                "modulename": "quiz",
                "instance": 33,
                "eventtype": "close",
                "course": {"id": 11},
            },
        ]
        return JSONResponse({"events": events, "firstid": 7001, "lastid": 7002})
    if fn == "core_calendar_get_calendar_upcoming_view":
        return JSONResponse(
            {
                "events": [
                    {
                        "id": 8001,
                        "name": "Вебинар по графам",
                        "timestart": _ts(2, 15),
                        "timeduration": 5400,
                        "eventtype": "course",
                        "location": "online",
                    },
                    {
                        "id": 8002,
                        "name": "Напоминание",
                        "timestart": _ts(2, 18),
                        "timeduration": 0,
                        "eventtype": "user",
                    },
                ]
            }
        )
    if fn == "mod_assign_get_assignments":
        return JSONResponse(
            {"courses": [{"id": 11, "fullname": "Алгоритмы", "assignments": _assignments()}]}
        )
    if fn == "mod_assign_get_submission_status":
        if str(form.get("assignid")) == "501":
            return JSONResponse(
                {
                    "lastattempt": {
                        "submission": {"status": "submitted"},
                        "gradingstatus": "notgraded",
                    }
                }
            )
        return JSONResponse(
            {
                "lastattempt": {
                    "submission": {"status": "new"},
                    "extensionduedate": _ts(11, 17),
                }
            }
        )
    if fn == "gradereport_user_get_grade_items":
        if str(form.get("courseid")) != "11":
            return JSONResponse({"usergrades": [{"courseid": 12, "gradeitems": []}]})
        return JSONResponse(
            {
                "usergrades": [
                    {
                        "courseid": 11,
                        "gradeitems": [
                            {
                                "id": 1,
                                "itemname": "ДЗ 1",
                                "itemtype": "mod",
                                "gradeformatted": "8,50",
                                "graderaw": 8.5,
                                "grademax": 10,
                                "weightraw": 0.2,
                                "gradedategraded": _ts(-5),
                            },
                            {"id": 2, "itemname": "ДЗ 2", "gradeformatted": "-", "graderaw": None},
                            {
                                "id": 3,
                                "itemname": None,
                                "itemtype": "course",
                                "gradeformatted": "72,0",
                                "grademax": 100,
                            },
                        ],
                    }
                ]
            }
        )
    return JSONResponse(_error("accessexception"))
