"""L1: grades as layers, the source's status words, the student's corrections and «Новое».

A code playbook reads the synthetic LMS (`tests/fakeuni/lms.py`) the way docs/v4/27 §3 asks: item
grades with `max`/`assignmentId`/`url`/`layer`, a course total without a scale, deadlines with the
page's own status words in `stateText` and an exact link per task. The test drives the real runner
(sandboxed code, fakeuni over ASGI) and reads the domain back through the repositories.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from botai.core.clock import SystemClock
from botai.db.models import Deadline, Grade
from botai.domain import overrides, repo
from botai.sources.v4 import playbooks, service
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import Sm
from tests.fakeuni import lms
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
STATE_QUERY = "&".join(f"state={state}" for state in lms.STATES)
SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [{"name": "tasks", "url": f"{BASE}{lms.TASKS_PATH}?{STATE_QUERY}", "format": "json"}],
    "timeout_s": 10,
}
CODE = """
import json
tasks = json.load(open("reads/tasks.json", encoding="utf-8"))
BASE = "https://fakeuni.test"
SURE = {"evaluated": "graded", "review": "submitted"}
records = []
totals = {}
for t in tasks:
    course, ex = t["course"], t["exercise"]
    url = (f"{BASE}/learn/courses/{course['id']}/themes/{t['theme']['id']}"
           f"/longreads/{t['longread']['id']}")
    fields = {"title": ex["name"], "dueAt": t["deadline"], "courseId": str(course["id"]),
              "assignmentId": str(ex["id"]), "stateText": t["stateLabel"], "url": url}
    if t["state"] in SURE:
        fields["state"] = SURE[t["state"]]
    records.append({"kind": "deadlines", "external_id": str(t["id"]), "fields": fields})
    records.append({"kind": "assignments", "external_id": str(ex["id"]), "fields": {
        "title": ex["name"], "courseId": str(course["id"]), "maxScore": ex["maxScore"],
        "url": url}})
    if t["score"] is not None:
        records.append({"kind": "grades", "external_id": f"grade-{t['id']}", "fields": {
            "title": ex["name"], "value": t["score"], "max": ex["maxScore"], "layer": "item",
            "assignmentId": str(ex["id"]), "courseId": str(course["id"]), "url": url,
            "gradedAt": t["submitAt"], "comment": "Проверено"}})
        totals[course["id"]] = totals.get(course["id"], 0) + t["score"]
for course_id, total in totals.items():
    # what a cabinet shows as "points for the course": a number with no scale
    records.append({"kind": "grades", "external_id": f"total-{course_id}", "fields": {
        "title": "Баллы за курс", "value": total, "scale": "баллы за курс",
        "layer": "current", "courseId": str(course_id)}})
print(json.dumps({"records": records}))
"""


@pytest.fixture(autouse=True)
def _fresh_lms() -> Iterator[None]:
    lms.reset()
    yield
    lms.reset()


async def _source_with_playbook(sm: Sm, account: Account) -> str:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="ЛК Fakeuni",
            entry_url=f"{BASE}/learn",
            kinds=["site", "deadlines", "assignments", "grades"],
        )
        playbook = await playbooks.create_playbook(
            session,
            user_id=account.user_id,
            title="Fakeuni LMS",
            university_id=account.university_id,
            language="python",
        )
        version = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC, code=CODE
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=version.version,
            canary={"ok": True},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=version.version
        )
        await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()
        return source.id


async def _sync(sm: Sm, account: Account, source_id: str, transport: Any, cookies: Any) -> Any:
    async with sm() as session:
        report = await SourceRunner(
            session, user_id=account.user_id, transport=transport, cookies=cookies, env="test"
        ).sync(source_id)
    assert report.ok, report.errors
    return report


async def test_item_grades_statuses_corrections_and_updates(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    sm = tx_sessionmaker
    source_id = await _source_with_playbook(sm, account)
    report = await _sync(sm, account, source_id, fakeuni_transport, fakeuni_cookies)

    # the run tells the agent what is still wrong: the course total has a number but no scale
    assert [w.code for w in report.warnings] == ["grades_without_numbers"]
    assert report.warnings[0].examples == ["Баллы за курс: 2", "Баллы за курс: 9"]
    assert "warnings" in report.model_dump(mode="json")

    async with sm() as session:
        grades = {
            g.external_id: g
            for g in await session.scalars(select(Grade).where(Grade.source_id == source_id))
        }
        rows = {
            d.external_id: d
            for d in await session.scalars(select(Deadline).where(Deadline.source_id == source_id))
        }
    item = grades["grade-9005"]
    assert (item.value, item.scale, item.value_num, item.scale_max) == ("9", "из 10", 9.0, 10.0)
    assert (item.layer, item.assignment_external_id, item.comment) == ("item", "8005", "Проверено")
    assert item.url == f"{BASE}/learn/courses/4101/themes/7005/longreads/6005"
    total = grades["total-4101"]
    assert (total.layer, total.scale, total.value_num, total.scale_max) == (
        "current",
        "баллы за курс",
        None,
        None,
    )
    # the page's words are kept; the state is read from them (or taken when the parser is sure)
    assert {ext: (d.state, d.state_text) for ext, d in rows.items()} == {
        "9001": ("unknown", "Не приступал"),
        "9002": ("open", "В работе"),
        "9003": ("submitted", "Решение прикреплено"),
        "9004": ("open", "На доработке"),
        "9005": ("graded", "Принято"),
    }
    # the first sync of a source is not news
    assert all(g.changed_at is None for g in grades.values())
    assert all(d.changed_at is None for d in rows.values())

    async with sm() as session:
        tasks = {
            d.title: d
            for d in await repo.list_deadlines(session, account.user_id, include_done=True)
        }
    graded = tasks["Домашнее задание 2"]
    assert graded.grade is not None
    assert (graded.grade.value, graded.grade.value_num, graded.grade.scale_max) == ("9", 9.0, 10.0)
    assert graded.url == f"{BASE}/learn/courses/4101/themes/7005/longreads/6005"
    assert (graded.state, graded.state_text, graded.state_source) == ("graded", "Принято", "source")
    assert tasks["Домашнее задание 3"].grade is not None  # the failed one is graded 2 of 10
    assert tasks["Домашнее задание 4"].grade is None

    # the student: «Не приступал» in this source means open; «В работе» here is already sent
    async with sm() as session:
        rule = await overrides.set_status_rule(
            session, account.user_id, source_id=source_id, label="  не  ПРИСТУПАЛ ", state="open"
        )
        assert rule.label == "не приступал"
        await overrides.set_user_state(session, account.user_id, rows["9002"].id, "submitted")
        await session.commit()
    async with sm() as session:
        shown = {
            d.title: d
            for d in await repo.list_deadlines(session, account.user_id, include_done=True)
        }
        todo = {d.title for d in await repo.list_deadlines(session, account.user_id)}
    assert (shown["Домашнее задание 4"].state, shown["Домашнее задание 4"].state_source) == (
        "open",
        "rule",
    )
    assert (shown["Лабораторная работа 2"].state, shown["Лабораторная работа 2"].state_source) == (
        "submitted",
        "student",
    )
    assert todo == {"Домашнее задание 4", "Домашнее задание 3"}

    # the teacher checks «Контрольная 1»: the next sync brings a grade and a new state
    for task in lms.STATE.tasks:
        if task["id"] == 9003:
            task.update(state="evaluated", stateLabel="Принято", score=7.0)
    second = await _sync(sm, account, source_id, fakeuni_transport, fakeuni_cookies)
    assert second.domain["grades"]["inserted"] == 1
    async with sm() as session:
        changed = {
            d.external_id: d.changed_at
            for d in await session.scalars(select(Deadline).where(Deadline.source_id == source_id))
        }
        backlog = await session.scalar(
            select(Deadline.state).where(
                Deadline.source_id == source_id, Deadline.external_id == "9001"
            )
        )
        now = SystemClock().now()
        updates = await repo.list_updates(
            session, account.user_id, since=now - timedelta(days=1), now=now
        )
        today = await repo.today(session, account.user_id, now=now)
    assert backlog == "open"  # the rule is applied on every sync, over the words
    assert changed["9003"] is not None  # submitted -> graded is news
    assert [changed[ext] for ext in ("9001", "9004", "9005")] == [None, None, None]
    assert changed["9002"] is None  # the student's own mark is not news from the source
    by_kind = {(u.kind, u.title) for u in updates}
    assert ("grade", "Контрольная 1") in by_kind
    assert ("deadline", "Контрольная 1") in by_kind
    grade_update = next(u for u in updates if u.kind == "grade" and u.title == "Контрольная 1")
    assert (grade_update.value, grade_update.scale_max, grade_update.layer) == ("7", 10.0, "item")
    assert grade_update.course_title is None  # no course rows in this playbook
    assert [u.changed_at for u in updates] == sorted((u.changed_at for u in updates), reverse=True)
    assert {u.id for u in today.updates} <= {u.id for u in updates}
    assert today.updates

    # deleting the rule gives the words back their say
    async with sm() as session:
        await overrides.delete_status_rule(session, account.user_id, rule.id)
        await session.commit()
        state = await session.scalar(
            select(Deadline.state).where(
                Deadline.source_id == source_id, Deadline.external_id == "9001"
            )
        )
    assert state == "unknown"
