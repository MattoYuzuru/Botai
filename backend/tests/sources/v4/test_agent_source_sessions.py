"""Agent login state survives a turn and is reusable by a source without embedded credentials."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from botai.agent.v4.factory import build_agent_wiring, build_source_toolkit, memory_trace_store
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.providers import MockProvider
from botai.browser.control import BrowserControl, ControlConfig
from botai.core.vault import InMemoryVault
from botai.sources.v4 import service
from botai.sources.v4.secrets import AgentSessionStore, VaultSessionReader
from tests.agent.v4.agent_wiring_support import fake_services
from tests.auth.support import Sm
from tests.sources.v4.support import Account


def cookie(name: str, value: str, domain: str = "fakeuni.test") -> dict[str, Any]:
    return {"name": name, "value": value, "domain": domain, "path": "/"}


class SessionBrowser:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {"cookies": [], "origins": []}
        self.closed = False
        self.entries: list[Any] = []

    def network_log(self) -> list[Any]:
        return self.entries

    async def export_storage_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    async def import_storage_state(self, state: Any) -> None:
        raise AssertionError("Session restore must not navigate before installing localStorage")

    async def new_context(self, *, storage_state: Any) -> None:
        self.state = copy.deepcopy(storage_state)

    async def close(self) -> None:
        self.closed = True


async def test_browser_auth_survives_new_run_and_is_isolated_by_user(tmp_path: Path) -> None:
    vault = InMemoryVault()
    services = fake_services(vault=vault)
    first = SessionBrowser()
    first_wiring = await build_agent_wiring(
        services,
        user_id="owner",
        run_id="first",
        control=cast(BrowserControl, first),
        provider=MockProvider(),
        workspace_root=tmp_path,
        trace_store=memory_trace_store(),
    )
    first.state = {"cookies": [cookie("session", "fixture-login")], "origins": []}
    await first_wiring.aclose()
    assert first.closed
    second = SessionBrowser()
    second_wiring = await build_agent_wiring(
        services,
        user_id="owner",
        run_id="second",
        control=cast(BrowserControl, second),
        provider=MockProvider(),
        workspace_root=tmp_path,
        trace_store=memory_trace_store(),
    )
    assert second.state["cookies"] == first.state["cookies"]
    await second_wiring.aclose()
    assert await AgentSessionStore(vault, user_id="friend").load() == {}


async def test_session_save_keeps_other_runs_origins_and_respects_logout() -> None:
    vault = InMemoryVault()
    first, other = (
        AgentSessionStore(vault, user_id="owner"),
        AgentSessionStore(vault, user_id="owner"),
    )
    await first.load()
    await first.save({"cookies": [cookie("session", "first")], "origins": []})
    await other.load()
    await other.save(
        {"cookies": [cookie("session", "first"), cookie("lms", "other", "lms.test")], "origins": []}
    )
    await first.save({"cookies": [], "origins": []})
    restored = await AgentSessionStore(vault, user_id="owner").load()
    assert restored["cookies"] == [cookie("lms", "other", "lms.test")]


@pytest.mark.db
async def test_source_session_save_load_and_new_runner_authenticate_without_cookie_arguments(
    tx_sessionmaker: Sm,
    account: Account,
    tmp_path: Path,
) -> None:
    vault = InMemoryVault()
    browser = SessionBrowser(
        {
            "cookies": [
                cookie("session", "source-fixture") | {"secure": True},
                cookie("private", "other-fixture", "other.test"),
            ],
            "origins": [],
        }
    )
    browser.entries = [
        SimpleNamespace(
            url="https://fakeuni.test/api/courses",
            request_headers={"Authorization": "Bearer fixture-observed-token"},
        )
    ]
    requests: list[httpx.Request] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if (
            request.url.host == "fakeuni.test"
            and request.headers.get("authorization") != "Bearer fixture-observed-token"
        ):
            return httpx.Response(401)
        return httpx.Response(200, json={"courses": ["Математика"]})

    transport = httpx.MockTransport(endpoint)
    services = fake_services(vault=vault, db=tx_sessionmaker)
    toolkit = build_source_toolkit(
        services,
        user_id=account.user_id,
        transport=transport,
        artifact_root=tmp_path / "artifacts",
        browser_control=cast(BrowserControl, browser),
    )
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="ЛК",
            entry_url="https://fakeuni.test/app",
        )
        await session.commit()
    tools = {tool.spec.name: tool for tool in toolkit.tools()}
    context = ToolContext(
        run_id="run", task="Подключи", user_id=account.user_id, workspace=tmp_path
    )
    saved = await tools["source_session"].run({"source_id": source.id, "action": "save"}, context)
    assert saved.ok
    assert "source-fixture" not in json.dumps(saved.to_wire())
    async with tx_sessionmaker() as session:
        updated = await service.get_source(session, user_id=account.user_id, source_id=source.id)
        assert updated.auth_ref == f"session:{source.id}"
        state = await VaultSessionReader(vault)(updated)
        assert state is not None
        assert state["cookies"] == browser.state["cookies"]
        assert state["http_headers"][0]["authorization"] == "Bearer fixture-observed-token"
    # A new toolkit has no browser and no externally supplied cookies, as a later background job.
    fresh = build_source_toolkit(
        services,
        user_id=account.user_id,
        transport=transport,
        artifact_root=tmp_path / "next",
    )
    fresh_tools = {tool.spec.name: tool for tool in fresh.tools()}
    read = await fresh_tools["source_read_live"].run(
        {"source_id": source.id, "url": "https://fakeuni.test/api/courses"},
        context,
    )
    assert read.ok
    assert requests[-1].headers["cookie"] == "session=source-fixture"
    assert requests[-1].headers["authorization"] == "Bearer fixture-observed-token"
    other = await fresh_tools["source_read_live"].run(
        {"source_id": source.id, "url": "https://other.test/api/courses"},
        context,
    )
    assert other.ok
    assert "authorization" not in requests[-1].headers
    assert "source-fixture" not in requests[-1].headers.get("cookie", "")
    await fresh_tools["source_read_live"].run(
        {"source_id": source.id, "url": "http://fakeuni.test/api/courses"},
        context,
    )
    assert "authorization" not in requests[-1].headers
    assert "source-fixture" not in requests[-1].headers.get("cookie", "")
    browser.state = {"cookies": [], "origins": []}
    restored = await tools["source_session"].run(
        {"source_id": source.id, "action": "load"}, context
    )
    assert restored.ok
    assert browser.state["cookies"][0]["value"] == "source-fixture"


@pytest.mark.db
@pytest.mark.browser
async def test_real_browser_bearer_is_replayed_by_source_probe_and_sync(
    tx_sessionmaker: Sm,
    account: Account,
    tmp_path: Path,
) -> None:
    """HTTPS is intercepted in Chromium: no internet, real JS storage and request capture."""
    control = await BrowserControl.launch(config=ControlConfig(headless=True, sandbox=False))
    vault = InMemoryVault()
    services = fake_services(vault=vault, db=tx_sessionmaker)
    expected = {"items": [{"id": "course-1", "title": "Математика"}]}

    async def route(request_route: Any) -> None:
        if request_route.request.url.endswith("/api/courses"):
            authorized = (
                request_route.request.headers.get("authorization") == "Bearer browser-fixture"
            )
            await request_route.fulfill(
                status=200 if authorized else 401,
                content_type="application/json",
                body=json.dumps(expected),
            )
        else:
            await request_route.fulfill(
                content_type="text/html", body="<html><body>Кабинет</body></html>"
            )

    try:
        await control._page().route("https://fakeuni.test/**", route)
        await control.navigate("https://fakeuni.test/app")
        status = await control.eval_js("""async () => {
            localStorage.setItem('opaque_auth_key', 'browser-fixture');
            const response = await fetch('/api/courses', {
                headers: {Authorization: 'Bearer ' + localStorage.getItem('opaque_auth_key')}
            });
            await response.json();
            return response.status;
        }""")
        assert status == 200

        def endpoint(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer browser-fixture"
            return httpx.Response(200, json=expected)

        toolkit = build_source_toolkit(
            services,
            user_id=account.user_id,
            transport=httpx.MockTransport(endpoint),
            artifact_root=tmp_path / "artifacts",
            browser_control=control,
        )
        async with tx_sessionmaker() as session:
            source = await service.create_source(
                session,
                user_id=account.user_id,
                space_id=account.space_id,
                title="ЛК",
                entry_url="https://fakeuni.test/app",
            )
            await session.commit()
        tools = {tool.spec.name: tool for tool in toolkit.tools()}
        ctx = ToolContext(
            run_id="browser-run", task="Подключи", user_id=account.user_id, workspace=tmp_path
        )
        assert (
            await tools["source_session"].run({"source_id": source.id, "action": "save"}, ctx)
        ).ok
        spec = {
            "reads": [
                {
                    "name": "courses",
                    "kind": "courses",
                    "url": "/api/courses",
                    "items_path": "/items",
                    "fields": {"externalId": "/id", "title": "/title"},
                }
            ]
        }
        probe = await tools["source_probe"].run({"source_id": source.id, "spec": spec}, ctx)
        assert probe.ok
        assert probe.data["report"]["counts"] == {"courses": 1}
        update = await tools["source_update_playbook"].run(
            {"source_id": source.id, "spec": spec}, ctx
        )
        assert update.ok
        sync = await tools["source_sync"].run({"source_id": source.id}, ctx)
        assert sync.ok
        assert sync.data["report"]["counts"] == {"courses": 1}
        assert "browser-fixture" not in json.dumps(sync.to_wire())
    finally:
        await control.close()
