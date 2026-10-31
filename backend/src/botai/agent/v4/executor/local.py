"""Local code executor: a separate process on the worker host, bounded in time/output/files.

This is the phase-1 developer/local sandbox. It is **not** a security boundary against a
determined attacker: the code runs as the worker user with the network available (D-003 lets the
agent reach any site), and only the box's POSIX limits, the cleaned environment and the workspace
path guard stand between it and the rest of the machine. The real isolation is the container
executor in `botai.agent.v4.executor.container`; this one keeps the same `Executor` API so the
runtime does not care which is wired.

What it does guarantee in local mode:

- a fresh process per call in its own session (a timeout kills the whole process group, and
  lingering children are killed when the call ends);
- an environment built from a safe allowlist: no parent `BOTAI_*`, `DATABASE_URL`, API keys or
  other secrets, `HOME`/`TMPDIR`/XDG dirs inside the workspace, never the real ones;
- `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE` and (where the OS enforces it — Linux) the
  address-space limit; macOS refuses `RLIMIT_AS`/`RLIMIT_DATA`, so memory is best effort there;
- stdout/stderr each capped, a call that keeps printing is drained and truncated, not buffered
  without bound;
- the request-provided files and every file the code creates live only inside the workspace, and
  paths that would leave it are refused before anything is written.

`preexec_fn` applies the POSIX limits between fork and exec. That call is not thread-safe in
general; the executor runs one call at a time per workspace and the function only calls
`setrlimit`, so the risk is limited to the local mode that is being replaced by a container.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

from botai.agent.v4.executor.protocol import (
    MEBIBYTE,
    ExecLimits,
    ExecRequest,
    ExecResult,
    ExecutorError,
    ExecutorUnavailable,
    Language,
    Workspace,
    created_files,
)

# Language -> file name of the script written into the executor's private run directory.
SCRIPT_NAMES: dict[Language, str] = {
    "python": "main.py",
    "js": "main.js",
    "bash": "main.sh",
}

_LANGUAGE_LABELS: dict[Language, str] = {
    "python": "Python",
    "js": "Node.js",
    "bash": "bash",
}

# An env key that looks like a secret or a backend setting is never passed to agent code, not even
# through the explicit `extra_env` of an embedding application.
_FORBIDDEN_ENV = re.compile(
    r"(?i:^(?:BOTAI_.*|DATABASE_(?:URL|DSN)|KEK.*)$"
    r"|(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|PASS|API_KEY|PRIVATE_KEY|CREDENTIALS?)(?:_|$))"
)

# The parent's PATH lets the agent use the tools installed for the worker (node, git, curl…).
_FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Variables that describe the locale/time zone, not the application: safe to inherit.
_INHERITED_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")

# The executor owns these: a caller must not be able to point HOME/PATH/PYTHONPATH back at the
# worker's real environment through `extra_env`.
_RESERVED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "BASH_ENV",
        "NODE_OPTIONS",
    }
)

# Set by the executor for every run. HOME and the XDG dirs are inside the workspace so that a
# program cannot read or write the worker's real home while it thinks it is home.
_FIXED_ENV: dict[str, str] = {
    "NO_COLOR": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
}

# Node/V8 reserves a huge virtual address space at startup (pointer-compression cage), so
# RLIMIT_AS would kill `node` before it runs anything. Memory is limited for the other languages;
# the container executor will limit every language with cgroups.
_MEMORY_LIMITED_LANGUAGES: frozenset[Language] = frozenset({"python", "bash"})

_READ_CHUNK = 64 * 1024


def _set_limit(which: int, value: tuple[int, int]) -> None:
    try:
        resource.setrlimit(which, value)
    except (OSError, ValueError):
        # Not every host exposes every limit (macOS refuses RLIMIT_AS/RLIMIT_DATA). Local mode is
        # best effort here; the container executor is where memory limits become reliable.
        return


def _limit_applier(
    *, cpu_s: int, memory_mb: int, nofile: int, fsize_mb: int, language: Language
) -> Callable[[], None]:
    """Build the child-side function applied between fork and exec (POSIX only)."""

    def apply() -> None:
        _set_limit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
        if language in _MEMORY_LIMITED_LANGUAGES:
            memory_bytes = memory_mb * MEBIBYTE
            _set_limit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            _set_limit(resource.RLIMIT_DATA, (memory_bytes, memory_bytes))
        _set_limit(resource.RLIMIT_NOFILE, (nofile, nofile))
        _set_limit(resource.RLIMIT_FSIZE, (fsize_mb * MEBIBYTE, fsize_mb * MEBIBYTE))
        _set_limit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _read_capped(stream: IO[bytes], sink: bytearray, cap: int, truncated: threading.Event) -> None:
    """Read a pipe until EOF, keeping at most `cap` bytes; the rest is drained and dropped."""
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                return
            room = cap - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
            if len(chunk) > room:
                truncated.set()
    except (OSError, ValueError):  # the parent closed the pipe after killing the process group
        return


def _kill_group(pid: int) -> None:
    """Kill the run's process group; its leader is the Popen child (start_new_session)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        # The group is already gone (ProcessLookupError) or was never a group: nothing to kill.
        # SIGKILL is not catchable, so a repeated call is harmless.
        return


def _sanitized_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (extra or {}).items():
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError(f"invalid executor environment entry: {key!r}")
        if key.upper() in _RESERVED_ENV:
            raise ValueError(
                f"executor environment entry {key!r} is controlled by the executor; "
                "an embedder cannot point HOME/PATH/PYTHONPATH back at the worker"
            )
        if _FORBIDDEN_ENV.search(key):
            raise ValueError(
                f"executor environment entry {key!r} looks like a secret or a backend setting; "
                "it is never passed to runnable code"
            )
        result[key] = value
    return result


class LocalExecutor:
    """Runs agent code in a bounded subprocess on this host (see the module docstring)."""

    def __init__(
        self,
        *,
        workspace: Workspace | None = None,
        limits: ExecLimits | None = None,
        python: str | None = None,
        node: str | None = None,
        bash: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._limits = limits or ExecLimits()
        self._owns_workspace = workspace is None
        self._workspace = workspace or Workspace.create()
        self._python = python or sys.executable
        self._node = node
        self._bash = bash
        self._extra_env = _sanitized_env(extra_env)
        # One workspace is one task: runs are serialised so two calls never share cwd at once.
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def limits(self) -> ExecLimits:
        return self._limits

    async def run(self, request: ExecRequest) -> ExecResult:
        if self._closed:
            raise ExecutorUnavailable("the executor is closed")
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, request)

    def close(self) -> None:
        """Drop the workspace when the executor created it; a caller-provided one is kept."""
        if self._closed:
            return
        self._closed = True
        if self._owns_workspace:
            self._workspace.cleanup()

    # --- one call ---------------------------------------------------------------------------

    def _run_sync(self, request: ExecRequest) -> ExecResult:
        limits, workspace = self._limits, self._workspace
        self._check_request(request)
        interpreter = self._interpreter(request.language)
        # Validate every destination before writing anything: a refused path must not leave a
        # half-written request in the workspace.
        for relative in request.files:
            workspace.resolve(relative)
        for relative, content in request.files.items():
            workspace.write_text(relative, content, max_bytes=limits.max_file_bytes)

        # The executor's limits are caps, not just defaults: a request may narrow one call but
        # never raise the configured ceiling (timeout/memory of the whole task).
        timeout_s = (
            min(request.timeout_s, limits.timeout_s)
            if request.timeout_s is not None
            else limits.timeout_s
        )
        memory_mb = (
            min(request.memory_mb, limits.memory_mb)
            if request.memory_mb is not None
            else limits.memory_mb
        )
        # Environment first: it touches the private directory, which agent code may have tried
        # to replace with a symlink; a hijacked workspace must fail before a run dir is made.
        environment = self._environment(workspace, request.language)
        run_dir = workspace.internal_dir("runs") / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        script = run_dir / SCRIPT_NAMES[request.language]
        script.write_text(request.code, encoding="utf-8")
        before = workspace.snapshot(limit=limits.max_files)
        started = time.monotonic()
        try:
            process = self._spawn(
                [interpreter, str(script)],
                cwd=workspace.root,
                env=environment,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
                language=request.language,
            )
            stdout, stderr, truncated, timed_out = self._collect(process, timeout_s)
            exit_code = process.returncode if process.returncode is not None else -1
            duration_ms = int((time.monotonic() - started) * 1000)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        after = workspace.snapshot(limit=limits.max_files)
        return ExecResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            files=created_files(before, after, limit=limits.max_files),
            truncated=truncated,
            timed_out=timed_out,
        )

    def _check_request(self, request: ExecRequest) -> None:
        limits = self._limits
        if len(request.files) > limits.max_request_files:
            raise ExecutorError(
                f"too many request files: {len(request.files)} > {limits.max_request_files}"
            )
        total = sum(len(content.encode("utf-8")) for content in request.files.values())
        if total > limits.max_request_bytes:
            raise ExecutorError(
                f"request files are too large: {total} > {limits.max_request_bytes} bytes"
            )

    def _interpreter(self, language: Language) -> str:
        if language == "python":
            return self._require(self._python, language)
        if language == "js":
            return self._require(self._node or shutil.which("node"), language)
        return self._require(self._bash or shutil.which("bash") or "/bin/bash", language)

    @staticmethod
    def _require(path: str | None, language: Language) -> str:
        if path is None or not Path(path).is_file():
            label = _LANGUAGE_LABELS[language]
            raise ExecutorUnavailable(
                f"{label} runtime is not available in the local executor: {path!r}. "
                "Install it or pass its path; the container executor will ship its own runtimes."
            )
        return path

    def _environment(self, workspace: Workspace, language: Language) -> dict[str, str]:
        home = workspace.internal_dir("home")
        temp = workspace.internal_dir("tmp")
        environment: dict[str, str] = {
            "PATH": os.environ.get("PATH") or _FALLBACK_PATH,
            "HOME": str(home),
            "TMPDIR": str(temp),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            **_FIXED_ENV,
            **self._extra_env,
        }
        for name in _INHERITED_ENV:
            value = os.environ.get(name)
            if value:
                environment.setdefault(name, value)
        if language == "python":
            # The agent can import a helper it wrote in an earlier call from the workspace cwd.
            environment["PYTHONPATH"] = str(workspace.root)
        return environment

    # --- the child process ------------------------------------------------------------------

    def _spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        memory_mb: int,
        language: Language,
    ) -> subprocess.Popen[bytes]:
        limits = self._limits
        cpu_s = limits.cpu_s if limits.cpu_s is not None else max(1, math.ceil(timeout_s))
        preexec: Callable[[], None] | None = None
        if os.name == "posix":
            preexec = _limit_applier(
                cpu_s=cpu_s,
                memory_mb=memory_mb,
                nofile=limits.nofile,
                fsize_mb=limits.fsize_mb,
                language=language,
            )
        try:
            return subprocess.Popen(  # noqa: S603 - fixed interpreter and executor-owned script
                argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
                close_fds=True,
            )
        except OSError as exc:
            raise ExecutorUnavailable(
                f"cannot start {_LANGUAGE_LABELS[language]}: {exc.strerror or exc}"
            ) from exc

    def _collect(
        self, process: subprocess.Popen[bytes], timeout_s: float
    ) -> tuple[bytes, bytes, bool, bool]:
        cap = self._limits.max_output_bytes
        stdout, stderr = bytearray(), bytearray()
        truncated = threading.Event()
        if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE was passed
            raise ExecutorError("the child process has no output pipes")
        readers = [
            threading.Thread(
                target=_read_capped, args=(process.stdout, stdout, cap, truncated), daemon=True
            ),
            threading.Thread(
                target=_read_capped, args=(process.stderr, stderr, cap, truncated), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)
        finally:
            # Even after a normal exit: children the code left behind must not outlive the call,
            # and they keep the output pipes open (the reader threads wait for EOF).
            _kill_group(process.pid)
            for reader in readers:
                reader.join(timeout=5.0)
            for stream in (process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()
        return bytes(stdout), bytes(stderr), truncated.is_set(), timed_out
