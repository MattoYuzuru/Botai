"""`source_*` agent tools: JSON specs, create/probe/playbook/sync/repair/read/disable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from botai.agent.v4.protocol import ToolContext, ToolHandler, ToolRegistry
from botai.agent.v4.tools.sources import SourceToolkit
from botai.sources.v4 import artifacts, service
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
SPEC: dict[str, Any] = {
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
            },
        }
    ],
    "timeout_s": 10,
}


def toolkit(
    tx_sessionmaker: Sm,
    transport: httpx.ASGITransport,
    store: Any,
    cookies: dict[str, str],
    tmp_path: Path,
) -> tuple[dict[str, ToolHandler], ToolContext]:
    source_tools = SourceToolkit(
        sm=tx_sessionmaker,
        transport=transport,
        artifact_store=store,
        cookies=cookies,
    )
    tools = {tool.spec.name: tool for tool in source_tools.tools()}
    registry = ToolRegistry(source_tools.tools())
    assert len(registry) == 8
    assert registry.validate("source_list", {}) == []
    context = ToolContext(run_id="run-1", task="test", user_id="", workspace=tmp_path)
    return tools, context


async def run_tool(
    tools: dict[str, ToolHandler],
    context: ToolContext,
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str,
) -> Any:
    bound = ToolContext(
        run_id=context.run_id,
        task=context.task,
        user_id=user_id,
        workspace=context.workspace,
    )
    return await tools[name].run(arguments, bound)


async def test_create_list_and_conflict(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
    tmp_path: Path,
) -> None:
    tools, context = toolkit(
        tx_sessionmaker, fakeuni_transport, artifact_store, fakeuni_cookies, tmp_path
    )
    created = await run_tool(
        tools,
        context,
        "source_create",
        {
            "space_id": account.space_id,
            "title": "ЛК Fakeuni",
            "url": f"{BASE}/app",
            "kinds": ["site", "schedule"],
        },
        user_id=account.user_id,
    )
    assert created.ok, created.error
    assert created.data["source"]["status"] == "draft"
    source_id = created.data["source"]["id"]
    duplicate = await run_tool(
        tools,
        context,
        "source_create",
        {"space_id": account.space_id, "title": "Дубль", "url": f"{BASE}/app"},
        user_id=account.user_id,
    )
    assert not duplicate.ok
    assert duplicate.error is not None
    assert duplicate.error.code == "conflict"
    listed = await run_tool(tools, context, "source_list", {}, user_id=account.user_id)
    assert listed.ok
    assert [row["id"] for row in listed.data["sources"]] == [source_id]


async def test_probe_playbook_sync_and_the_full_agent_flow(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
    tmp_path: Path,
) -> None:
    tools, context = toolkit(
        tx_sessionmaker, fakeuni_transport, artifact_store, fakeuni_cookies, tmp_path
    )
    created = await run_tool(
        tools,
        context,
        "source_create",
        {"space_id": account.space_id, "title": "ЛК", "url": f"{BASE}/app"},
        user_id=account.user_id,
    )
    source_id = created.data["source"]["id"]
    probe = await run_tool(
        tools,
        context,
        "source_probe",
        {"source_id": source_id, "spec": SPEC},
        user_id=account.user_id,
    )
    assert probe.ok, probe.error
    assert probe.data["report"]["counts"] == {"schedule": 4}
    assert probe.data["report"]["persisted"] is False

    update = await run_tool(
        tools,
        context,
        "source_update_playbook",
        {"source_id": source_id, "spec": SPEC, "activate": True},
        user_id=account.user_id,
    )
    assert update.ok, update.error
    assert update.data["version"] == 1
    assert update.data["activated"] is True

    sync = await run_tool(
        tools, context, "source_sync", {"source_id": source_id}, user_id=account.user_id
    )
    assert sync.ok, sync.error
    report = sync.data["report"]
    assert report["ok"] is True
    assert report["counts"] == {"schedule": 4}
    assert report["source_status"] == "active"
    token = next(iter(fakeuni_cookies.values()))
    assert token not in json.dumps(sync.data, ensure_ascii=False)
    async with tx_sessionmaker() as session:
        rows = await artifacts.list_artifacts(session, user_id=account.user_id, source_id=source_id)
        stored = await service.get_source(session, user_id=account.user_id, source_id=source_id)
    assert len(rows) == 1
    assert stored.status == "active"

    live = await run_tool(
        tools,
        context,
        "source_read_live",
        {"source_id": source_id, "url": f"{BASE}/api/v2/me"},
        user_id=account.user_id,
    )
    assert live.ok, live.error
    assert "Иванова" in live.data["text"]
    assert token not in json.dumps(live.data, ensure_ascii=False)

    disabled = await run_tool(
        tools, context, "source_disable", {"source_id": source_id}, user_id=account.user_id
    )
    assert disabled.ok
    assert disabled.data["source"]["status"] == "disabled"


async def test_repair_candidate_is_activated_only_after_the_canary(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
    tmp_path: Path,
) -> None:
    from tests.fakeuni import api as fakeuni_api

    tools, context = toolkit(
        tx_sessionmaker, fakeuni_transport, artifact_store, fakeuni_cookies, tmp_path
    )
    created = await run_tool(
        tools,
        context,
        "source_create",
        {"space_id": account.space_id, "title": "ЛК", "url": f"{BASE}/app"},
        user_id=account.user_id,
    )
    source_id = created.data["source"]["id"]
    await run_tool(
        tools,
        context,
        "source_update_playbook",
        {"source_id": source_id, "spec": SPEC, "activate": True},
        user_id=account.user_id,
    )
    fakeuni_api.STATE.timetable_version = "v3"
    failed = await run_tool(
        tools, context, "source_sync", {"source_id": source_id}, user_id=account.user_id
    )
    assert not failed.ok
    assert failed.error is not None
    assert failed.error.code == "read_failed"

    candidate = json.loads(json.dumps(SPEC))
    candidate["reads"][0]["url"] = f"{BASE}/api/v3/schedule"
    repaired = await run_tool(
        tools,
        context,
        "source_repair",
        {"source_id": source_id, "spec": candidate, "activate": True},
        user_id=account.user_id,
    )
    assert repaired.ok, repaired.error
    assert repaired.data["report"]["ok"] is True
    healed = await run_tool(
        tools, context, "source_sync", {"source_id": source_id}, user_id=account.user_id
    )
    assert healed.ok, healed.error
    assert healed.data["report"]["source_status"] == "active"


async def test_foreign_ids_look_missing(
    tx_sessionmaker: Sm,
    account: Account,
    second_account: Account,
    fakeuni_transport: httpx.ASGITransport,
    artifact_store: Any,
    tmp_path: Path,
) -> None:
    tools, context = toolkit(tx_sessionmaker, fakeuni_transport, artifact_store, {}, tmp_path)
    created = await run_tool(
        tools,
        context,
        "source_create",
        {"space_id": account.space_id, "title": "ЛК", "url": f"{BASE}/app"},
        user_id=account.user_id,
    )
    source_id = created.data["source"]["id"]
    foreign = await run_tool(
        tools,
        context,
        "source_sync",
        {"source_id": source_id},
        user_id=second_account.user_id,
    )
    assert not foreign.ok
    assert foreign.error is not None
    assert foreign.error.code == "not_found"
    foreign_list = await run_tool(tools, context, "source_list", {}, user_id=second_account.user_id)
    assert foreign_list.ok
    assert foreign_list.data["sources"] == []
