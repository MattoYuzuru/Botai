"""`code_exec` and the secret broker tools: executor limits, handles, masking (no real network)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from botai.agent.v4.executor import ExecLimits, LocalExecutor, Workspace
from botai.agent.v4.protocol import AskAnswer, AskQuestion, ToolContext
from botai.agent.v4.tools.code import CodeExecTool
from botai.agent.v4.tools.secrets import SecretBroker, SecretRequestTool, SecretUseTool
from botai.core.vault import InMemoryVault

NODE = shutil.which("node")
SECRET = "sk-live-FIXTURE-secret-value"


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "agent-workspace")


def context(workspace: Workspace) -> ToolContext:
    return ToolContext(run_id="run-1", workspace=workspace.root)


def broker(*, vault: InMemoryVault | None = None) -> SecretBroker:
    return SecretBroker(vault or InMemoryVault(), user_id="u1", scope_id="job-1")


# --- code_exec ---------------------------------------------------------------------------------


async def test_code_exec_runs_python_in_the_shared_workspace(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace)
    try:
        tool = CodeExecTool(executor)
        result = await tool.run(
            {
                "language": "python",
                "code": "open('parsed.json', 'w').write('{\"ok\": true}')",
            },
            context(workspace),
        )
    finally:
        executor.close()

    assert result.ok
    assert result.data["exit_code"] == 0
    assert result.data["files"] == ["parsed.json"]
    assert (workspace.root / "parsed.json").read_text() == '{"ok": true}'


async def test_code_exec_reports_a_nonzero_exit_code_as_data(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace)
    try:
        result = await CodeExecTool(executor).run(
            {"language": "python", "code": "raise SystemExit(7)"}, context(workspace)
        )
    finally:
        executor.close()

    assert result.ok  # the code ran; a nonzero exit is the program's result, not a tool failure
    assert result.data["exit_code"] == 7


async def test_code_exec_maps_javascript_to_the_executor(workspace: Workspace) -> None:
    if NODE is None:
        pytest.skip("node.js is not installed")
    executor = LocalExecutor(workspace=workspace)
    try:
        result = await CodeExecTool(executor).run(
            {"language": "javascript", "code": "console.log(6 * 7)"}, context(workspace)
        )
    finally:
        executor.close()

    assert result.ok
    assert result.data["stdout"] == "42\n"


async def test_code_exec_timeout_is_a_tool_failure_with_partial_data(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace, limits=ExecLimits(timeout_s=0.4))
    try:
        result = await CodeExecTool(executor).run(
            {"language": "bash", "code": "sleep 9971"}, context(workspace)
        )
    finally:
        executor.close()

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.data["timed_out"] is True


async def test_code_exec_refuses_a_different_workspace(
    workspace: Workspace, tmp_path: Path
) -> None:
    executor = LocalExecutor(workspace=workspace)
    try:
        other = Workspace.create(tmp_path / "other")
        result = await CodeExecTool(executor).run(
            {"language": "python", "code": "print(1)"}, context(other)
        )
        other.cleanup()
    finally:
        executor.close()

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "workspace_mismatch"


async def test_code_exec_without_a_runtime_is_a_clean_failure(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace, node="/nonexistent/node-fixture")
    try:
        result = await CodeExecTool(executor).run(
            {"language": "javascript", "code": "console.log(1)"}, context(workspace)
        )
    finally:
        executor.close()

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "executor_unavailable"


# --- secret_request ----------------------------------------------------------------------------

SECRET_QUESTION = "Пароль от кабинета"


async def test_secret_request_returns_only_a_handle(workspace: Workspace) -> None:
    secrets = broker()
    asked: list[AskQuestion] = []

    async def ask(question: AskQuestion) -> AskAnswer:
        asked.append(question)
        return AskAnswer(text=SECRET)

    tool = SecretRequestTool(secrets)
    result = await tool.run({"question": SECRET_QUESTION}, ToolContext(run_id="r", ask=ask))

    assert result.ok
    handle = result.data["handle"]
    assert handle.startswith("sec_")
    assert SECRET not in json.dumps(result.to_wire())
    assert asked
    assert asked[0].input_kind == "secret"
    assert await secrets.resolve(handle) == SECRET


async def test_secret_request_without_a_channel_fails() -> None:
    result = await SecretRequestTool(broker()).run(
        {"question": SECRET_QUESTION}, ToolContext(run_id="r")
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "no_ask_channel"


async def test_secret_request_cancelled_is_reported() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        return AskAnswer(cancelled=True)

    result = await SecretRequestTool(broker()).run(
        {"question": SECRET_QUESTION}, ToolContext(run_id="r", ask=ask)
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "cancelled"


# --- secret_use over HTTP (MockTransport, no network) ------------------------------------------


async def test_secret_use_puts_the_value_in_a_header_and_masks_the_echo() -> None:
    secrets = broker()
    handle = await secrets.issue(SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"echo": request.headers.get("authorization")})

    tool = SecretUseTool(secrets, transport=httpx.MockTransport(handler))
    result = await tool.run(
        {
            "handle": handle,
            "kind": "http",
            "method": "GET",
            "url": "https://fixture.invalid/api/me",
            "placement": "header",
            "name": "Authorization",
            "format": "Bearer {secret}",
        },
        ToolContext(run_id="r"),
    )

    assert result.ok
    assert seen
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    encoded = json.dumps(result.to_wire())
    assert SECRET not in encoded
    assert "***" in result.data["text"]


async def test_secret_use_puts_the_value_in_a_json_body() -> None:
    secrets = broker()
    handle = await secrets.issue(SECRET)
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"ok": True})

    tool = SecretUseTool(secrets, transport=httpx.MockTransport(handler))
    result = await tool.run(
        {
            "handle": handle,
            "kind": "http",
            "method": "POST",
            "url": "https://fixture.invalid/api/login",
            "placement": "json",
            "name": "password",
            "body": {"login": "student"},
        },
        ToolContext(run_id="r"),
    )

    assert result.ok
    payload = json.loads(bodies[0])
    assert payload == {"login": "student", "password": SECRET}


async def test_secret_use_unknown_handle_is_refused() -> None:
    tool = SecretUseTool(broker())
    result = await tool.run(
        {"handle": "sec_01M68Z000000000000000000A0", "kind": "http", "url": "http://x.invalid/"},
        ToolContext(run_id="r"),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "unknown_handle"


async def test_secret_use_browser_field_without_a_browser_fails() -> None:
    secrets = broker()
    handle = await secrets.issue(SECRET)
    result = await SecretUseTool(secrets).run(
        {"handle": handle, "kind": "browser_field", "target": {"selector": "#pass"}},
        ToolContext(run_id="r"),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "no_browser"


async def test_masker_replaces_every_known_value() -> None:
    secrets = broker()
    handle = await secrets.issue(SECRET)
    assert handle
    masked = secrets.mask(f"a {SECRET} b {SECRET} c")
    assert SECRET not in masked
    assert masked.count("***") == 2
    assert secrets.mask("short-ok") == "short-ok"
