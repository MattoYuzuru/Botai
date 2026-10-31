"""Code-execution sandbox protocol for the v4 agent: requests, results and a task workspace.

An agent writes code; the runtime hands it to an `Executor` and gets back stdout/stderr, the
exit code, the files the code created and whether the run hit a limit. This module is data and
path arithmetic only — no process is started here. `LocalExecutor`
(`botai.agent.v4.executor.local`) runs code in a separate process on the same host for the dev
and local mode of phase 1; the container implementation is planned (D-003, phase 1.2).

A workspace belongs to one agent task. Files persist between executor calls, so the agent can
write a parser in one call and use it in the next. Every path that enters the workspace goes
through `Workspace.resolve`, which refuses absolute paths and anything that resolves outside the
directory, including through a symlink: code may write anywhere inside the task and nowhere else.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Language = Literal["python", "js", "bash"]

MEBIBYTE = 1024 * 1024

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MEMORY_MB = 256
# Per stream: stdout and stderr are each capped, so a runaway printer cannot exhaust the worker.
DEFAULT_MAX_OUTPUT_BYTES = MEBIBYTE
DEFAULT_MAX_FILES = 1000
DEFAULT_NOFILE = 256
DEFAULT_FSIZE_MB = 64
DEFAULT_MAX_FILE_BYTES = MEBIBYTE
DEFAULT_MAX_REQUEST_BYTES = 4 * MEBIBYTE
DEFAULT_MAX_REQUEST_FILES = 100
# Private subdirectory of a workspace for per-call scripts, HOME and TMPDIR. Hidden from the
# agent's file listing and snapshots: it is executor plumbing, not the agent's material.
INTERNAL_DIR = ".botai-exec"


class ExecutorError(Exception):
    """The executor refused a request or could not run it (not the code's own failure)."""


class WorkspacePathError(ExecutorError, ValueError):
    """A path leaves the workspace, is absolute or points into the executor's private directory."""


class ExecutorUnavailable(ExecutorError):
    """A language runtime or the executor itself is not available on this host."""


@dataclass(frozen=True, slots=True)
class ExecLimits:
    """Limits of one executor call: they are the upper bounds, not only the defaults.

    A request may ask for a lower timeout/memory for one call; asking for more is clamped to
    these values, so a request can never raise the ceiling the deployer configured.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S
    memory_mb: int = DEFAULT_MEMORY_MB
    # RLIMIT_CPU seconds. None = derived from the effective wall timeout (ceil(timeout)).
    cpu_s: int | None = None
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_files: int = DEFAULT_MAX_FILES
    nofile: int = DEFAULT_NOFILE
    fsize_mb: int = DEFAULT_FSIZE_MB
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_request_files: int = DEFAULT_MAX_REQUEST_FILES

    def __post_init__(self) -> None:
        self._require(self.timeout_s > 0 and self.timeout_s <= 3600, "timeout_s must be 0<..<=3600")
        self._require(32 <= self.memory_mb <= 32768, "memory_mb must be 32..32768")
        self._require(
            self.cpu_s is None or 1 <= self.cpu_s <= 3600, "cpu_s must be 1..3600 or None"
        )
        self._require(
            1024 <= self.max_output_bytes <= 64 * MEBIBYTE,
            "max_output_bytes must be 1024..64 MiB",
        )
        self._require(1 <= self.max_files <= 100_000, "max_files must be 1..100000")
        self._require(16 <= self.nofile <= 65_536, "nofile must be 16..65536")
        self._require(1 <= self.fsize_mb <= 4096, "fsize_mb must be 1..4096")
        self._require(1 <= self.max_file_bytes <= 64 * MEBIBYTE, "max_file_bytes must be 1..64 MiB")
        self._require(
            1024 <= self.max_request_bytes <= 256 * MEBIBYTE,
            "max_request_bytes must be 1024..256 MiB",
        )
        self._require(1 <= self.max_request_files <= 1000, "max_request_files must be 1..1000")

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(f"invalid executor limits: {message}")


class ExecRequest(BaseModel):
    """One run of agent-written code. `files` are written into the workspace before it starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Language
    code: str = Field(min_length=1, max_length=200_000)
    files: dict[str, str] = Field(default_factory=dict, max_length=DEFAULT_MAX_REQUEST_FILES)
    # None = the executor's limit; a lower value narrows this one call, a higher one is clamped
    # to ExecLimits (the ceiling of the executor, not of the request model).
    timeout_s: float | None = Field(default=None, gt=0, le=300)
    memory_mb: int | None = Field(default=None, ge=32, le=4096)


class ExecResult(BaseModel):
    """What a run produced. `files` are the files the run created, relative to the workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int = Field(ge=0)
    files: list[str] = Field(default_factory=list)
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Workspace:
    """The guarded directory of one agent task; files persist between executor calls."""

    def __init__(self, root: Path | str, *, owned: bool = False) -> None:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        self._root = path.resolve()
        self._owned = owned

    @classmethod
    def create(cls, root: Path | str | None = None) -> Self:
        """A workspace at `root` (kept by its caller) or a fresh private temporary directory."""
        if root is None:
            return cls(tempfile.mkdtemp(prefix="botai-exec-workspace-"), owned=True)
        return cls(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def owned(self) -> bool:
        """True when the workspace created its own temporary directory and may remove it."""
        return self._owned

    def resolve(self, relative: str) -> Path:
        """A path inside the workspace, or `WorkspacePathError` for anything that would leave it.

        Absolute paths, NUL bytes, the directory itself and the executor's private subdirectory
        are refused; a relative path that climbs out (`../x`) or reaches outside through a
        symlink is refused by resolving it first.
        """
        if not relative:
            raise WorkspacePathError("a path inside the workspace is required")
        if "\x00" in relative:
            raise WorkspacePathError("a workspace path must not contain NUL")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise WorkspacePathError(f"workspace paths must be relative: {relative!r}")
        if candidate.parts and candidate.parts[0] == INTERNAL_DIR:
            # The logical name is reserved even when a symlink would make it resolve elsewhere.
            raise WorkspacePathError(f"the executor's private directory is reserved: {relative!r}")
        resolved = (self._root / candidate).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise WorkspacePathError(f"path leaves the workspace: {relative!r}") from None
        if resolved == self._root:
            raise WorkspacePathError("a file path inside the workspace is required")
        internal = self._root / INTERNAL_DIR
        try:
            resolved.relative_to(internal)
        except ValueError:
            return resolved
        raise WorkspacePathError(f"the executor's private directory is reserved: {relative!r}")

    def write_text(
        self, relative: str, text: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES
    ) -> Path:
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            raise ExecutorError(f"file {relative!r} is larger than {max_bytes} bytes")
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def read_text(self, relative: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
        target = self.resolve(relative)
        if not target.is_file():
            raise ExecutorError(f"no such workspace file: {relative!r}")
        size = target.stat().st_size
        if size > max_bytes:
            raise ExecutorError(f"file {relative!r} is larger than {max_bytes} bytes")
        return target.read_text(encoding="utf-8", errors="replace")

    def list_files(self, *, include_internal: bool = False) -> list[str]:
        return sorted(self.snapshot(include_internal=include_internal))

    def snapshot(
        self, *, limit: int = DEFAULT_MAX_FILES, include_internal: bool = False
    ) -> dict[str, int]:
        """Relative path -> size of every regular file; no symlinks, no private subdirectory."""
        found: dict[str, int] = {}
        for directory, dirnames, filenames in os.walk(self._root, followlinks=False):
            current = Path(directory)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if include_internal or not (current == self._root and name == INTERNAL_DIR)
            )
            for name in sorted(filenames):
                path = current / name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    found[path.relative_to(self._root).as_posix()] = path.stat().st_size
                except OSError:
                    continue
                if len(found) >= limit:
                    return found
        return found

    def internal_dir(self, *parts: str) -> Path:
        """A real directory under the executor's private subdirectory, created on demand.

        Agent code from an earlier call can write anywhere in the workspace, so the reserved
        names may have been replaced by a symlink pointing at the worker's real home or a
        directory outside the workspace. Executor plumbing never follows such a link: every
        component must be a real directory (`lstat`), otherwise the run is refused.
        """
        path = self._root / INTERNAL_DIR
        self._ensure_real_directory(path)
        for part in parts:
            if not part or part in {".", ".."} or "/" in part or os.sep in part:
                raise WorkspacePathError(f"invalid internal directory name: {part!r}")
            path = path / part
            self._ensure_real_directory(path)
        return path

    @staticmethod
    def _ensure_real_directory(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise WorkspacePathError(
                f"the executor's private path {path.name!r} is not a real directory; "
                "refusing to follow it"
            )
        try:
            path.mkdir()
        except FileExistsError:
            if path.is_symlink() or not path.is_dir():
                raise WorkspacePathError(
                    f"the executor's private path {path.name!r} is not a real directory"
                ) from None

    def cleanup(self) -> None:
        """Remove a self-created workspace; for a caller-provided one, only the private plumbing."""
        if self._owned:
            shutil.rmtree(self._root, ignore_errors=True)
            return
        internal = self._root / INTERNAL_DIR
        if internal.is_symlink() or (internal.exists() and not internal.is_dir()):
            # Never follow, and never delete what the link points at: just drop the link itself.
            internal.unlink(missing_ok=True)
            return
        shutil.rmtree(internal, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


@runtime_checkable
class Executor(Protocol):
    """Runs agent code. The runtime sees only this interface; local/container are replaceable."""

    @property
    def workspace(self) -> Workspace: ...

    @property
    def limits(self) -> ExecLimits: ...

    async def run(self, request: ExecRequest) -> ExecResult: ...


def created_files(before: Mapping[str, int], after: Mapping[str, int], *, limit: int) -> list[str]:
    """Relative paths that did not exist before the run, sorted, capped at `limit`."""
    return sorted(set(after) - set(before))[:limit]
