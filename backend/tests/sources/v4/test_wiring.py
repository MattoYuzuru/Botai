"""The production runner wiring: real Vault, artifact store, sandboxed executor and browser.

These are the collaborators the REST router builds a runner with (`get_runner_builder`); tests
elsewhere override that dependency, so this file is the only place the real wiring is asserted.
No connection is opened and no browser is launched here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botai.agent.v4.executor import ContainerExecutor, LocalExecutor
from botai.agent.v4.factory import AgentWiringError
from botai.settings import Settings
from botai.sources.v4.artifacts import FileArtifactStore
from botai.sources.v4.secrets import VaultSessionReader
from botai.sources.v4.wiring import build_runner_wiring

pytestmark = pytest.mark.db


def test_wiring_builds_the_real_collaborators(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={
            "source_artifacts_dir": str(tmp_path / "artifacts"),
            "env": "test",
        }
    )
    wiring = build_runner_wiring(settings)
    assert isinstance(wiring.artifact_store, FileArtifactStore)
    assert wiring.artifact_store.root == tmp_path / "artifacts"
    assert isinstance(wiring.session_reader, VaultSessionReader)
    assert wiring.vault is not None
    factory = wiring.executor_factory
    executor = factory()
    try:
        assert isinstance(executor, LocalExecutor)  # dev/test: a local sandbox is explicit
    finally:
        close = getattr(executor, "close", None)
        if callable(close):
            close()
    # The browser factory is the production one: a real full-control Chromium per browser run.
    assert wiring.browser_factory is not None


def test_prod_wiring_has_no_local_executor_fallback(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={
            "source_artifacts_dir": str(tmp_path / "artifacts"),
            "env": "prod",
        }
    )
    wiring = build_runner_wiring(settings)
    executor = wiring.executor_factory()
    assert isinstance(executor, ContainerExecutor)  # the Docker sandbox, never a host process
    assert not isinstance(executor, LocalExecutor)
    # An explicit "local" in prod is a loud configuration error, not a silent fallback.
    forced_local = settings.model_copy(update={"agent_executor": "local"})
    wired_local = build_runner_wiring(forced_local)
    with pytest.raises(AgentWiringError):
        wired_local.executor_factory()
