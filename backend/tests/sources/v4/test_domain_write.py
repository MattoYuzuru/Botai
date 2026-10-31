"""A v4 sync writes the product domain with `source_id` provenance, idempotently.

The v3 path writes rows keyed by `connection_id`; a v4 source has no connection, so its rows carry
`source_id` instead and the two paths never touch each other's rows. Tests here drive the real
runner against fakeuni (ASGI transport, no network) and read the tables back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from botai.db.models import Connection, Deadline, ScheduleEvent
from botai.sources.v4 import playbooks, service
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
SCHEDULE_SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {
            "name": "timetable",
            "kind": "schedule",
            "url": f"{BASE}/api/v2/timetable",
            "format": "json",
            "items_path": "/items",
            "fields": {
                "externalId": "/uid",
                "title": "/subject/name",
                "startsAt": "/slot/begin",
                "endsAt": "/slot/end",
                "location": "/place/room",
                "kind": "/type",
            },
        },
        {
            "name": "deadlines",
            "kind": "deadlines",
            "url": f"{BASE}/api/v2/tasks/deadlines",
            "format": "json",
            "items_path": "",
            "fields": {
                "externalId": "/taskId",
                "title": "/name",
                "dueAt": {"pointer": "/due", "transform": "unixSeconds"},
                "courseId": "/course",
                "state": "/status",
            },
        },
    ],
    "timeout_s": 10,
}


async def make_source(
    sm: Sm,
    account: Account,
    *,
    url: str = f"{BASE}/app",
    title: str = "ЛК Fakeuni",
) -> Any:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            entry_url=url,
            kinds=["site", "schedule", "deadlines"],
        )
        await session.commit()
        return source


async def attach_playbook(
    sm: Sm, account: Account, source: Any, *, spec: dict[str, Any] | None = None
) -> tuple[str, int]:
    async with sm() as session:
        playbook = await playbooks.create_playbook(
            session, user_id=account.user_id, title="Fakeuni", university_id=account.university_id
        )
        version = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=spec or SCHEDULE_SPEC
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
        return playbook.id, version.version


def runner(sm: Sm, account: Account, transport: Any, cookies: dict[str, str]) -> SourceRunner:
    return SourceRunner(
        sm(), user_id=account.user_id, transport=transport, cookies=cookies, env="test"
    )


async def seed_v3_connection(sm: Sm, account: Account) -> None:
    """A v3 connection with one event that a v4 sync must not disturb."""
    async with sm() as session:
        old = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
        session.add(
            Connection(
                id="v3-conn-0000000000000001",
                user_id=account.user_id,
                space_id=account.space_id,
                origin="https://fakeuni.test",
                kind="lms",
                title="Старый кабинет",
                status="active",
                allow_hosts=[],
            )
        )
        await session.flush()
        session.add(
            ScheduleEvent(
                id="v3-event-000000000000001",
                user_id=account.user_id,
                connection_id="v3-conn-0000000000000001",
                external_id="ev-1",
                source_url="https://fakeuni.test/api/v2/timetable",
                fetched_at=old,
                seen_at=old,
                title="Старая пара",
                kind="other",
                starts_at=old,
                ends_at=old,
            )
        )
        await session.commit()


async def test_sync_writes_domain_rows_with_source_provenance(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    await seed_v3_connection(tx_sessionmaker, account)
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source)
    report = await runner(tx_sessionmaker, account, fakeuni_transport, fakeuni_cookies).sync(
        source.id
    )
    assert report.ok, report.errors
    assert report.domain["schedule"]["inserted"] == 4
    assert report.domain["deadlines"]["inserted"] == 2
    async with tx_sessionmaker() as session:
        events = (
            await session.scalars(select(ScheduleEvent).where(ScheduleEvent.source_id == source.id))
        ).all()
        deadlines = (
            await session.scalars(select(Deadline).where(Deadline.source_id == source.id))
        ).all()
        v3_event = await session.get(ScheduleEvent, "v3-event-000000000000001")
    assert len(events) == 4
    assert len(deadlines) == 2
    titles = {event.external_id: event.title for event in events}
    assert titles["ev-1"] == "Математический анализ"
    first = next(event for event in events if event.external_id == "ev-1")
    assert first.connection_id is None
    assert first.user_id == account.user_id
    assert first.source_url.startswith("https://fakeuni.test/api/")
    assert first.open_url
    assert first.fetched_at.tzinfo is not None
    assert first.spec_revision == 1
    assert first.seen_at is not None
    assert first.starts_at.isoformat().startswith("2026-10-26T06:30")
    assert {deadline.external_id for deadline in deadlines} == {"t-1", "t-2"}
    # The v3 row is exactly as it was: else the two paths would overwrite each other.
    assert v3_event is not None
    assert v3_event.connection_id == "v3-conn-0000000000000001"
    assert v3_event.source_id is None
    assert v3_event.title == "Старая пара"
    assert v3_event.seen_at == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


async def test_resync_is_idempotent_and_updates_rows_in_place(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source)
    first = await runner(tx_sessionmaker, account, fakeuni_transport, fakeuni_cookies).sync(
        source.id
    )
    assert first.ok
    async with tx_sessionmaker() as session:
        before = {
            event.external_id: event.id
            for event in (
                await session.scalars(
                    select(ScheduleEvent).where(ScheduleEvent.source_id == source.id)
                )
            ).all()
        }
    second = await runner(tx_sessionmaker, account, fakeuni_transport, fakeuni_cookies).sync(
        source.id
    )
    assert second.ok
    assert second.domain["schedule"] == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 4,
        "skipped": 0,
    }
    assert second.domain["deadlines"]["inserted"] == 0
    async with tx_sessionmaker() as session:
        rows = (
            await session.scalars(select(ScheduleEvent).where(ScheduleEvent.source_id == source.id))
        ).all()
        total = await session.scalar(
            select(func.count())
            .select_from(ScheduleEvent)
            .where(ScheduleEvent.source_id == source.id)
        )
    assert total == 4  # no duplicates
    assert {row.external_id: row.id for row in rows} == before  # same rows, updated in place

    # The university changes one class: the next sync updates that row (and marks it changed).
    from tests.fakeuni import data as fakeuni_data

    original = fakeuni_data.timetable()

    def changed() -> list[dict[str, object]]:
        items = [dict(item) for item in original]
        renamed = json.loads(json.dumps(items[0]))
        renamed["subject"] = {"name": "Математический анализ (перенос)"}
        items[0] = renamed
        return items

    monkeypatch.setattr(fakeuni_data, "timetable", changed)
    third = await runner(tx_sessionmaker, account, fakeuni_transport, fakeuni_cookies).sync(
        source.id
    )
    assert third.ok
    assert third.domain["schedule"] == {
        "inserted": 0,
        "updated": 1,
        "unchanged": 3,
        "skipped": 0,
    }
    async with tx_sessionmaker() as session:
        updated = await session.get(ScheduleEvent, before["ev-1"])
    assert updated is not None
    assert updated.title == "Математический анализ (перенос)"
    assert updated.changed is True


async def test_two_sources_of_one_host_write_their_own_rows(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    first = await make_source(tx_sessionmaker, account, url=f"{BASE}/app", title="Первый")
    second = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/portal/deadlines", title="Второй"
    )
    await attach_playbook(tx_sessionmaker, account, first)
    await attach_playbook(tx_sessionmaker, account, second)
    one = runner(tx_sessionmaker, account, fakeuni_transport, fakeuni_cookies)
    assert (await one.sync(first.id)).ok
    assert (await one.sync(second.id)).ok
    async with tx_sessionmaker() as session:
        per_source = (
            await session.execute(
                select(ScheduleEvent.source_id, func.count())
                .where(ScheduleEvent.source_id.in_([first.id, second.id]))
                .group_by(ScheduleEvent.source_id)
            )
        ).all()
    assert dict(per_source) == {first.id: 4, second.id: 4}
    # Same external ids, two sources: the natural key is per source, not per host.
    async with tx_sessionmaker() as session:
        ids = (
            await session.execute(
                select(ScheduleEvent.id).where(
                    ScheduleEvent.source_id.in_([first.id, second.id]),
                    ScheduleEvent.external_id == "ev-1",
                )
            )
        ).all()
    assert len(ids) == 2
