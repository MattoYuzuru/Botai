"""Playbook runner against fakeuni: declarative fields, parser code, canary, drift and repair."""

from __future__ import annotations

import json
from typing import Any

import pytest

from botai.core.errors import BotaiError
from botai.sources.v4 import artifacts, playbooks, service, state
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
CODE_SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {"name": "timetable", "url": f"{BASE}/api/v2/timetable", "format": "json"},
    ],
    "timeout_s": 10,
}
CODE = """
import json
data = json.load(open("reads/timetable.json", encoding="utf-8"))
records = [
    {
        "kind": "schedule",
        "external_id": item["uid"],
        "fields": {
            "title": item["subject"]["name"],
            "startsAt": item["slot"]["begin"],
            "endsAt": item["slot"]["end"],
            "location": item["place"]["room"],
            "kind": item["type"],
        },
    }
    for item in data["items"]
]
print(json.dumps({"records": records, "notes": "parsed by code"}))
"""


async def make_source(
    sm: Sm,
    account: Account,
    *,
    url: str = f"{BASE}/app",
    title: str = "ЛК Fakeuni",
    kinds: list[str] | None = None,
) -> Any:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            entry_url=url,
            kinds=kinds or ["site", "schedule", "deadlines"],
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


def runner(
    tx_sessionmaker: Sm,
    account: Account,
    transport: Any,
    store: Any,
    cookies: dict[str, str],
) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession

    session: AsyncSession = tx_sessionmaker()
    return SourceRunner(
        session,
        user_id=account.user_id,
        transport=transport,
        artifact_store=store,
        cookies=cookies,
    )


async def test_sync_reads_declarative_fields_and_persists_artifacts_state_status(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            artifact_store=artifact_store,
            cookies=fakeuni_cookies,
        ).sync(source.id)
    assert report.ok, report.errors
    assert report.counts == {"schedule": 4, "deadlines": 2}
    assert len(report.records) == 6
    first = report.records[0]
    assert first.kind == "schedule"
    assert first.fields["title"] == "Математический анализ"
    assert first.fields["starts_at"].isoformat().startswith("2026-10-26T06:30")
    assert report.persisted
    assert report.source_status == "active"
    assert len(report.artifact_ids) == 2
    async with tx_sessionmaker() as session:
        rows = await artifacts.list_artifacts(session, user_id=account.user_id, source_id=source.id)
    assert len(rows) == 2
    assert all(row.storage_ref for row in rows)
    stored = list((artifact_store.root).rglob("*"))
    assert any(path.is_file() for path in stored)
    async with tx_sessionmaker() as session:
        refreshed = await service.get_source(session, user_id=account.user_id, source_id=source.id)
        assert refreshed.status == "active"
        assert refreshed.last_sync_at is not None
        state_row = await state.get_source_state(
            session, user_id=account.user_id, source_id=source.id
        )
    assert state_row is not None
    assert state_row.last_revision == 1
    assert [endpoint["name"] for endpoint in state_row.endpoints] == ["timetable", "deadlines"]


async def test_two_sources_of_one_host_live_together(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    first = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/app", title="Первое приложение"
    )
    second = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/portal/deadlines", title="Дедлайны"
    )
    await attach_playbook(tx_sessionmaker, account, first)
    await attach_playbook(tx_sessionmaker, account, second)
    async with tx_sessionmaker() as session:
        runner_one = SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        )
        first_report = await runner_one.sync(first.id)
        second_report = await runner_one.sync(second.id)
        rows = await service.list_sources(session, user_id=account.user_id)
    assert first_report.ok
    assert second_report.ok
    assert {row.id for row in rows} == {first.id, second.id}
    assert len({row.identity_key for row in rows}) == 2


async def test_canary_records_the_version_and_health(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    source = await make_source(tx_sessionmaker, account)
    playbook_id, version = await attach_playbook(tx_sessionmaker, account, source)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).canary(source.id, version=version)
        assert report.ok
        stored = await playbooks.get_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook_id, version=version
        )
        playbook = await playbooks.get_playbook(
            session, user_id=account.user_id, playbook_id=playbook_id
        )
    assert stored.canary is not None
    assert stored.canary["ok"] is True
    assert stored.canary["counts"] == {"schedule": 4, "deadlines": 2}
    assert playbook.health["ok"] is True
    async with tx_sessionmaker() as session:
        failed = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies={"FAKEUNI_SESSION": "expired"},
        ).sync(source.id)
        refreshed = await service.get_source(session, user_id=account.user_id, source_id=source.id)
    assert not failed.ok
    assert failed.errors[0].code == "source_auth_required"
    assert refreshed.status == "needs_reauth"


async def test_parser_code_runs_in_the_executor_and_not_in_the_worker(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).probe(source.id, spec=CODE_SPEC, code=CODE)
    assert report.ok, report.errors
    assert report.counts == {"schedule": 4}
    assert report.notes == ["parsed by code"]
    async with tx_sessionmaker() as session:
        broken = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).probe(source.id, spec=CODE_SPEC, code="print('not json')")
    assert not broken.ok
    assert broken.errors[0].code == "recipe_drift"


async def test_drift_is_reported_and_a_candidate_repairs_it(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    from tests.fakeuni import api as fakeuni_api

    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source)
    fakeuni_api.STATE.timetable_version = "v3"
    async with tx_sessionmaker() as session:
        failed = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).sync(source.id)
    assert not failed.ok
    assert failed.errors[0].code == "read_failed"
    assert failed.errors[0].detail == "HTTP 404"
    candidate = json.loads(json.dumps(SCHEDULE_SPEC))
    candidate["reads"][0]["url"] = f"{BASE}/api/v3/schedule"
    async with tx_sessionmaker() as session:
        repaired = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).repair(source.id, spec=candidate)
    assert repaired.ok, repaired.errors
    assert repaired.counts == {"schedule": 4, "deadlines": 2}
    async with tx_sessionmaker() as session:
        still_broken = await service.get_source(
            session, user_id=account.user_id, source_id=source.id
        )
    assert still_broken.status == "error"  # the active version is still the broken one

    # The candidate becomes useful only when stored as a version, canaried and activated.
    async with tx_sessionmaker() as session:
        playbook_id = still_broken.playbook_id
        assert playbook_id is not None
        version = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook_id,
            spec=candidate,
        )
        canary = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).canary(source.id, version=version.version)
        assert canary.ok
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook_id, version=version.version
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        healed = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).sync(source.id)
        refreshed = await service.get_source(session, user_id=account.user_id, source_id=source.id)
    assert healed.ok
    assert refreshed.status == "active"


async def test_live_read_uses_the_source_session_and_saves_an_artifact(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        result = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
        ).read_live(source.id, url=f"{BASE}/api/v2/me")
        await session.commit()
    assert result.ok
    assert "Иванова" in result.text
    assert result.artifact_id is not None
    async with tx_sessionmaker() as session:
        denied = await SourceRunner(
            session, user_id=account.user_id, transport=fakeuni_transport
        ).read_live(source.id, url=f"{BASE}/api/v2/me")
    assert not denied.ok
    assert denied.error is not None
    assert denied.error.code == "source_auth_required"


async def test_unknown_source_is_not_found(tx_sessionmaker: Sm, account: Account) -> None:
    async with tx_sessionmaker() as session:
        with pytest.raises(BotaiError):
            await SourceRunner(session, user_id=account.user_id).sync("no-such-source")


async def test_prod_refuses_parser_code_without_an_injected_executor(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    """In ENV=prod the worker must not run agent code locally: no factory means no sandbox."""
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
            env="prod",
        ).probe(source.id, spec=CODE_SPEC, code=CODE)
    assert not report.ok
    assert report.errors[0].code == "executor_unavailable"
    assert report.records == []  # the code never ran


async def test_injected_executor_factory_is_the_only_code_path(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    """Wiring passes the sandbox: the runner uses that factory instead of inventing a local one."""
    from botai.agent.v4.executor import LocalExecutor

    calls: list[str] = []

    def factory() -> LocalExecutor:
        calls.append("built")
        return LocalExecutor()

    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
            executor_factory=factory,
            env="prod",
        ).probe(source.id, spec=CODE_SPEC, code=CODE)
    assert report.ok, report.errors
    assert calls == ["built"]
    assert report.counts == {"schedule": 4}


async def test_any_injected_executor_is_closed_after_the_run(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    """A per-run executor (local or container) may own a workspace: the runner always releases it."""
    from botai.agent.v4.executor import ExecResult

    class ClosingExecutor:
        def __init__(self) -> None:
            self.closed = False

        async def run(self, request: Any) -> ExecResult:
            return ExecResult(stdout='{"records": []}', stderr="", exit_code=0, duration_ms=1)

        def close(self) -> None:
            self.closed = True

    executor = ClosingExecutor()
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
            executor_factory=lambda: executor,
        ).probe(source.id, spec=CODE_SPEC, code=CODE)
    assert report.ok, report.errors
    assert executor.closed is True
