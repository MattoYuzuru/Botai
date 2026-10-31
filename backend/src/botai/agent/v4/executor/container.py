"""Placeholder for the container code executor (phase 1.2b). No Docker implementation yet.

The plan (docs/v4/02-architecture.md C5, docs/v4/13-executor.md) is one long-lived sandbox
container per user/task: the workspace is a volume mounted at the same place, the code runs as a
non-root user with a read-only root filesystem, network available (free agent, D-003), and
CPU/memory/pids/disk limits enforced by cgroups instead of best-effort `setrlimit`. It implements
the same `Executor` protocol, so the runtime is wired to the protocol and can switch by config.

This class exists so the runtime can be written against the final interface now, and so a missing
container executor fails loudly instead of silently running code on the worker host.
"""

from __future__ import annotations

from botai.agent.v4.executor.protocol import (
    ExecLimits,
    ExecRequest,
    ExecResult,
    ExecutorUnavailable,
    Workspace,
)

DEFAULT_IMAGE = "botai-sandbox:latest"


class ContainerExecutor:
    """Explicit "not implemented yet" counterpart of `LocalExecutor`.

    Every call raises `ExecutorUnavailable`; there is deliberately no fallback to a local process:
    a host that expected a sandbox must not run agent code without one.
    """

    def __init__(
        self,
        *,
        workspace: Workspace | None = None,
        limits: ExecLimits | None = None,
        image: str = DEFAULT_IMAGE,
    ) -> None:
        self._workspace = workspace or Workspace.create()
        self._limits = limits or ExecLimits()
        self._image = image

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def limits(self) -> ExecLimits:
        return self._limits

    @property
    def image(self) -> str:
        return self._image

    async def run(self, request: ExecRequest) -> ExecResult:
        raise ExecutorUnavailable(
            "the container executor is not implemented yet; the phase-1 runtime uses "
            "LocalExecutor (docs/v4/13-executor.md)"
        )
