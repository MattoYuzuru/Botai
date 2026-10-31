"""Code-execution sandbox of the v4 agent (phase 1.2): Executor, requests, results, workspaces.

The runtime calls `Executor.run(ExecRequest) -> ExecResult`; `LocalExecutor` runs the code in a
bounded process on this host for the dev/local mode, and `ContainerExecutor` is the explicit
placeholder for the isolated sandbox container of the next iteration. See docs/v4/13-executor.md.
"""

from botai.agent.v4.executor.container import DEFAULT_IMAGE, ContainerExecutor
from botai.agent.v4.executor.local import SCRIPT_NAMES, LocalExecutor
from botai.agent.v4.executor.protocol import (
    DEFAULT_FSIZE_MB,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_REQUEST_FILES,
    DEFAULT_MEMORY_MB,
    DEFAULT_NOFILE,
    DEFAULT_TIMEOUT_S,
    INTERNAL_DIR,
    MEBIBYTE,
    ExecLimits,
    ExecRequest,
    ExecResult,
    Executor,
    ExecutorError,
    ExecutorUnavailable,
    Language,
    Workspace,
    WorkspacePathError,
    created_files,
)

__all__ = [
    "DEFAULT_FSIZE_MB",
    "DEFAULT_IMAGE",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_REQUEST_FILES",
    "DEFAULT_MEMORY_MB",
    "DEFAULT_NOFILE",
    "DEFAULT_TIMEOUT_S",
    "INTERNAL_DIR",
    "MEBIBYTE",
    "SCRIPT_NAMES",
    "ContainerExecutor",
    "ExecLimits",
    "ExecRequest",
    "ExecResult",
    "Executor",
    "ExecutorError",
    "ExecutorUnavailable",
    "Language",
    "LocalExecutor",
    "Workspace",
    "WorkspacePathError",
    "created_files",
]
