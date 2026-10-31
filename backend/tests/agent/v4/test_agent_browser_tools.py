"""`browser_*` tools on a real Chromium against a loopback cabinet (marker `browser`).

The tools wrap `BrowserControl`; here we check the runtime-facing contract: data in results,
files inside the workspace only, credential headers masked in network/HAR output, cookies as
session material, and session export/import.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.browser import build_browser_tools
from botai.agent.v4.tools.secrets import SecretBroker, build_secret_tools
from botai.browser.control import BrowserControl, ControlConfig
from botai.core.vault import InMemoryVault
from tests.agent.v4.agent_wiring_support import SECRET_MARKER, serve_agent_site

pytestmark = pytest.mark.browser


@pytest.fixture
async def site() -> AsyncIterator[str]:
    async with serve_agent_site() as base:
        yield base


@pytest.fixture
async def control(workspace: Path) -> AsyncIterator[BrowserControl]:
    browser = await BrowserControl.launch(
        config=ControlConfig(headless=True, sandbox=False, upload_root=workspace)
    )
    try:
        yield browser
    finally:
        await browser.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    root.mkdir()
    return root


@pytest.fixture
def tools(control: BrowserControl, workspace: Path) -> dict[str, Any]:
    return {tool.spec.name: tool for tool in build_browser_tools(control, workspace=workspace)}


def context(workspace: Path) -> ToolContext:
    return ToolContext(run_id="run-1", workspace=workspace)


async def test_navigate_read_eval_and_screenshot(
    tools: dict[str, Any], site: str, workspace: Path
) -> None:
    ctx = context(workspace)
    navigated = await tools["browser_navigate"].run({"url": f"{site}/cabinet"}, ctx)
    assert navigated.ok
    assert navigated.data["url"] == f"{site}/cabinet"

    read = await tools["browser_read"].run({"mode": "text"}, ctx)
    assert read.ok
    assert "Математический анализ" in read.data["text"]

    evaluated = await tools["browser_eval"].run(
        {"expression": "() => document.querySelectorAll('table tr').length"}, ctx
    )
    assert evaluated.ok
    assert evaluated.data == 3

    shot = await tools["browser_screenshot"].run({"name": "cabinet"}, ctx)
    assert shot.ok
    relative = shot.data["path"]
    assert relative.startswith("screenshots/")
    assert (workspace / relative).is_file()
    assert shot.artifacts
    assert shot.artifacts[0].sha256 == shot.data["sha256"]


async def test_typing_a_secret_masks_it_everywhere(
    control: BrowserControl, tools: dict[str, Any], site: str, workspace: Path
) -> None:
    broker = SecretBroker(InMemoryVault(), user_id="u1", scope_id="job-1")
    masked_browser = build_browser_tools(control, workspace=workspace, mask=broker.mask)
    secret_tools = {
        tool.spec.name: tool
        for tool in (*masked_browser, *build_secret_tools(broker, control=control))
    }
    handle = await broker.issue(SECRET_MARKER)
    ctx = ToolContext(run_id="run-1", workspace=workspace)
    assert (await tools["browser_navigate"].run({"url": f"{site}/form"}, ctx)).ok
    typed = await secret_tools["browser_type"].run(
        {"target": {"selector": "#login"}, "text": "student"}, ctx
    )
    assert typed.ok
    used = await secret_tools["secret_use"].run(
        {"handle": handle, "kind": "browser_field", "target": {"selector": "#pass"}}, ctx
    )
    assert used.ok
    assert SECRET_MARKER not in json.dumps(used.to_wire())
    clicked = await secret_tools["browser_click"].run({"target": {"selector": "#send"}}, ctx)
    assert clicked.ok
    read = await secret_tools["browser_read"].run({"mode": "text"}, ctx)
    assert read.ok
    assert SECRET_MARKER not in read.data["text"]
    assert str(len(SECRET_MARKER)) in read.data["text"]  # the page shows the password length

    echoed = await secret_tools["browser_navigate"].run({"url": f"{site}/echo-secret"}, ctx)
    assert echoed.ok
    masked = await secret_tools["browser_read"].run({"mode": "text"}, ctx)
    assert masked.ok
    assert "***" in masked.data["text"]


async def test_cookies_storage_and_masked_network_headers(
    tools: dict[str, Any], site: str, workspace: Path
) -> None:
    ctx = context(workspace)
    assert (await tools["browser_navigate"].run({"url": f"{site}/state"}, ctx)).ok
    await tools["browser_wait"].run({"kind": "timeout", "value": 0.1}, ctx)

    cookies = await tools["browser_cookies"].run({"action": "list"}, ctx)
    assert cookies.ok
    names = {row["name"] for row in cookies.data["cookies"]}
    assert "lab" in names

    storage = await tools["browser_storage"].run({"action": "get", "kind": "local"}, ctx)
    assert storage.ok
    assert storage.data["items"].get("token") == "local-fixture"
    written = await tools["browser_storage"].run(
        {"action": "set", "kind": "local", "data": {"extra": "value"}}, ctx
    )
    assert written.ok

    # A fetch with an explicit Authorization header gives the network layer a credential header
    # to mask (Playwright does not expose Cookie values in request headers).
    fetched = await tools["browser_eval"].run(
        {
            "expression": """async () => (await fetch('/whoami', {
                headers: {'Authorization': 'Bearer fixture-token'}
            })).status""",
        },
        ctx,
    )
    assert fetched.ok
    log = await tools["browser_network"].run({"action": "log", "max_entries": 20}, ctx)
    assert log.ok
    whoami = [row for row in log.data["entries"] if row["url"].endswith("/whoami")]
    assert whoami
    authorization = [
        row["request_headers"].get("authorization")
        for row in whoami
        if "authorization" in {name.lower() for name in row["request_headers"]}
    ]
    assert authorization
    assert set(authorization) == {"***"}
    assert "fixture-token" not in json.dumps(log.to_wire())


async def test_har_is_a_workspace_file_with_masked_credentials(
    tools: dict[str, Any], site: str, workspace: Path
) -> None:
    ctx = context(workspace)
    assert (await tools["browser_navigate"].run({"url": f"{site}/state"}, ctx)).ok
    assert (await tools["browser_network"].run({"action": "har_start"}, ctx)).ok
    fetched = await tools["browser_eval"].run(
        {
            "expression": """async () => (await fetch('/whoami', {
                headers: {'Authorization': 'Bearer fixture-token'}
            })).status""",
        },
        ctx,
    )
    assert fetched.ok
    stopped = await tools["browser_network"].run({"action": "har_stop", "name": "run"}, ctx)
    assert stopped.ok
    relative = stopped.data["path"]
    assert relative.startswith("browser/")
    document = json.loads((workspace / relative).read_text(encoding="utf-8"))
    entries = document["log"]["entries"]
    assert entries
    authorization_values = [
        header["value"]
        for entry in entries
        for header in entry["request"]["headers"]
        if header["name"].lower() == "authorization"
    ]
    assert authorization_values
    assert set(authorization_values) == {"***"}
    assert "fixture-token" not in (workspace / relative).read_text(encoding="utf-8")


async def test_tabs_list_new_switch_close(
    tools: dict[str, Any], site: str, workspace: Path
) -> None:
    ctx = context(workspace)
    await tools["browser_navigate"].run({"url": f"{site}/cabinet"}, ctx)
    opened = await tools["browser_open"].run({"url": f"{site}/", "new_tab": True}, ctx)
    assert opened.ok
    tabs = await tools["browser_tabs"].run({"action": "list"}, ctx)
    assert tabs.ok
    assert len(tabs.data["tabs"]) == 2
    first = tabs.data["tabs"][0]["tab_id"]
    switched = await tools["browser_tabs"].run({"action": "switch", "tab_id": first}, ctx)
    assert switched.ok
    closed = await tools["browser_tabs"].run({"action": "close", "tab_id": first}, ctx)
    assert closed.ok
    remaining = await tools["browser_tabs"].run({"action": "list"}, ctx)
    assert len(remaining.data["tabs"]) == 1


async def test_download_and_upload_stay_inside_the_workspace(
    tools: dict[str, Any], site: str, workspace: Path
) -> None:
    ctx = context(workspace)
    assert (await tools["browser_navigate"].run({"url": f"{site}/download-link"}, ctx)).ok
    downloaded = await tools["browser_download"].run({"target": {"selector": "#dl"}}, ctx)
    assert downloaded.ok
    relative = downloaded.data["path"]
    assert relative.startswith("downloads/")
    assert (workspace / relative).is_file()
    assert (workspace / relative).read_bytes() == b"schedule-report-bytes"

    upload = workspace / "upload.txt"
    upload.write_text("payload-for-upload", encoding="utf-8")
    assert (await tools["browser_navigate"].run({"url": f"{site}/upload-form"}, ctx)).ok
    assert (
        await tools["browser_upload"].run(
            {"target": {"selector": "#file"}, "paths": ["upload.txt"]}, ctx
        )
    ).ok
    assert (await tools["browser_click"].run({"target": {"selector": "#send"}}, ctx)).ok
    read = await tools["browser_read"].run({"mode": "text"}, ctx)
    assert read.ok
    assert str(len("payload-for-upload")) in read.data["text"]

    escaped = await tools["browser_upload"].run(
        {"target": {"selector": "#file"}, "paths": ["../outside.txt"]}, ctx
    )
    assert not escaped.ok


async def test_session_export_and_import(tools: dict[str, Any], site: str, workspace: Path) -> None:
    ctx = context(workspace)
    assert (await tools["browser_navigate"].run({"url": f"{site}/state"}, ctx)).ok
    exported = await tools["browser_session"].run({"action": "export", "name": "task"}, ctx)
    assert exported.ok
    assert exported.data["cookies"] >= 1
    path = exported.data["path"]
    assert path == "sessions/task.json"
    saved = json.loads((workspace / path).read_text(encoding="utf-8"))
    assert any(cookie["name"] == "lab" for cookie in saved["cookies"])

    imported = await tools["browser_session"].run({"action": "import", "path": path}, ctx)
    assert imported.ok
    assert imported.data["cookies_added"] >= 1
