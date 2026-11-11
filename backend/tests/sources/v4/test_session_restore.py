"""Restoring a session must install origin storage before any authentication redirect runs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from botai.agent.v4.factory import build_agent_wiring, memory_trace_store
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.tools.sources import SourceToolkit
from botai.browser.control import BrowserControl, ControlConfig
from botai.core.vault import InMemoryVault, SecretKind
from botai.sources.v4 import service
from botai.sources.v4.secrets import AgentSessionStore, browser_storage_state
from tests.agent.v4.agent_wiring_support import fake_services
from tests.auth.support import Sm
from tests.sources.v4.conftest import _serve
from tests.sources.v4.support import Account


@pytest.fixture
async def redirect_site() -> AsyncIterator[tuple[str, list[tuple[str, str]]]]:
    app = FastAPI()
    requests: list[tuple[str, str]] = []

    @app.get("/{path:path}")
    async def page(request: Request, path: str) -> Any:
        requests.append((str(request.url.hostname), request.url.path))
        if path == "api/data":
            authorized = request.headers.get("authorization") == "Bearer restore-fixture"
            return JSONResponse({"ok": authorized}, status_code=200 if authorized else 401)
        if path == "saml":
            return HTMLResponse("<html><body>Вход</body></html>")
        return HTMLResponse(
            "<html><body><script>"
            "const token = localStorage.getItem('opaque_auth_key');"
            "if (!token) { location.replace('http://localhost:"
            + str(request.url.port)
            + "/saml'); } else {"
            "window.ready = fetch('/api/data', {headers: {Authorization: 'Bearer ' + token}})"
            ".then(response => response.status); }"
            "</script></body></html>"
        )

    async with _serve(app, "127.0.0.1") as port:
        yield f"http://127.0.0.1:{port}", requests


def session_state(origin: str) -> dict[str, Any]:
    return {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [{"name": "opaque_auth_key", "value": "restore-fixture"}],
            }
        ],
        "http_headers": [{"origin": origin, "authorization": "Bearer restore-fixture"}],
    }


def test_native_state_preserves_nested_storage_without_http_metadata() -> None:
    state = session_state("https://fakeuni.test")
    state["origins"][0]["indexedDB"] = [{"name": "fixture-db", "version": 1, "stores": []}]
    native = browser_storage_state(state)
    assert set(native) == {"cookies", "origins"}
    assert native["origins"] == state["origins"]


@pytest.mark.browser
@pytest.mark.parametrize("injected", [False, True])
async def test_agent_restore_does_not_visit_origins_and_authorizes_first_request(
    redirect_site: tuple[str, list[tuple[str, str]]],
    tmp_path: Path,
    injected: bool,
) -> None:
    origin, requests = redirect_site
    vault = InMemoryVault()
    await vault.put(
        "owner", AgentSessionStore.SCOPE, SecretKind.SESSION, json.dumps(session_state(origin))
    )
    control = (
        await BrowserControl.launch(config=ControlConfig(headless=True, sandbox=False))
        if injected
        else None
    )
    wiring = await build_agent_wiring(
        fake_services(vault=vault),
        user_id="owner",
        run_id=f"restore-{injected}",
        control=control,
        provider=MockProvider(),
        workspace_root=tmp_path,
        trace_store=memory_trace_store(),
    )
    try:
        assert requests == []  # native restore must make zero origin HTTP requests
        # The browser is lazy (D-044): opening it is when the saved session is installed.
        browser = await wiring.open_browser()
        assert requests == []
        await browser.navigate(origin + "/app")
        assert await browser.eval_js("async () => await window.ready") == 200
        assert requests == [("127.0.0.1", "/app"), ("127.0.0.1", "/api/data")]
        state = await browser.export_storage_state()
        assert [item["origin"] for item in state["origins"]] == [origin]
    finally:
        await wiring.aclose()


@pytest.mark.db
@pytest.mark.browser
async def test_source_session_load_installs_storage_before_any_navigation(
    tx_sessionmaker: Sm,
    account: Account,
    tmp_path: Path,
    redirect_site: tuple[str, list[tuple[str, str]]],
) -> None:
    origin, requests = redirect_site
    vault = InMemoryVault()
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Кабинет",
            entry_url=origin + "/app",
            auth_ref="session:restore-source",
        )
        await session.commit()
    await vault.put(
        account.user_id, "restore-source", SecretKind.SESSION, json.dumps(session_state(origin))
    )
    control = await BrowserControl.launch(config=ControlConfig(headless=True, sandbox=False))
    try:
        toolkit = SourceToolkit(sm=tx_sessionmaker, vault=vault, browser_control=control)
        tools = {tool.spec.name: tool for tool in toolkit.tools()}
        ctx = ToolContext(
            run_id="restore-source",
            task="Восстанови вход",
            user_id=account.user_id,
            workspace=tmp_path,
        )
        loaded = await tools["source_session"].run({"source_id": source.id, "action": "load"}, ctx)
        assert loaded.ok
        assert requests == []
        await control.navigate(origin + "/app")
        assert await control.eval_js("async () => await window.ready") == 200
        assert requests == [("127.0.0.1", "/app"), ("127.0.0.1", "/api/data")]
        state = await control.export_storage_state()
        assert [item["origin"] for item in state["origins"]] == [origin]
    finally:
        await control.close()
