"""A browser playbook is a first-class reading path: the page's own JSON API feeds the domain.

The real production factory launches Chromium; fakeuni is served on loopback HTTP with no proxy.
Two scenarios the HTTP path cannot read:

* a client-side cabinet: the HTML is an empty shell and the classes are drawn from a Bearer JSON
  API — the runner parses the *observed* API answer (network-first) instead of the DOM and does not
  re-fetch what the page already asked for;
* a login page: the password comes from the Vault as a `secretHandle` and is typed into the form,
  never appearing in the report, while the resulting session reads the cabinet's APIs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.agent.v4.factory import VaultSecretReader
from botai.core.vault import InMemoryVault, SecretKind
from botai.db.models import ScheduleEvent
from botai.sources.v4 import playbooks, service, state
from botai.sources.v4.artifacts import FileArtifactStore
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from botai.sources.v4.secrets import VaultSessionReader
from tests.auth.support import Sm
from tests.fakeuni import client_app, data
from tests.fakeuni import login as fakeuni_login
from tests.sources.v4.support import Account

pytestmark = [pytest.mark.db, pytest.mark.browser]

SCHEDULE_READ: dict[str, Any] = {
    "name": "schedule",
    "kind": "schedule",
    "url": "/client-cabinet/api/schedule",
    "format": "json",
    "items_path": "/items",
    "fields": {
        "externalId": "/uid",
        "title": "/title",
        "startsAt": "/starts_at",
        "endsAt": "/ends_at",
        "location": "/room",
    },
}
TIMETABLE_READ: dict[str, Any] = {
    "name": "timetable",
    "kind": "schedule",
    "url": "/api/v2/timetable",
    "format": "json",
    "items_path": "/items",
    "fields": {
        "externalId": "/uid",
        "title": "/subject/name",
        "startsAt": "/slot/begin",
        "endsAt": "/slot/end",
        "location": "/place/room",
    },
}
DEADLINES_READ: dict[str, Any] = {
    "name": "deadlines",
    "kind": "deadlines",
    "url": "/api/v2/tasks/deadlines",
    "format": "json",
    "items_path": "",
    "fields": {
        "externalId": "/taskId",
        "title": "/name",
        "dueAt": {"pointer": "/due", "transform": "unixSeconds"},
    },
}


async def make_source(sm: Sm, account: Account, *, url: str, auth_ref: str | None = None) -> Any:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Кабинет",
            entry_url=url,
            kinds=["site", "schedule"],
            auth_ref=auth_ref,
        )
        await session.commit()
        return source


async def attach_playbook(
    sm: Sm, account: Account, source: Any, *, spec: dict[str, Any], language: str = "declarative"
) -> None:
    async with sm() as session:
        playbook = await playbooks.create_playbook(
            session,
            user_id=account.user_id,
            title="Кабинет",
            university_id=account.university_id,
            language=language,
        )
        version = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=spec
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


def sync_runner(
    sm: Sm,
    account: Account,
    *,
    browser_factory: Any,
    vault: InMemoryVault,
    store: FileArtifactStore,
    secret_reader: Any = None,
) -> SourceRunner:
    return SourceRunner(
        sm(),
        user_id=account.user_id,
        artifact_store=store,
        secret_reader=secret_reader,
        session_reader=VaultSessionReader(vault, profiles=None),
        browser_factory=browser_factory,
        env="test",
    )


async def test_browser_playbook_reads_observed_json_api_without_dom(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_http: str,
    browser_factory: Any,
    artifact_store: FileArtifactStore,
    fakeuni_app: Any,
) -> None:
    client_app.reset()
    vault = InMemoryVault()
    scope = "client-session"
    origin_state = client_app.storage_state(fakeuni_http)
    await vault.put(account.user_id, scope, SecretKind.SESSION, json.dumps(origin_state))
    source = await make_source(
        tx_sessionmaker,
        account,
        url=f"{fakeuni_http}/client-cabinet/app",
        auth_ref=f"session:{scope}",
    )
    spec = {
        "version": 1,
        "format": "browser",
        "timeout_s": 30,
        "browser": {
            "baseUrl": f"{fakeuni_http}/",
            "steps": [
                {"action": "goto", "target": "client-cabinet/app"},
                {"action": "wait", "target": "#schedule li", "timeoutSeconds": 15},
            ],
        },
        "reads": [SCHEDULE_READ],
    }
    await attach_playbook(tx_sessionmaker, account, source, spec=spec)
    report = await sync_runner(
        tx_sessionmaker, account, browser_factory=browser_factory, vault=vault, store=artifact_store
    ).sync(source.id)
    assert report.ok, report.errors
    assert report.counts == {"schedule": 2}
    assert report.reads[0].items_seen == 2
    # The DOM of the shell has no data at all: only the JSON API can feed the record.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fakeuni_app), base_url="https://fakeuni.test"
    ) as client:
        shell = await client.get("/client-cabinet/app")
    html = shell.text
    assert "Теория вероятностей" not in html
    # Network-first: the page asked once, the runner parsed that answer instead of fetching again.
    assert client_app.STATE.schedule_reads == 1
    assert report.notes
    assert any("JSON" in note for note in report.notes)

    # The observed endpoint is kept in the state and exposed in the report (query-stripped).
    assert any(
        endpoint.get("observed") is True
        and endpoint["url"].endswith("/client-cabinet/api/schedule")
        for endpoint in report.discovered_endpoints
    )
    async with tx_sessionmaker() as session:
        state_row = await state.get_source_state(
            session, user_id=account.user_id, source_id=source.id
        )
    assert state_row is not None
    assert any(endpoint.get("observed") is True for endpoint in state_row.endpoints)

    # Artifacts: the page snapshot and the JSON answer, content-addressed and stored on disk.
    async with tx_sessionmaker() as session:
        from botai.sources.v4 import artifacts as artifacts_service

        rows = await artifacts_service.list_artifacts(
            session, user_id=account.user_id, source_id=source.id
        )
    assert {row.kind for row in rows} >= {"page", "json"}
    assert all(row.storage_ref for row in rows)
    assert all(artifact_store.get(row.storage_ref or "").__class__ is bytes for row in rows)

    # The page's bearer never reaches the report.
    serialized = report.model_dump_json()
    assert client_app.CLIENT_TOKEN not in serialized

    # The domain got the classes with the observed API as their provenance.
    async with tx_sessionmaker() as session:
        events = (
            await session.scalars(select(ScheduleEvent).where(ScheduleEvent.source_id == source.id))
        ).all()
    assert {event.external_id for event in events} == {"cc-1", "cc-2"}
    assert all(event.connection_id is None for event in events)
    assert all("/client-cabinet/api/schedule" in (event.source_url or "") for event in events)
    client_app.reset()


async def test_browser_playbook_types_a_vault_secret_and_reads_the_cabinet(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_httpx_localhost: str,
    browser_factory: Any,
    artifact_store: FileArtifactStore,
) -> None:
    fakeuni_login.ATTEMPTS.clear()
    vault = InMemoryVault()
    scope = "client-password"
    await vault.put(account.user_id, scope, SecretKind.PASSWORD, data.PASSWORD)
    base = fakeuni_httpx_localhost
    source = await make_source(tx_sessionmaker, account, url=f"{base}/plain/login")
    spec = {
        "version": 1,
        "format": "browser",
        "timeout_s": 30,
        "browser": {
            "baseUrl": f"{base}/",
            "steps": [
                {"action": "goto", "target": "plain/login"},
                {"action": "type", "target": "#u", "value": data.USERNAME},
                {"action": "type", "target": "#p", "secretHandle": f"password:{scope}"},
                {"action": "click", "target": "button"},
                {"action": "wait", "value": "networkidle", "timeoutSeconds": 15},
            ],
        },
        "reads": [TIMETABLE_READ, DEADLINES_READ],
    }
    await attach_playbook(tx_sessionmaker, account, source, spec=spec)
    report = await sync_runner(
        tx_sessionmaker,
        account,
        browser_factory=browser_factory,
        vault=vault,
        store=artifact_store,
        secret_reader=VaultSecretReader(vault, user_id=account.user_id),
    ).sync(source.id)
    assert report.ok, report.errors
    assert report.counts == {"schedule": 4, "deadlines": 2}
    # The primary secret was typed, not reported.
    serialized = report.model_dump_json()
    assert data.PASSWORD not in serialized
    assert data.OTP not in serialized
    # The login really happened: the cabinet answered its own APIs and no attempt was refused.
    assert fakeuni_login.ATTEMPTS == []
    async with tx_sessionmaker() as session:
        events = await session.scalar(
            select(ScheduleEvent.id).where(ScheduleEvent.source_id == source.id).limit(1)
        )
    assert events is not None
