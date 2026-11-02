"""Protected calendar links work through the actual source tools and later syncs (D-008)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from botai.agent.v4.factory import VaultSecretReader
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.secrets import SecretBroker
from botai.agent.v4.tools.sources import SourceToolkit
from botai.core.vault import InMemoryVault, SecretKind
from botai.db.models import ScheduleEvent
from botai.sources.v4 import artifacts, service
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

FEED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:booking
DTSTART:20261102T100000Z
DTEND:20261102T110000Z
SUMMARY:Встреча
END:VEVENT
BEGIN:VEVENT
UID:available
DTSTART;VALUE=DATE:20261102
DTEND;VALUE=DATE:20261103
SUMMARY:Есть свободное время
END:VEVENT
END:VCALENDAR
"""
PARSER = """
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
records = []
for event in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", Path('reads/feed.txt').read_text(), re.S):
    fields = dict(line.split(':', 1) for line in event.strip().splitlines() if ':' in line)
    all_day = 'DTSTART;VALUE=DATE' in fields
    key = 'DTSTART;VALUE=DATE' if all_day else 'DTSTART'
    end = 'DTEND;VALUE=DATE' if all_day else 'DTEND'
    fmt = '%Y%m%d' if all_day else '%Y%m%dT%H%M%SZ'
    zone = ZoneInfo('Europe/Moscow' if all_day else 'UTC')
    records.append({'kind': 'schedule', 'external_id': fields['UID'], 'fields': {
        'title': fields['SUMMARY'],
        'startsAt': datetime.strptime(fields[key], fmt).replace(tzinfo=zone).isoformat(),
        'endsAt': datetime.strptime(fields[end], fmt).replace(tzinfo=zone).isoformat(),
        'attributes': [{'label': 'Весь день', 'value': 'Да'}] if all_day else [],
    }})
print(json.dumps({'records': records}))
"""


async def test_protected_links_import_two_calendars_and_resync_without_the_broker(
    tx_sessionmaker: Sm, account: Account, tmp_path: Path
) -> None:
    vault = InMemoryVault()
    broker = SecretBroker(vault, user_id=account.user_id, scope_id="calendar-run")
    links = [
        f"https://calendar.fixture.invalid/feed.ics?token=fixture-{name}" for name in ("one", "two")
    ]
    seen: list[str] = []

    def fetch(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert str(request.url) in links
        seen.append(str(request.url))
        # Providers sometimes echo their subscription URL in the exported body.
        return httpx.Response(200, text=FEED + f"\nX-SUBSCRIPTION:{request.url}\n")

    store = artifacts.FileArtifactStore(tmp_path / "artifacts")
    kwargs = dict(
        sm=tx_sessionmaker,
        vault=vault,
        secret_reader=VaultSecretReader(vault, user_id=account.user_id),
        transport=httpx.MockTransport(fetch),
        artifact_store=store,
    )
    toolkit = SourceToolkit(**kwargs, broker=broker)
    tools = {tool.spec.name: tool for tool in toolkit.tools()}
    context = ToolContext(run_id="calendar-run", user_id=account.user_id)
    source_ids: list[str] = []
    wire: list[object] = []
    for index, link in enumerate(links):
        handle = await broker.issue(link)
        created = await tools["source_create"].run(
            {
                "space_id": account.space_id,
                "title": f"Календарь {index}",
                "link": handle,
                "kinds": ["calendar", "schedule"],
            },
            context,
        )
        assert created.ok, created.error
        wire.append(created.to_wire())
        source = created.data["source"]
        assert source["entryUrl"] is None
        assert source["readUrl"] == "{secret}"
        source_ids.append(source["id"])
        saved = await vault.get(account.user_id, source["id"], SecretKind.ICAL_URL)
        assert saved
        assert saved.get_secret_value() == link
        live = await tools["source_read_live"].run(
            {"source_id": source["id"], "url": source["readUrl"]}, context
        )
        assert live.ok, live.error
        wire.append(live.to_wire())
        spec = {"version": 1, "reads": [{"name": "feed", "url": "{secret}", "format": "text"}]}
        updated = await tools["source_update_playbook"].run(
            {"source_id": source["id"], "spec": spec, "code": PARSER, "activate": True}, context
        )
        assert updated.ok, updated.error
        wire.append(updated.to_wire())
        imported = await tools["source_sync"].run({"source_id": source["id"]}, context)
        assert imported.ok, imported.error
        assert imported.data["report"]["counts"] == {"schedule": 2}
        wire.append(imported.to_wire())
    broker.clear()
    # Background sync and repair happen in a different run with no transient handles.
    tools = {tool.spec.name: tool for tool in SourceToolkit(**kwargs).tools()}
    for source_id in source_ids:
        repeated = await tools["source_sync"].run({"source_id": source_id}, context)
        assert repeated.ok, repeated.error
        wire.append(repeated.to_wire())
        repaired = await tools["source_repair"].run({"source_id": source_id, "version": 1}, context)
        assert repaired.ok, repaired.error
        wire.append(repaired.to_wire())
    async with tx_sessionmaker() as session:
        rows = list(
            await session.scalars(
                select(ScheduleEvent).where(ScheduleEvent.source_id.in_(source_ids))
            )
        )
        sources = await service.list_sources(session, user_id=account.user_id)
        assert len(sources) == 2
        assert sources[0].identity_key != sources[1].identity_key
        wire.extend(
            [
                {
                    "identity": source.identity_key,
                    "config": source.config,
                    "entry": source.entry_url,
                }
                for source in sources
            ]
        )
        for source_id in source_ids:
            wire.extend(
                [
                    row.metadata_json
                    for row in await artifacts.list_artifacts(
                        session, user_id=account.user_id, source_id=source_id
                    )
                ]
            )
    assert (
        len(rows) == 4
    )  # repeated sync updates each calendar's own UID, without cross-source dedupe
    assert {row.source_id for row in rows} == set(source_ids)
    all_day = [row for row in rows if row.external_id == "available"]
    assert len(all_day) == 2
    assert all(row.attributes == [{"label": "Весь день", "value": "Да"}] for row in all_day)
    assert all((row.ends_at - row.starts_at).days == 1 for row in all_day)
    encoded = json.dumps(wire, ensure_ascii=False)
    artifact_bytes = b"".join(
        path.read_bytes() for path in (tmp_path / "artifacts").rglob("*") if path.is_file()
    )
    for value in (*links, "fixture-one", "fixture-two"):
        assert value not in encoded
        assert value.encode() not in artifact_bytes
    assert set(seen) == set(links)


async def test_unknown_calendar_handle_does_not_create_an_empty_source(
    tx_sessionmaker: Sm, account: Account
) -> None:
    vault = InMemoryVault()
    toolkit = SourceToolkit(
        sm=tx_sessionmaker,
        vault=vault,
        broker=SecretBroker(vault, user_id=account.user_id, scope_id="other-run"),
    )
    create = next(tool for tool in toolkit.tools() if tool.spec.name == "source_create")
    result = await create.run(
        {
            "space_id": account.space_id,
            "title": "Календарь",
            "link": "sec_01M68Z000000000000000000A0",
        },
        ToolContext(run_id="r", user_id=account.user_id),
    )
    assert not result.ok
    assert result.error
    assert result.error.code == "unknown_handle"
    async with tx_sessionmaker() as session:
        assert await service.list_sources(session, user_id=account.user_id) == []


@pytest.mark.parametrize("failure", [False, True])
async def test_protected_url_is_hidden_in_non_utf8_artifacts_and_long_http_errors(
    tx_sessionmaker: Sm, account: Account, tmp_path: Path, failure: bool
) -> None:
    token = "fixturetoken" * 250
    link = f"https://calendar.fixture.invalid/private-fixturekey/feed.ics?token={token}"
    vault = InMemoryVault()
    broker = SecretBroker(vault, user_id=account.user_id, scope_id="r")
    handle = await broker.issue(link)
    original = b"SUMMARY:caf\xe9\nX-SUBSCRIPTION:" + link.encode()

    def fetch(request: httpx.Request) -> httpx.Response:
        if failure:
            raise httpx.ConnectError("HTTP failed " + str(request.url), request=request)
        return httpx.Response(
            200, content=original, headers={"content-type": "text/calendar; charset=latin-1"}
        )

    store = artifacts.FileArtifactStore(tmp_path / "artifacts")
    toolkit = SourceToolkit(
        sm=tx_sessionmaker,
        vault=vault,
        broker=broker,
        secret_reader=VaultSecretReader(vault, user_id=account.user_id),
        transport=httpx.MockTransport(fetch),
        artifact_store=store,
    )
    tools = {tool.spec.name: tool for tool in toolkit.tools()}
    context = ToolContext(run_id="r", user_id=account.user_id)
    created = await tools["source_create"].run(
        {"space_id": account.space_id, "title": "Календарь", "link": handle}, context
    )
    assert created.ok, created.error
    source_id = created.data["source"]["id"]
    result = await tools["source_read_live"].run(
        {"source_id": source_id, "url": "{secret}"}, context
    )
    assert result.ok is (not failure)
    wire = json.dumps(result.to_wire())
    assert "fixturetoken" not in wire
    assert "fixturekey" not in wire
    if not failure:
        stored = b"".join(path.read_bytes() for path in store.root.rglob("*") if path.is_file())
        assert stored == b"SUMMARY:caf\xe9\nX-SUBSCRIPTION:***"
    else:
        spec = {"version": 1, "reads": [{"name": "feed", "url": "{secret}", "format": "text"}]}
        probe = await tools["source_probe"].run({"source_id": source_id, "spec": spec}, context)
        assert not probe.ok
        assert "fixturetoken" not in json.dumps(probe.to_wire())
