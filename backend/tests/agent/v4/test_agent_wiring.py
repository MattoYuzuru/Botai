"""Wiring of the v4 harness: limits mapping, the prod guard, tool registry and the job entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from botai.agent.v4.factory import (
    AgentWiringError,
    build_agent_wiring,
    build_executor,
    enqueue_agent_run,
    memory_trace_store,
    run_limits_from_settings,
    workspace_for,
)
from botai.agent.v4.handlers import handle_agent
from botai.agent.v4.protocol import ToolRegistry
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.tools.secrets import SecretBroker
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.vault import InMemoryVault, SecretKind
from tests.agent.v4.agent_wiring_support import fake_services, fixture_settings


def test_soft_limits_are_mapped_from_settings() -> None:
    settings = fixture_settings(
        agent_limits_steps=12,
        agent_limits_seconds=90.5,
        agent_limits_tokens=250_000,
        agent_limits_usd=1.25,
        agent_hard_stop_usd=3.0,
        agent_auto_continue=True,
    )
    limits = run_limits_from_settings(settings)

    assert limits.max_steps == 12
    assert limits.max_seconds == 90.5
    assert limits.max_tokens == 250_000
    assert limits.max_usd == 1.25
    assert limits.hard_stop_usd == 3.0
    assert limits.auto_continue is True


def test_unset_soft_limits_mean_no_limit() -> None:
    limits = run_limits_from_settings(fixture_settings())
    assert limits.max_steps is None
    assert limits.max_seconds is None
    assert limits.max_tokens is None
    assert limits.max_usd is None


def test_local_executor_is_refused_in_prod() -> None:
    workspace = workspace_for(None, "01M68Z000000000000000000A0")
    try:
        with pytest.raises(AgentWiringError, match="ENV=prod"):
            build_executor(fixture_settings(env="prod"), workspace)
    finally:
        workspace.cleanup()


async def test_wiring_itself_refuses_prod_before_starting_a_browser() -> None:
    services = fake_services(settings=fixture_settings(env="prod"))
    with pytest.raises(AgentWiringError, match="LocalExecutor запрещён"):
        await build_agent_wiring(
            services,
            user_id="u1",
            run_id="01M68Z000000000000000000A0",
            provider=MockProvider(),
            trace_store=memory_trace_store(),
            with_browser=True,
            workspace_root=Path("/tmp") / "botai-agent-v4-prod-guard-fixture",
        )


async def test_the_agent_toolset_contains_browser_code_and_secrets() -> None:
    services = fake_services()
    wiring = await build_agent_wiring(
        services,
        user_id="u1",
        run_id="01M68Z000000000000000000A0",
        provider=MockProvider(),
        trace_store=memory_trace_store(),
        with_browser=False,
    )
    try:
        names = {tool.spec.name for tool in wiring.tools}
        assert {
            "http_request",
            "fs_read",
            "fs_write",
            "fs_list",
            "ask_user",
            "trace_read",
            "code_exec",
            "secret_request",
            "secret_use",
        } <= names
        assert not any(name.startswith("browser_") for name in names)
        # The registry validates specs and schemas without a browser.
        registry = ToolRegistry(wiring.tools)
        assert registry.llm_tools()
        assert all(hasattr(tool, "spec") and hasattr(tool, "run") for tool in wiring.tools)
        assert wiring.broker is not None
        assert wiring.trace_store is not None
    finally:
        await wiring.aclose()


async def test_enqueue_agent_run_creates_an_agent_job() -> None:
    services = fake_services()
    job_id = await enqueue_agent_run(
        services,
        user_id="u1",
        task="открой кабинет",
        connection_id="c1",
        thread_id=None,
        workspace_root=Path("/tmp/ws"),
    )
    jobs = services.queue.jobs
    assert [job["id"] for job in jobs] == [job_id]
    assert jobs[0]["kind"] is JobKind.AGENT
    assert jobs[0]["payload"]["task"] == "открой кабинет"
    assert jobs[0]["payload"]["_workspace_root"] == "/tmp/ws"


async def test_workspace_directory_survives_between_calls(tmp_path: Path) -> None:
    first = workspace_for(tmp_path, "run-1")
    first.write_text("data.txt", "kept")
    second = workspace_for(tmp_path, "run-1")
    try:
        assert second.read_text("data.txt") == "kept"
    finally:
        first.cleanup()
        second.cleanup()


async def test_the_handler_refuses_while_the_engine_flag_is_v3() -> None:
    services = fake_services(settings=fixture_settings(agent_engine="v3"))
    ctx = InMemoryJobContext(user_id="u1")
    with pytest.raises(BotaiError) as refused:
        await handle_agent(ctx, services, {"task": "проверка"})
    assert refused.value.code is ErrorCode.VALIDATION
    assert "BOTAI_AGENT_ENGINE=v4" in refused.value.detail


async def test_the_handler_requires_a_task() -> None:
    services = fake_services()
    ctx = InMemoryJobContext(user_id="u1")
    with pytest.raises(BotaiError) as refused:
        await handle_agent(ctx, services, {"task": "  "})
    assert refused.value.code is ErrorCode.VALIDATION


async def test_secret_broker_stores_values_in_the_vault_and_hands_out_handles() -> None:
    vault = InMemoryVault()
    broker = SecretBroker(vault, user_id="u1", scope_id="job-1")
    handle = await broker.issue("P@ssw0rd-FIXTURE")

    assert handle.startswith("sec_")
    assert "P@ssw0rd-FIXTURE" not in handle
    assert broker.handles == (handle,)
    assert await broker.resolve(handle) == "P@ssw0rd-FIXTURE"
    assert broker.mask("token P@ssw0rd-FIXTURE end") == "token *** end"
    stored = await vault.get("u1", f"job-1:{handle}", SecretKind.TRANSIENT)
    assert stored is not None
    assert stored.get_secret_value() == "P@ssw0rd-FIXTURE"
    with pytest.raises(ValueError, match="invalid secret handle"):
        await broker.resolve("not-a-handle")
    with pytest.raises(ValueError, match="unknown secret handle"):
        await broker.resolve("sec_01M68Z000000000000000000A0")
