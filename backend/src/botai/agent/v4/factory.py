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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from botai.agent.v4.executor import ExecLimits, Executor, LocalExecutor, Workspace
from botai.agent.v4.protocol import ToolHandler
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.providers.factory import build_provider
from botai.agent.v4.runtime import AgentRuntime, RunLimits
from botai.agent.v4.tools import build_basic_tools
from botai.agent.v4.tools.browser import build_browser_tools
from botai.agent.v4.tools.code import build_code_tools
from botai.agent.v4.tools.secrets import SecretBroker, build_secret_tools
from botai.agent.v4.trace_store import DbTraceStore
from botai.agent.v4.traces import MemoryTraceStore, TraceStore
from botai.browser.control import BrowserControl, ControlConfig
from botai.browser.gateway import PlaywrightGateway
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.settings import Settings

DEFAULT_WORKSPACE_ROOT = Path(tempfile.gettempdir()) / "botai-agent-v4"


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
    """The executor for this environment: local only outside prod (the container comes later).

    `LocalExecutor` runs agent code as the worker user: it is bounded (time/output/files) and its
    environment is cleaned, but it is not a security boundary against a hostile program. The
    production worker holds the database credentials and the Vault KEK, so wiring it there is
    refused loudly instead of silently running code next to secrets.
    """
    if settings.env == "prod":
        raise AgentWiringError(
            "LocalExecutor запрещён в ENV=prod: он не изолирует код от worker'а с секретами "
            "(DATABASE_URL, BOTAI_KEK, Vault). Для production нужен container executor; "
            "см. docs/v4/13-executor.md §2 и docs/v4/15-runtime-wiring.md."
        )
    return LocalExecutor(workspace=workspace, limits=limits)


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
    owns_provider: bool = False

    async def aclose(self) -> None:
        # Idempotent enough for the handler's finally: a second close is a no-op in the parts.
        if self.control is not None:
            # The run is over; a broken browser must not mask its result.
            with contextlib.suppress(Exception):
                await self.control.close()
        if isinstance(self.executor, LocalExecutor):
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
) -> AgentWiring:
    """Assemble the runtime for one run. The caller owns `aclose` (handler does it in finally)."""
    settings = services.settings
    workspace = workspace_for(workspace_root, run_id)
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
    runtime = AgentRuntime(clock=services.clock.now)
    return AgentWiring(
        runtime=runtime,
        provider=active_provider,
        tools=tools,
        trace_store=store,
        workspace=workspace,
        executor=executor,
        control=active_control,
        broker=broker,
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
    payload: dict[str, Any] | None = None,
) -> str:
    """Programmatic entry point of the experimental path (the HTTP route comes with the UI).

    The job kind is `AGENT`; v3 jobs are untouched. `agent_engine` must be `v4` for the handler to
    run, so enqueueing from an old deployment fails cleanly in the job, not silently.
    """
    body: dict[str, Any] = {"task": task, **(payload or {})}
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
    "build_agent_wiring",
    "build_executor",
    "enqueue_agent_run",
    "launch_browser_control",
    "memory_trace_store",
    "run_limits_from_settings",
    "workspace_for",
]
