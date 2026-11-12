"""A repaired playbook must not leave its old rows behind (the parser-change sweep).

A new playbook version often reads the same tasks under a new external-id scheme. A successful
sync of version N deletes the rows of each kind it wrote that another version left and it did not
see (time-bound kinds only inside the dates it read); its own unseen rows stay. A deleted task hands
the student's mark to its surviving copy, and nothing of the parser change is «Новое».
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select

from botai.agent.v4.executor import ExecResult, Executor
from botai.core.ids import new_id
from botai.db.models import Course, Deadline, Grade, ScheduleEvent, Source
from botai.domain import repo
from botai.sources.v4 import domain, playbooks, service
from botai.sources.v4.runner import NormalizedRecord, SourceRunner
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import Sm
from tests.sources.v4.support import Account, create_account

pytestmark = pytest.mark.db

T1 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
DUE = datetime(2026, 11, 20, 20, 59, tzinfo=UTC)


def task(ext: str, title: str, due: datetime = DUE, state: str = "open") -> NormalizedRecord:
    return NormalizedRecord(
        kind="deadlines",
        external_id=ext,
        fields={"title": title, "due_at": due, "state": state, "state_text": None},
    )


def grade(ext: str, title: str, value: str, **fields: Any) -> NormalizedRecord:
    return NormalizedRecord(
        kind="grades", external_id=ext, fields={"title": title, "value": value, **fields}
    )


def lesson(ext: str, starts: datetime) -> NormalizedRecord:
    return NormalizedRecord(
        kind="schedule",
        external_id=ext,
        fields={"title": "Пара", "starts_at": starts, "ends_at": starts + timedelta(minutes=90)},
    )


async def _source(sm: Sm, account: Account, title: str = "ЛК") -> str:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            entry_url=f"https://fakeuni.test/{new_id().lower()}",
            kinds=["site", "deadlines", "grades", "schedule", "courses"],
        )
        await session.commit()
        return source.id


async def _write(
    sm: Sm, account: Account, source_id: str, records: list[NormalizedRecord], **kw: Any
) -> dict[str, dict[str, int]]:
    async with sm() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        counts = await domain.write_records(
            session, user_id=account.user_id, source=source, records=records, **kw
        )
        await session.commit()
        return counts


async def _ids(sm: Sm, model: Any, source_id: str) -> set[str]:
    async with sm() as session:
        found = await session.scalars(select(model.external_id).where(model.source_id == source_id))
        return set(found)


async def test_a_new_parser_version_sweeps_the_old_ids_keeps_marks_and_makes_no_news(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await _source(sm, account)
    other = await _source(sm, account, title="Портал")
    stranger = await create_account(sm, title="Чужой вуз")
    foreign = await _source(sm, stranger)
    v1 = [
        task("old-1", "ДЗ 1"),
        task("old-2", "ДЗ 2"),
        grade("legacy", "Баллы за курс", "2", scale="баллы за курс"),
        NormalizedRecord(kind="courses", external_id="1136", fields={"title": "Матанализ"}),
    ]
    await _write(sm, account, source, v1, now=T1, revision=1, synced_before=False, sweep=True)
    await _write(sm, account, other, v1[:2], now=T1, revision=1, synced_before=False, sweep=True)
    await _write(sm, stranger, foreign, v1[:2], now=T1, revision=1, synced_before=False, sweep=True)
    async with sm() as session:  # the student handed in «ДЗ 1»: the mark lives on the old row
        row = await session.scalar(
            select(Deadline).where(Deadline.source_id == source, Deadline.external_id == "old-1")
        )
        assert row is not None
        row.user_state = "submitted"
        await session.commit()

    t2 = T1 + timedelta(hours=3)
    v2 = [
        task("task:9001", "ДЗ 1"),
        task("task:9002", "ДЗ 2"),
        grade("total:1136", "Баллы за курс", "2", scale="из 10", layer="current"),
    ]
    counts = await _write(
        sm, account, source, v2, now=t2, revision=2, synced_before=True, sweep=True
    )
    assert counts["deadlines"]["deleted"] == 2
    assert counts["grades"]["deleted"] == 1
    assert counts["deadlines"]["inserted"] == 2
    assert await _ids(sm, Deadline, source) == {"task:9001", "task:9002"}
    assert await _ids(sm, Grade, source) == {"total:1136"}
    # a kind this run did not write is untouched; so are another source and another user
    assert await _ids(sm, Course, source) == {"1136"}
    assert await _ids(sm, Deadline, other) == {"old-1", "old-2"}
    assert await _ids(sm, Deadline, foreign) == {"old-1", "old-2"}

    async with sm() as session:
        rows = {
            r.external_id: r
            for r in await session.scalars(select(Deadline).where(Deadline.source_id == source))
        }
        new_grade = await session.scalar(select(Grade).where(Grade.source_id == source))
        updates = await repo.list_updates(
            session, account.user_id, since=T1 - timedelta(days=30), now=t2
        )
    assert rows["task:9001"].user_state == "submitted"  # the mark followed the task
    assert rows["task:9002"].user_state is None
    assert all(r.changed_at is None for r in rows.values())  # a parser change is not news
    assert new_grade is not None
    assert new_grade.changed_at is None
    assert updates == []

    # the same version again: what it does not see now stays (reads are windowed)
    t3 = t2 + timedelta(hours=3)
    again = await _write(
        sm, account, source, [v2[0]], now=t3, revision=2, synced_before=True, sweep=True
    )
    assert again["deadlines"]["deleted"] == 0
    assert await _ids(sm, Deadline, source) == {"task:9001", "task:9002"}
    # and now that version 2 is known, a genuinely new task is news again
    newer = [task("task:9003", "ДЗ 3")]
    await _write(sm, account, source, newer, now=t3, revision=2, synced_before=True, sweep=True)
    async with sm() as session:
        fresh = await session.scalar(
            select(Deadline.changed_at).where(
                Deadline.source_id == source, Deadline.external_id == "task:9003"
            )
        )
    assert fresh == t3


async def test_rows_without_a_version_are_older_and_dates_outside_the_read_stay(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await _source(sm, account)
    week = [lesson(f"l-{d}", T1 + timedelta(days=d)) for d in (0, 7, 14)]
    await _write(sm, account, source, week, now=T1, revision=None, synced_before=False)
    # version 3 reads only the second week, under new ids
    counts = await _write(
        sm,
        account,
        source,
        [lesson("lesson:7", T1 + timedelta(days=7))],
        now=T1 + timedelta(hours=1),
        revision=3,
        synced_before=True,
        sweep=True,
    )
    assert counts["schedule"]["deleted"] == 1  # «l-7» (no version, inside the read span)
    assert await _ids(sm, ScheduleEvent, source) == {"l-0", "l-14", "lesson:7"}


async def test_without_sweep_nothing_is_deleted(tx_sessionmaker: Sm, account: Account) -> None:
    sm = tx_sessionmaker
    source = await _source(sm, account)
    await _write(sm, account, source, [task("a", "ДЗ")], now=T1, revision=1)
    counts = await _write(sm, account, source, [task("b", "ДЗ")], now=T1, revision=2)
    assert counts["deadlines"]["deleted"] == 0
    assert await _ids(sm, Deadline, source) == {"a", "b"}


# --- through the runner: only a successful sync sweeps ------------------------------------------


class OutputExecutor:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def run(self, request: Any) -> ExecResult:
        return ExecResult(stdout=json.dumps(self.output), stderr="", exit_code=0, duration_ms=1)

    def close(self) -> None:
        pass


def _output(prefix: str) -> dict[str, Any]:
    return {
        "records": [
            {
                "kind": "deadlines",
                "external_id": f"{prefix}{n}",
                "fields": {
                    "title": f"ДЗ {n}",
                    "dueAt": "2026-11-20T20:59:00Z",
                    "stateText": "Задано",
                },
            }
            for n in (1, 2)
        ]
    }


async def _activate(sm: Sm, account: Account, source_id: str, playbook_id: str | None) -> str:
    async with sm() as session:
        if playbook_id is None:
            playbook = await playbooks.create_playbook(
                session, user_id=account.user_id, title="ЛК", language="python"
            )
            playbook_id = playbook.id
        version = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook_id,
            code=f"print('parser {new_id()}')",
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook_id,
            version=version.version,
            canary={"ok": True},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook_id, version=version.version
        )
        await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source_id,
            patch=SourcePatch(playbook_id=playbook_id),
        )
        await session.commit()
        return playbook_id


async def _run(sm: Sm, account: Account, source_id: str, output: dict[str, Any], run: str) -> Any:
    async with sm() as session:
        runner = SourceRunner(
            session,
            user_id=account.user_id,
            executor_factory=lambda: cast("Executor", OutputExecutor(output)),
            env="test",
        )
        if run == "probe":
            return await runner.probe(source_id, code="print('draft')")
        return await runner.sync(source_id)


async def test_only_a_successful_sync_sweeps(tx_sessionmaker: Sm, account: Account) -> None:
    sm = tx_sessionmaker
    source = await _source(sm, account)
    playbook = await _activate(sm, account, source, None)
    first = await _run(sm, account, source, _output("old-"), "sync")
    assert first.ok, first.errors
    assert await _ids(sm, Deadline, source) == {"old-1", "old-2"}

    await _activate(sm, account, source, playbook)  # version 2, new ids
    probe = await _run(sm, account, source, _output("new-"), "probe")
    assert probe.ok
    expired = await _run(sm, account, source, {**_output("new-"), "auth_required": True}, "sync")
    assert not expired.ok
    empty = await _run(sm, account, source, {"records": [], "empty_reason": "каникулы"}, "sync")
    assert empty.domain == {}
    assert await _ids(sm, Deadline, source) == {"old-1", "old-2"}  # nothing was swept

    swept = await _run(sm, account, source, _output("new-"), "sync")
    assert swept.ok, swept.errors
    assert swept.domain["deadlines"]["deleted"] == 2
    assert "deleted" in swept.model_dump(mode="json")["domain"]["deadlines"]
    assert await _ids(sm, Deadline, source) == {"new-1", "new-2"}
