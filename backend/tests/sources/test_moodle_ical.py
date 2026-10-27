"""Moodle Web Services and iCal adapters on the fakeuni fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import InMemoryVault, SecretKind
from botai.sources.adapter import AdapterResult, SessionExpired
from botai.sources.adapters.moodle_ws import ALLOWED_FUNCTIONS
from botai.sources.factory import AdapterFactory
from tests.fakeuni import ical as fakeuni_ical
from tests.fakeuni import moodle as fakeuni_moodle
from tests.sources.helpers import CONN_ID, LMS_HOST, NOW, USER_ID, make_ctx

ALL_FUNCTIONS = sorted(ALLOWED_FUNCTIONS)
LMS = "https://lms.fakeuni.test"


def by(result: AdapterResult, kind: str) -> dict[str, Any]:
    return {r.external_id: r for r in result.records if r.kind == kind}


async def vault_with(kind: SecretKind, value: str) -> InMemoryVault:
    vault = InMemoryVault()
    await vault.put(USER_ID, CONN_ID, kind, value)
    return vault


def schema_functions() -> list[str]:
    path = Path(__file__).resolve().parents[3] / "contracts" / "adapter-spec.schema.json"
    schema = json.loads(path.read_text())
    return schema["$defs"]["moodleWsSpec"]["properties"]["functions"]["items"]["enum"]


def test_code_allowlist_equals_the_contract_enum() -> None:
    assert sorted(schema_functions()) == ALL_FUNCTIONS


# --- Moodle ---------------------------------------------------------------------------------


async def moodle_ctx(app: object, token: str = fakeuni_moodle.TOKEN) -> Any:
    vault = await vault_with(SecretKind.TOKEN, token)
    return make_ctx(app, vault=vault, allow_hosts=(LMS_HOST,), origin=LMS, logged_in=False)


def moodle_spec(functions: list[str] | None = None) -> dict[str, Any]:
    return {"adapter": "moodle_ws", "origin": LMS, "functions": functions or ALL_FUNCTIONS}


async def test_moodle_reads_courses_deadlines_assignments_and_grades(fakeuni_app: object) -> None:
    result = await AdapterFactory().create(moodle_spec()).fetch(await moodle_ctx(fakeuni_app))

    courses = by(result, "courses")
    assert courses["11"].fields["title"] == "Алгоритмы и структуры данных"
    assert courses["11"].fields["archived"] is False
    assert courses["12"].fields["archived"] is True  # ended long ago

    deadlines = by(result, "deadlines")
    # the calendar event of an assignment and the assignment itself are one deadline
    assert sorted(deadlines) == ["assign-501", "assign-502", "event-8002", "quiz-33"]
    hw4 = deadlines["assign-501"].fields
    assert hw4["state"] == "submitted"  # submission status says so
    assert hw4["assignmentId"] == "501"
    assert hw4["courseId"] == "11"
    hw5 = deadlines["assign-502"].fields
    assert hw5["state"] == "open"
    assert hw5["effectiveDueAt"] > hw5["dueAt"]  # an extension moves the effective deadline

    assignments = by(result, "assignments")
    assert assignments["501"].fields["description"] == "Решите задачи 1–3."
    assert assignments["501"].fields["maxScore"] == 10
    assert "maxScore" not in assignments["502"].fields  # grade 0 means ungraded

    events = by(result, "scheduleEvents")
    assert list(events) == ["event-8001"]  # the zero-duration reminder became a deadline
    assert events["event-8001"].fields["location"] == "online"

    grades = by(result, "grades")
    assert sorted(grades) == ["11-1", "11-3"]  # the "-" item has no grade yet
    assert grades["11-1"].fields["value"] == "8,50"
    assert grades["11-1"].fields["scale"] == "0–10"
    assert grades["11-3"].fields["title"] == "Итого по курсу"


async def test_moodle_token_travels_in_the_body_never_in_a_url(fakeuni_app: object) -> None:
    urls: list[str] = []
    bodies: list[str] = []

    class Spy(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self.inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            bodies.append((await request.aread()).decode())
            return await self.inner.handle_async_request(request)

    ctx = await moodle_ctx(fakeuni_app)
    ctx.transport = Spy(ctx.transport)
    result = await AdapterFactory().create(moodle_spec()).fetch(ctx)
    assert urls
    assert all(fakeuni_moodle.TOKEN not in u for u in urls)
    assert all(fakeuni_moodle.TOKEN in b for b in bodies)
    assert fakeuni_moodle.TOKEN not in repr(result)


async def test_moodle_rejects_a_function_outside_the_allowlist() -> None:
    spec = moodle_spec(["core_user_update_users"])
    with pytest.raises(BotaiError) as exc:
        AdapterFactory().create(spec)
    assert exc.value.code == ErrorCode.RECIPE_REJECTED


async def test_moodle_invalid_token_means_the_user_must_give_a_new_one(fakeuni_app: object) -> None:
    with pytest.raises(SessionExpired) as exc:
        await AdapterFactory().create(moodle_spec()).fetch(await moodle_ctx(fakeuni_app, "wrong"))
    assert exc.value.reauth == "token"


async def test_moodle_without_a_stored_token_asks_for_one(fakeuni_app: object) -> None:
    ctx = make_ctx(fakeuni_app, allow_hosts=(LMS_HOST,), origin=LMS, logged_in=False)
    with pytest.raises(SessionExpired) as exc:
        await AdapterFactory().create(moodle_spec()).fetch(ctx)
    assert exc.value.reauth == "token"


async def test_moodle_function_without_access_fails_alone(fakeuni_app: object) -> None:
    ctx = await moodle_ctx(fakeuni_app)

    class Denying(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = (await request.aread()).decode()
            if "gradereport_user_get_grade_items" in body:
                return httpx.Response(200, json={"exception": "x", "errorcode": "accessexception"})
            return await httpx.ASGITransport(app=fakeuni_app).handle_async_request(  # type: ignore[arg-type]
                httpx.Request(request.method, request.url, headers=request.headers, content=body)
            )

    ctx.transport = Denying()
    result = await AdapterFactory().create(moodle_spec()).fetch(ctx)
    bad = {o.endpoint_id: o for o in result.outcomes if not o.ok}
    assert list(bad) == ["gradereport_user_get_grade_items"]
    assert result.count("courses") == 2
    assert "grades" not in result.complete_kinds()


# --- iCal -----------------------------------------------------------------------------------


async def ical_ctx(app: object, url: str = fakeuni_ical.URL) -> Any:
    vault = await vault_with(SecretKind.ICAL_URL, url)
    return make_ctx(app, vault=vault, allow_hosts=(LMS_HOST,), origin=LMS, logged_in=False)


ICAL_SPEC = {
    "adapter": "ical",
    "origin": LMS,
    "classify": {"deadlineIf": {"zeroDuration": True, "categoryIn": ["Deadline"]}},
}


async def test_ical_classifies_expands_and_skips(fakeuni_app: object) -> None:
    result = await AdapterFactory().create(ICAL_SPEC).fetch(await ical_ctx(fakeuni_app))
    events = by(result, "scheduleEvents")
    deadlines = by(result, "deadlines")

    # weekly lecture: 6 occurrences from the rule, minus the EXDATE, and the moved one replaced
    lectures = sorted(k for k in events if k.startswith("lec-1"))
    starts = [events[k].fields["startsAt"] for k in lectures]
    assert "2026-11-09T06:30:00+00:00" not in starts  # excluded date
    assert "2026-11-02T06:30:00+00:00" not in starts  # replaced by the override...
    assert "2026-11-02T08:00:00+00:00" in starts  # ...which sits at the new time
    assert events[lectures[0]].fields["location"] in {"А-101", "Б-204"}
    # today (Tue Oct 27) is the first day of the window, so Monday Oct 26 is outside it
    assert starts[0] == "2026-11-02T06:30:00+00:00".replace("06:30", "08:00")

    # a TZID-qualified class is converted to UTC; an all-day event spans the day
    assert events["sem-1@fakeuni.test"].fields["startsAt"] == "2026-10-28T10:10:00+00:00"
    holiday = events["holiday-1@fakeuni.test"].fields
    assert holiday["startsAt"] == "2026-11-03T21:00:00+00:00"
    assert holiday["endsAt"] == "2026-11-04T21:00:00+00:00"
    assert "cancel-1@fakeuni.test" not in events  # cancelled events are not shown

    # zero duration -> deadline; category -> deadline; VTODO -> deadline
    assert sorted(deadlines) == [
        "due-1@fakeuni.test",
        "proj-1@fakeuni.test",
        "todo-1@fakeuni.test",
    ]
    assert deadlines["due-1@fakeuni.test"].fields["dueAt"] == "2026-10-29T17:00:00+00:00"
    assert deadlines["due-1@fakeuni.test"].fields["state"] == "open"
    assert deadlines["proj-1@fakeuni.test"].fields["dueAt"] == "2026-10-30T10:00:00+00:00"
    assert deadlines["todo-1@fakeuni.test"].fields["state"] == "submitted"
    assert result.complete_kinds() == {"scheduleEvents", "deadlines"}


async def test_ical_default_classification_is_zero_duration_only(fakeuni_app: object) -> None:
    spec = {"adapter": "ical", "origin": LMS}
    result = await AdapterFactory().create(spec).fetch(await ical_ctx(fakeuni_app))
    assert "proj-1@fakeuni.test" in by(result, "scheduleEvents")  # no category rule given
    assert "due-1@fakeuni.test" in by(result, "deadlines")


async def test_ical_summary_pattern_classifier(fakeuni_app: object) -> None:
    spec = {
        "adapter": "ical",
        "origin": LMS,
        "classify": {"deadlineIf": {"zeroDuration": False, "summaryPattern": r"защит"}},
    }
    result = await AdapterFactory().create(spec).fetch(await ical_ctx(fakeuni_app))
    assert sorted(by(result, "deadlines")) == ["proj-1@fakeuni.test", "todo-1@fakeuni.test"]


async def test_ical_revoked_link_asks_for_a_new_one(fakeuni_app: object) -> None:
    fakeuni_ical.revoke_link()
    with pytest.raises(SessionExpired) as exc:
        await AdapterFactory().create(ICAL_SPEC).fetch(await ical_ctx(fakeuni_app))
    assert exc.value.reauth == "token"


async def test_ical_link_to_a_foreign_host_is_refused(fakeuni_app: object) -> None:
    ctx = await ical_ctx(fakeuni_app, "https://evil.test/steal.ics")
    with pytest.raises(BotaiError) as exc:
        await AdapterFactory().create(ICAL_SPEC).fetch(ctx)
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED


async def test_ical_garbage_is_a_format_problem(fakeuni_app: object) -> None:
    ctx = await ical_ctx(fakeuni_app)
    ctx.transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>not a calendar"))
    result = await AdapterFactory().create(ICAL_SPEC).fetch(ctx)
    assert [o.problem for o in result.outcomes] == ["format", "format"]
    assert result.complete_kinds() == set()


async def test_ical_url_token_is_not_in_the_result(fakeuni_app: object) -> None:
    result = await AdapterFactory().create(ICAL_SPEC).fetch(await ical_ctx(fakeuni_app))
    assert fakeuni_ical.TOKEN not in repr(result)
    assert NOW  # keeps the shared constant imported for readers of this module
