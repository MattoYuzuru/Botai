"""Wiring of the phase-1 v4 harness behind `agent_engine=v4` (plan 1.1-1.2, task-16).

One call to `build_agent_wiring` assembles everything a run needs: the runtime, the provider
chosen by settings, the code executor, the browser control, the secret broker, the DB trace store
and the joined tool registry. The v3 path is untouched; the flag decides which engine runs, and
`ENV=prod` refuses the local code executor explicitly — the production sandbox will be the
container executor, not agent code on the worker host (docs/v4/13-executor.md §2, 15-runtime-wiring).

`build_executor` is the single place that decides where code runs, so the prod guard cannot be
bypassed by a forgotten call site.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from botai.agent.v4.executor import (
    ContainerExecutor,
    ExecLimits,
    Executor,
    LocalExecutor,
    Workspace,
)
from botai.agent.v4.protocol import ToolHandler
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.providers.factory import build_provider
from botai.agent.v4.runtime import AgentRuntime, RunLimits
from botai.agent.v4.tools import build_basic_tools
from botai.agent.v4.tools.actions import build_action_tools
from botai.agent.v4.tools.browser import build_browser_tools
from botai.agent.v4.tools.code import build_code_tools
from botai.agent.v4.tools.rag import build_rag_tools
from botai.agent.v4.tools.secrets import SecretBroker, build_secret_tools
from botai.agent.v4.tools.sources import SourceToolkit, build_source_tools
from botai.agent.v4.trace_store import DbTraceStore
from botai.agent.v4.traces import MemoryTraceStore, TraceStore
from botai.browser.control import BrowserControl, ControlConfig
from botai.browser.gateway import PlaywrightGateway
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind, Vault
from botai.settings import Settings
from botai.sources.v4.artifacts import FileArtifactStore

DEFAULT_WORKSPACE_ROOT = Path(tempfile.gettempdir()) / "botai-agent-v4"
DEFAULT_ARTIFACT_ROOT = DEFAULT_WORKSPACE_ROOT / "source-artifacts"


class AgentWiringError(RuntimeError):
    """The requested wiring is not allowed here (configuration/security, not a model error)."""


def run_limits_from_settings(settings: Settings) -> RunLimits:
    """Map the owner-facing `agent_limits_*` settings onto the runtime's soft limits (D-009)."""
    return RunLimits(
        max_steps=settings.agent_limits_steps,
        max_seconds=settings.agent_limits_seconds,
        max_tokens=settings.agent_limits_tokens,
        max_usd=settings.agent_limits_usd,
        hard_stop_usd=settings.agent_hard_stop_usd,
        auto_continue=settings.agent_auto_continue,
    )


def build_executor(
    settings: Settings,
    workspace: Workspace,
    *,
    limits: ExecLimits | None = None,
) -> Executor:
    """The only place that decides where agent code runs (docs/v4/13-executor.md §5).

    In production it is the Docker sandbox and never a host process: the worker holds
    `DATABASE_URL`, the Vault KEK and the provider keys, so `BOTAI_AGENT_EXECUTOR=local` there is a
    loud configuration error. Outside prod the bounded `LocalExecutor` is the default (dev and tests
    need no Docker); `BOTAI_AGENT_EXECUTOR=container` opts into the real sandbox for a check.
    """
    if settings.env == "prod":
        if settings.agent_executor == "local":
            raise AgentWiringError(
                "BOTAI_AGENT_EXECUTOR=local запрещён в ENV=prod: local-исполнитель не изолирует "
                "код от worker'а с секретами (DATABASE_URL, BOTAI_KEK, Vault). В production код "
                "исполняется только в контейнерной песочнице (ContainerExecutor); см. "
                "docs/v4/13-executor.md §2 и §5."
            )
        return _container_executor(settings, workspace, limits)
    if settings.agent_executor == "container":
        return _container_executor(settings, workspace, limits)
    return LocalExecutor(workspace=workspace, limits=limits)


def _container_executor(
    settings: Settings, workspace: Workspace, limits: ExecLimits | None
) -> ContainerExecutor:
    """The Docker sandbox wired from settings: image and optional Docker endpoint."""
    return ContainerExecutor(
        workspace=workspace,
        limits=limits,
        image=settings.botai_sandbox_image,
        docker_host=settings.botai_sandbox_docker_host,
    )


def workspace_for(root: str | Path | None, run_id: str) -> Workspace:
    """A task workspace under `root` (default: a temp root) that survives run restarts."""
    base = Path(root) if root is not None else DEFAULT_WORKSPACE_ROOT
    return Workspace.create(base / run_id)


async def launch_browser_control(
    services: Services,
    *,
    workspace: Workspace,
    mask: Callable[[str], str] | None = None,
) -> BrowserControl:
    """A full-control browser for the run; reuses the worker gateway's driver when available.

    `upload_root` keeps browser uploads inside the task workspace; `redact_body` makes the
    control mask primary secrets in captured bodies before they are stored in the network log or
    HAR (the runtime's own `browser_network` wrapper masks again, defense in depth).
    """
    gateway = services.browser if isinstance(services.browser, PlaywrightGateway) else None
    return await BrowserControl.launch(
        gateway=gateway,
        config=ControlConfig(
            headless=True,
            upload_root=workspace.root,
            redact_body=mask,
        ),
    )


class VaultSecretReader:
    """Resolve `Source.auth_ref` to a Vault secret for the source runner (never logs the value).

    `auth_ref` is a handle, not a secret: `<kind>:<scope_id>`, where `<kind>` is one of the
    `SecretKind` values (`token`, `password`, `session`, `ical_url`, …) and `<scope_id>` is the
    Vault scope the value was stored under (a connection id, a job id, or an explicit secret
    name). The reader is the only place that turns that handle into plaintext for the runner: the
    runner puts it into a request header/field and never returns it.

    A missing or malformed handle is a loud configuration error, not an empty header: a sync that
    silently reads a site unauthenticated would look like "no data" instead of "no secret".
    """

    def __init__(self, vault: Vault, *, user_id: str) -> None:
        self._vault = vault
        self._user_id = user_id

    async def __call__(self, auth_ref: str) -> str | None:
        kind_name, separator, scope = auth_ref.strip().partition(":")
        if not separator or not kind_name or not scope:
            raise BotaiError(
                ErrorCode.VALIDATION,
                "auth_ref источника должен иметь вид «вид:область» (например, token:<id>)",
            )
        try:
            kind = SecretKind(kind_name)
        except ValueError:
            raise BotaiError(
                ErrorCode.VALIDATION, f"auth_ref источника: неизвестный вид секрета {kind_name!r}"
            ) from None
        secret = await self._vault.get(self._user_id, scope, kind)
        if secret is None:
            raise BotaiError(
                ErrorCode.VALIDATION,
                f"Секрет auth_ref={kind_name}:<…> не найден в Vault: источник не прочитан",
            )
        return secret.get_secret_value()


def workspace_root_from_settings(settings: Settings) -> Path | None:
    """The run workspace volume when the owner configured one (`BOTAI_AGENT_WORKSPACE_ROOT`).

    Empty (the dev/test default) means the temp root: workspaces are disposable there. Production
    points this at a persistent volume so a run survives restarts and the container executor can
    bind-mount the same absolute path (docs/v4/13-executor.md).
    """
    configured = getattr(settings, "agent_workspace_root", "")
    return Path(configured) if isinstance(configured, str) and configured else None


def artifact_root_from_settings(settings: Settings) -> Path:
    """Where source artifacts live: the lane-C setting when it exists, else a persistent-ish root.

    The setting is owned by the sources-v4 work (`BOTAI_SOURCE_ARTIFACTS_DIR`); reading it lazily
    keeps this wiring independent of the exact field name and lets it become effective without a
    second edit here. Without it, artifacts go next to the configured workspace volume (or under
    the temp root in dev). Production must point this at a persistent volume.
    """
    configured = getattr(settings, "source_artifacts_dir", None)
    if configured:
        return Path(configured)
    workspace = workspace_root_from_settings(settings)
    if workspace is not None:
        return workspace / "source-artifacts"
    return DEFAULT_ARTIFACT_ROOT


def source_executor_factory(settings: Settings) -> Callable[[], Executor]:
    """Where playbook parser code runs: the same single choice point as `build_executor`.

    Out of prod a fresh `LocalExecutor` owns and cleans its own temp workspace per call. In prod
    only the container executor may run parser code, and `build_executor` is the one place that
    decides it (docs/v4/13-executor.md §2). The sandbox container bind-mounts the task workspace,
    so that workspace must live under the configured agent workspace root (a host path mounted in
    the worker container too): a temp directory created inside the worker container would not
    exist for the host Docker daemon.
    """
    if settings.env == "prod":
        root = workspace_root_from_settings(settings)

        def _container_source_executor() -> Executor:
            return build_executor(settings, workspace_for(root, f"source-{new_id()}"))

        return _container_source_executor
    return LocalExecutor


def build_source_toolkit(
    services: Services,
    *,
    user_id: str,
    artifact_root: str | Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    cookies: Mapping[str, str] | None = None,
) -> SourceToolkit:
    """The `source_*` tools over the real DB, artifact root, Vault reader and executor.

    Raises `AgentWiringError` when the product engine is on but a required collaborator is absent:
    silently running without sources tools would make the agent report "I cannot connect sources"
    while the product pretends v4 is on.
    """
    db = getattr(services, "db", None)
    if db is None:
        raise AgentWiringError(
            "Источники v4 включены, но у сервисов нет фабрики сессий БД: source_* инструменты "
            "нельзя собрать (нужен services.db)."
        )
    root = (
        Path(artifact_root)
        if artifact_root is not None
        else artifact_root_from_settings(services.settings)
    )
    return SourceToolkit(
        sm=db,
        transport=transport,
        artifact_store=FileArtifactStore(root),
        executor_factory=source_executor_factory(services.settings),
        clock=services.clock,
        cookies=cookies,
        secret_reader=VaultSecretReader(services.vault, user_id=user_id),
    )


def build_rag_toolset(services: Services) -> list[ToolHandler]:
    """The `rag_*` tools over the real DB session factory; a broken RAG config fails loudly.

    `BOTAI_RAG_ENABLED` off is an explicit configuration, so the empty list is correct there.
    Enabled but not assemblable (bad endpoint provider, missing dependency) is a configuration
    error the run must not paper over with a silent no-RAG toolset.
    """
    db = getattr(services, "db", None)
    if db is None:
        raise AgentWiringError("RAG-инструменты v4 не собрать без services.db")
    try:
        return build_rag_tools(db)
    except AgentWiringError:
        raise
    except Exception as exc:
        raise AgentWiringError(f"RAG включён, но инструменты не собираются: {exc}") from exc


@dataclass(slots=True)
class AgentWiring:
    """Everything one v4 run needs; `aclose` releases the browser, executor and provider."""

    runtime: AgentRuntime
    provider: Provider
    tools: list[ToolHandler]
    trace_store: TraceStore
    workspace: Workspace
    executor: Executor
    control: BrowserControl | None = None
    broker: SecretBroker | None = None
    source_toolkit: SourceToolkit | None = None
    owns_provider: bool = False

    async def aclose(self) -> None:
        # Idempotent enough for the handler's finally: a second close is a no-op in the parts.
        if self.control is not None:
            # The run is over; a broken browser must not mask its result.
            with contextlib.suppress(Exception):
                await self.control.close()
        if isinstance(self.executor, (ContainerExecutor, LocalExecutor)):
            self.executor.close()
        if self.broker is not None:
            self.broker.clear()
        if self.owns_provider:
            await self.provider.aclose()


async def build_agent_wiring(
    services: Services,
    *,
    user_id: str,
    run_id: str,
    job_id: str | None = None,
    workspace_root: str | Path | None = None,
    provider: Provider | None = None,
    trace_store: TraceStore | None = None,
    with_browser: bool = True,
    control: BrowserControl | None = None,
    exec_limits: ExecLimits | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
) -> AgentWiring:
    """Assemble the runtime for one run. The caller owns `aclose` (handler does it in finally).

    With `agent_engine=v4` the product tools are part of the run: `source_*` over the real source
    service/runner and `rag_*` over the real RAG service. A missing required collaborator (the DB
    session factory, the RAG config being enabled but broken) raises `AgentWiringError` instead of
    handing the model a silently reduced toolset.
    """
    settings = services.settings
    root = (
        Path(workspace_root)
        if workspace_root is not None
        else workspace_root_from_settings(settings)
    )
    workspace = workspace_for(root, run_id)
    executor = build_executor(settings, workspace, limits=exec_limits)
    active_provider = provider or build_provider(settings)
    store: TraceStore = trace_store or DbTraceStore(services.db, user_id=user_id, job_id=job_id)
    broker = SecretBroker(services.vault, user_id=user_id, scope_id=job_id or run_id)
    active_control = control
    if active_control is None and with_browser:
        active_control = await launch_browser_control(
            services, workspace=workspace, mask=broker.mask
        )
    tools: list[ToolHandler] = [*build_basic_tools(), *build_code_tools(executor)]
    if active_control is not None:
        tools.extend(
            build_browser_tools(active_control, workspace=workspace.root, mask=broker.mask)
        )
    tools.extend(build_secret_tools(broker, control=active_control, transport=http_transport))
    source_toolkit: SourceToolkit | None = None
    if settings.agent_engine == "v4":
        source_toolkit = build_source_toolkit(
            services,
            user_id=user_id,
            artifact_root=artifact_root,
            transport=http_transport,
            cookies=source_cookies,
        )
        tools.extend(build_source_tools(source_toolkit))
        tools.extend(build_rag_toolset(services))
        tools.extend(
            build_action_tools(
                services=services,
                user_id=user_id,
                http_transport=http_transport,
                control=active_control,
            )
        )
    # The broker masker is applied by the runtime to every tool result (code_exec stdout, fs_*,
    # http, trace_read included): a parser that prints a profile e-mail must not reach the model,
    # the conversation checkpoint or the trace (D-008/D-014).
    runtime = AgentRuntime(clock=services.clock.now, mask=broker.mask)
    return AgentWiring(
        runtime=runtime,
        provider=active_provider,
        tools=tools,
        trace_store=store,
        workspace=workspace,
        executor=executor,
        control=active_control,
        broker=broker,
        source_toolkit=source_toolkit,
        owns_provider=provider is None,
    )


async def enqueue_agent_run(
    services: Services,
    *,
    user_id: str,
    task: str,
    connection_id: str | None = None,
    thread_id: str | None = None,
    workspace_root: str | Path | None = None,
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Programmatic entry point of the v4 agent path (used by the HTTP runs API and chat).

    The job kind is `AGENT`; v3 jobs are untouched. A caller that needs the trace id before the
    job starts (the REST API returns it immediately) passes a pre-generated `run_id`; the handler
    uses it for the run and its trace instead of generating a second one.
    """
    body: dict[str, Any] = {"task": task, **(payload or {})}
    if run_id is not None:
        body["_run_id"] = run_id
    if workspace_root is not None:
        body.setdefault("_workspace_root", str(workspace_root))
    return await services.queue.enqueue(
        JobKind.AGENT,
        user_id=user_id,
        connection_id=connection_id,
        thread_id=thread_id,
        payload=body,
        dedupe=False,
    )


def memory_trace_store() -> MemoryTraceStore:
    """Convenience for tests and one-off local runs."""
    return MemoryTraceStore()


__all__ = [
    "AgentWiring",
    "AgentWiringError",
    "VaultSecretReader",
    "artifact_root_from_settings",
    "build_agent_wiring",
    "build_executor",
    "build_rag_toolset",
    "build_source_toolkit",
    "enqueue_agent_run",
    "launch_browser_control",
    "memory_trace_store",
    "run_limits_from_settings",
    "source_executor_factory",
    "workspace_for",
]
