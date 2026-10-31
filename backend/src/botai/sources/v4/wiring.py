"""Production wiring of the v4 source runner: HTTP, Vault sessions, artifacts and sandboxed code.

The router and any worker handler that runs a playbook build the runner through this module, so
the product path (REST/agent) always gets the same collaborators:

* **Vault** — a handle reader for bearer tokens (`agent.v4.factory.VaultSecretReader`, the one
  implementation of the `kind:scope` convention) and `VaultSessionReader` for storage-state
  sessions; the runner applies them to HTTP cookies and to the browser context;
* **artifacts** — a content-addressed `FileArtifactStore` under `settings.source_artifacts_dir`
  (`BOTAI_SOURCE_ARTIFACTS_DIR`), so pages/JSON bodies survive the request and can be cleaned by
  retention;
* **code sandbox** — the executor factory from `agent.v4.factory`: `LocalExecutor` in dev/test and
  the container executor in prod (never agent code on the worker host);
* **browser** — a full-control Chromium per browser playbook; the factory never decides policy.

Nothing here is a test double: tests override the FastAPI runner-builder dependency instead. A
missing collaborator raises at wiring time instead of degrading a run silently.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.executor import Executor
from botai.core.vault import Vault
from botai.db.session import get_sessionmaker
from botai.settings import Settings
from botai.sources.v4 import artifacts, browser, secrets
from botai.sources.v4.runner import SourceRunner
from botai.vault.factory import build_vault

RunnerBuilder = Callable[[AsyncSession, str], SourceRunner]


@dataclass(frozen=True, slots=True)
class RunnerWiring:
    """Everything a runner needs that is the same for every request of one settings object."""

    settings: Settings
    vault: Vault
    artifact_store: artifacts.FileArtifactStore
    session_reader: secrets.VaultSessionReader
    executor_factory: Callable[[], Executor]
    browser_factory: browser.BrowserFactory

    def runner(self, session: AsyncSession, user_id: str) -> SourceRunner:
        from botai.agent.v4.factory import VaultSecretReader

        return SourceRunner(
            session,
            user_id=user_id,
            artifact_store=self.artifact_store,
            executor_factory=self.executor_factory,
            secret_reader=VaultSecretReader(self.vault, user_id=user_id),
            session_reader=self.session_reader,
            browser_factory=self.browser_factory,
            env=self.settings.env,
        )


def build_runner_wiring(settings: Settings) -> RunnerWiring:
    """Assemble the real collaborators. Cheap: no connection is opened until a run uses them.

    The agent wiring module is imported lazily: a v3 process still serves the v4 router's 404 and
    must not import the whole harness tool stack just because the routes exist.
    """
    from botai.agent.v4.factory import source_executor_factory

    vault = build_vault(settings, get_sessionmaker())
    root = Path(settings.source_artifacts_dir)
    return RunnerWiring(
        settings=settings,
        vault=vault,
        artifact_store=artifacts.FileArtifactStore(root),
        session_reader=secrets.VaultSessionReader(vault),
        executor_factory=source_executor_factory(settings),
        browser_factory=browser.playwright_factory(),
    )


# One wiring per settings object: `get_settings()` is a process-wide singleton in production, and
# building the Vault/logging a dev-key warning per request would be noise. Keyed by identity so a
# test that builds its own Settings gets its own wiring; the strong reference keeps the object
# alive (a handful of instances at most).
_WIRED: dict[int, tuple[Settings, RunnerWiring]] = {}


def wiring_for(settings: Settings) -> RunnerWiring:
    found = _WIRED.get(id(settings))
    if found is not None and found[0] is settings:
        return found[1]
    wiring = build_runner_wiring(settings)
    _WIRED[id(settings)] = (settings, wiring)
    return wiring


def build_default_runner_builder(settings: Settings) -> RunnerBuilder:
    """The router's `get_runner_builder`: a closure over one settings object."""
    wiring = wiring_for(settings)
    return wiring.runner
