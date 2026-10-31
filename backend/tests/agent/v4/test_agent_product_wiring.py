"""Product tools of the v4 engine: `source_*`/`rag_*` are part of a run and never quietly absent.

The v4 engine is the product engine behind `BOTAI_AGENT_ENGINE=v4`: a run there gets the source
tools over the real DB/artifact/Vault/executor collaborators and the RAG tools over the real
service. These tests drive the wiring itself (no browser, no network): what is registered, what is
refused and the one convention that lets the runner authenticate a source — `auth_ref` as a Vault
handle `kind:scope_id`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from botai.agent.v4.factory import (
    DEFAULT_ARTIFACT_ROOT,
    AgentWiringError,
    VaultSecretReader,
    artifact_root_from_settings,
    build_agent_wiring,
    build_source_toolkit,
    memory_trace_store,
)
from botai.agent.v4.providers import MockProvider
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import InMemoryVault, SecretKind
from tests.agent.v4.agent_wiring_support import fake_services, fixture_settings

SOURCE_TOOLS = {
    "source_list",
    "source_create",
    "source_probe",
    "source_sync",
    "source_repair",
    "source_update_playbook",
    "source_disable",
    "source_read_live",
}
RAG_TOOLS = {"rag_search", "rag_ingest"}


def _wiring_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "provider": MockProvider(),
        "trace_store": memory_trace_store(),
        "with_browser": False,
        "artifact_root": tmp_path / "artifacts",
    }


# --- registration -------------------------------------------------------------------------------


async def test_v4_run_gets_the_source_tools_over_the_real_collaborators(tmp_path: Path) -> None:
    services = fake_services()
    wiring = await build_agent_wiring(
        services, user_id="u1", run_id="01M68Z000000000000000000A0", **_wiring_kwargs(tmp_path)
    )
    try:
        names = {tool.spec.name for tool in wiring.tools}
        assert names >= SOURCE_TOOLS
        # RAG is an explicit feature flag; without it the tools are not registered.
        assert not (RAG_TOOLS & names)
        assert wiring.source_toolkit is not None
    finally:
        await wiring.aclose()


async def test_rag_tools_are_registered_when_the_flag_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTAI_RAG_ENABLED", "1")
    services = fake_services()
    wiring = await build_agent_wiring(
        services, user_id="u1", run_id="01M68Z000000000000000000A0", **_wiring_kwargs(tmp_path)
    )
    try:
        names = {tool.spec.name for tool in wiring.tools}
        assert names >= RAG_TOOLS
    finally:
        await wiring.aclose()


async def test_the_v3_flag_keeps_the_old_toolset(tmp_path: Path) -> None:
    services = fake_services(settings=fixture_settings(agent_engine="v3"))
    wiring = await build_agent_wiring(
        services, user_id="u1", run_id="01M68Z000000000000000000A0", **_wiring_kwargs(tmp_path)
    )
    try:
        names = {tool.spec.name for tool in wiring.tools}
        assert not (SOURCE_TOOLS & names)
        assert wiring.source_toolkit is None
    finally:
        await wiring.aclose()


# --- loud configuration failures ----------------------------------------------------------------


async def test_missing_db_is_a_loud_error_on_the_v4_engine(tmp_path: Path) -> None:
    services = fake_services(db=None)
    with pytest.raises(AgentWiringError, match=r"services\.db"):
        await build_agent_wiring(
            services, user_id="u1", run_id="01M68Z000000000000000000A0", **_wiring_kwargs(tmp_path)
        )


def test_the_source_toolkit_requires_its_session_factory() -> None:
    with pytest.raises(AgentWiringError, match=r"services\.db"):
        build_source_toolkit(fake_services(db=None), user_id="u1")


# --- artifact root ------------------------------------------------------------------------------


def test_artifact_root_is_the_sources_setting() -> None:
    # The sources-v4 lane owns the setting (`BOTAI_SOURCE_ARTIFACTS_DIR`): the same store serves
    # the runner and the agent, so both see the same artifacts.
    configured = fixture_settings()
    assert artifact_root_from_settings(configured) == Path(configured.source_artifacts_dir)
    custom = SimpleNamespace(source_artifacts_dir="/srv/botai/source-artifacts")
    assert artifact_root_from_settings(custom) == Path("/srv/botai/source-artifacts")  # type: ignore[arg-type]


def test_artifact_root_falls_back_to_the_workspace_volume() -> None:
    configured = SimpleNamespace(agent_workspace_root="/srv/botai/agent-workspaces")
    assert artifact_root_from_settings(configured) == Path(  # type: ignore[arg-type]
        "/srv/botai/agent-workspaces/source-artifacts"
    )


def test_artifact_root_last_resort_is_the_temp_root() -> None:
    assert artifact_root_from_settings(SimpleNamespace()) == DEFAULT_ARTIFACT_ROOT  # type: ignore[arg-type]


# --- auth_ref -> Vault --------------------------------------------------------------------------


async def test_the_secret_reader_resolves_a_typed_vault_handle() -> None:
    vault = InMemoryVault()
    await vault.put("u1", "connection-1", SecretKind.TOKEN, "bearer-FIXTURE")
    reader = VaultSecretReader(vault, user_id="u1")

    assert await reader("token:connection-1") == "bearer-FIXTURE"


async def test_the_secret_reader_refuses_malformed_and_missing_handles() -> None:
    reader = VaultSecretReader(InMemoryVault(), user_id="u1")
    with pytest.raises(BotaiError) as malformed:
        await reader("just-a-name")
    assert malformed.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as unknown_kind:
        await reader("keychain:scope")
    assert unknown_kind.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as missing:
        await reader("token:nowhere")
    assert missing.value.code is ErrorCode.VALIDATION
    # The error never echoes a secret; there is none here, only handles.
    assert "nowhere" not in missing.value.detail
