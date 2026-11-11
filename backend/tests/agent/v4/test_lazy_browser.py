"""The run's browser is launched on first use, not when the wiring is built (D-044 §5.4).

Production chat turns launched a Chromium context on every turn, also when the answer came from the
student's synced data. These tests drive the real wiring with a counting stand-in for the launch:
a run without browser tools never launches and has nothing to save; the first browser tool
launches once, with the saved agent session installed in that first context; cleanup saves and
closes only what was launched; a failed launch is a structured tool error, not a crashed run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from botai.agent.v4 import factory
from botai.agent.v4.factory import AgentWiring, build_agent_wiring, memory_trace_store
from botai.agent.v4.lazy_browser import LazyBrowser
from botai.agent.v4.protocol import ToolContext, ToolHandler
from botai.agent.v4.providers import MockProvider
from botai.browser.control import BrowserControl
from botai.core.vault import InMemoryVault, SecretKind
from botai.sources.v4.secrets import AgentSessionStore
from tests.agent.v4.agent_wiring_support import fake_services

SAVED = {
    "cookies": [
        {
            "name": "session",
            "value": "fixture-session",
            "domain": "lms.fakeuni.test",
            "path": "/",
        }
    ],
    "origins": [],
}


@dataclass
class Page:
    url: str
    title: str = "Кабинет"

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title}


@dataclass
class FakeControl:
    storage_state: Any
    visited: list[str] = field(default_factory=list[str])
    typed: list[str] = field(default_factory=list[str])
    closed: bool = False

    async def navigate(self, url: str, **kwargs: Any) -> Page:
        del kwargs
        self.visited.append(url)
        return Page(url)

    async def type(self, target: Any, text: str, **kwargs: Any) -> Page:
        del target, kwargs
        self.typed.append(text)
        return Page(self.visited[-1] if self.visited else "about:blank")

    async def export_storage_state(self, **kwargs: Any) -> Any:
        del kwargs
        return {
            "cookies": [*self.storage_state["cookies"], {**SAVED["cookies"][0], "name": "new"}],
            "origins": [],
        }

    def network_log(self) -> list[Any]:
        return []

    async def close(self) -> None:
        self.closed = True


class Launches:
    """Counts launches of the run's browser and remembers what each was given."""

    def __init__(self, *, fail: bool = False) -> None:
        self.controls: list[FakeControl] = []
        self.fail = fail

    async def __call__(self, services: Any, **kwargs: Any) -> BrowserControl:
        del services
        if self.fail:
            raise RuntimeError("no chromium here")
        control = FakeControl(storage_state=kwargs["storage_state"])
        self.controls.append(control)
        return control  # type: ignore[return-value]


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Launches:
    counter = Launches()
    monkeypatch.setattr(factory, "launch_browser_control", counter)
    return counter


async def _wiring(tmp_path: Path, vault: InMemoryVault) -> AgentWiring:
    return await build_agent_wiring(
        fake_services(vault=vault),
        user_id="owner",
        run_id="01M68Z000000000000000000L1",
        provider=MockProvider(),
        trace_store=memory_trace_store(),
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path,
    )


def _tool(wiring: AgentWiring, name: str) -> ToolHandler:
    return next(tool for tool in wiring.tools if tool.spec.name == name)


def _context() -> ToolContext:
    return ToolContext(run_id="run-1", user_id="owner")


async def _saved(vault: InMemoryVault) -> dict[str, Any]:
    secret = await vault.get("owner", AgentSessionStore.SCOPE, SecretKind.SESSION)
    return json.loads(secret.get_secret_value()) if secret else {}


async def _vault_with_session() -> InMemoryVault:
    vault = InMemoryVault()
    await vault.put("owner", AgentSessionStore.SCOPE, SecretKind.SESSION, json.dumps(SAVED))
    return vault


async def test_a_run_without_browser_tools_never_launches_and_saves_nothing(
    tmp_path: Path, launches: Launches
) -> None:
    vault = await _vault_with_session()
    wiring = await _wiring(tmp_path, vault)
    names = {tool.spec.name for tool in wiring.tools}
    assert "browser_navigate" in names, "the browser tools are offered"
    assert wiring.control is None
    await wiring.aclose()

    assert launches.controls == []
    assert await _saved(vault) == SAVED, "the saved session is untouched"


async def test_the_first_browser_tool_launches_once_with_the_saved_session(
    tmp_path: Path, launches: Launches
) -> None:
    vault = await _vault_with_session()
    wiring = await _wiring(tmp_path, vault)
    try:
        navigate = _tool(wiring, "browser_navigate")
        first = await navigate.run({"url": "https://lms.fakeuni.test/app"}, _context())
        second = await navigate.run({"url": "https://lms.fakeuni.test/tasks"}, _context())
        assert first.ok, first.error
        assert second.ok, second.error
        (control,) = launches.controls
        assert control.storage_state["cookies"] == SAVED["cookies"]
        assert control.visited == ["https://lms.fakeuni.test/app", "https://lms.fakeuni.test/tasks"]
        assert wiring.control is control  # type: ignore[comparison-overlap]
    finally:
        await wiring.aclose()

    assert control.closed
    names = {cookie["name"] for cookie in (await _saved(vault))["cookies"]}
    assert names == {"session", "new"}, "the run's session is saved back on close"


async def test_secret_use_in_a_browser_field_launches_on_demand(
    tmp_path: Path, launches: Launches
) -> None:
    wiring = await _wiring(tmp_path, InMemoryVault())
    try:
        assert wiring.broker is not None
        handle = await wiring.broker.issue("FIXTURE-pass-123")
        used = await _tool(wiring, "secret_use").run(
            {"handle": handle, "kind": "browser_field", "target": {"selector": "#pass"}},
            _context(),
        )
        assert used.ok, used.error
        (control,) = launches.controls
        assert control.typed == ["FIXTURE-pass-123"]
    finally:
        await wiring.aclose()


async def test_saving_a_source_session_from_an_unopened_browser_is_refused(
    tmp_path: Path, launches: Launches
) -> None:
    wiring = await _wiring(tmp_path, InMemoryVault())
    try:
        saved = await _tool(wiring, "source_session").run(
            {"source_id": "01M68Z000000000000000000S1", "action": "save"}, _context()
        )
        assert not saved.ok
        assert saved.error is not None
        assert saved.error.code == "browser_not_started"
        assert launches.controls == []
    finally:
        await wiring.aclose()


async def test_a_failed_launch_is_a_tool_error_and_cleanup_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(factory, "launch_browser_control", Launches(fail=True))
    wiring = await _wiring(tmp_path, InMemoryVault())
    result = await _tool(wiring, "browser_navigate").run(
        {"url": "https://lms.fakeuni.test/app"}, _context()
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "browser_unavailable"
    assert result.error.retryable
    assert "no chromium here" not in result.to_model_text()
    await wiring.aclose()
    assert wiring.control is None


async def test_concurrent_first_calls_launch_one_browser() -> None:
    import asyncio

    made: list[FakeControl] = []

    async def launch() -> BrowserControl:
        await asyncio.sleep(0.01)
        control = FakeControl(storage_state=None)
        made.append(control)
        return control  # type: ignore[return-value]

    lazy = LazyBrowser(launch)
    first, second = await asyncio.gather(lazy.get(), lazy.get())
    assert first is second
    assert len(made) == 1
    assert lazy.launches == 1
