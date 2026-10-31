"""Container code executor: agent code runs in a throwaway Docker sandbox, never on the worker host.

This is the production counterpart of `LocalExecutor` (docs/v4/13-executor.md §5,
docs/v4/02-architecture.md C5). It implements the same `Executor` protocol, so the v4 runtime does
not know where code runs; `botai.agent.v4.factory.build_executor` picks the implementation by
config and refuses the local one in `ENV=prod`.

What one call does (`Executor.run`):

- starts a **fresh container per call** from `image` (`docker run --rm`, unique name and labels),
  bind-mounting the task workspace at `/workspace`. Files survive between calls through the
  mount; the container itself never survives a call (see "Why per call" below);
- runs the code as the non-root uid/gid 10001 of the image, with a read-only root filesystem and
  a `tmpfs` `/tmp`, all Linux capabilities dropped and `no-new-privileges` set. The image ships
  Python 3.13, Node.js 22 and bash (ops/docker/sandbox.Dockerfile);
- passes only an explicit environment allowlist to the container: the image's `PATH`, `HOME` and
  the XDG dirs inside the workspace, `TMPDIR=/tmp`, the fixed locale/`NO_COLOR`/`PYTHON*` values
  and the embedder's sanitized `extra_env`. The worker's `BOTAI_*`, `DATABASE_URL`, KEK and
  provider keys are never passed; there is no docker socket inside the sandbox (the image has no
  docker CLI either);
- enforces cgroup limits on the container (`--memory`/`--memory-swap` with no swap,
  `--pids-limit`, `--cpus`) and POSIX rlimits inside it (`--ulimit cpu`, `nofile`, `fsize`), so
  the memory limit holds for every language, including Node.js (unlike the best-effort
  `RLIMIT_AS` of the local mode);
- enforces the wall timeout in the worker: on expiry the container is killed (`docker kill`,
  i.e. SIGKILL to its init and consequently all processes in its pid namespace) and removed, so
  the code and every child it spawned die — there is no detached survivor;
- caps stdout/stderr at `ExecLimits.max_output_bytes` per stream while draining the rest, and
  reports created files as a difference of host-side workspace snapshots, exactly like
  `LocalExecutor`;
- network is available (D-003: the agent is a full harness, not an egress filter).

Exit codes: the Docker CLI reports a process killed by signal N as 128+N (for example 137 for
SIGKILL and 152 for SIGXCPU), where `LocalExecutor` sees the negative wait status (-9/-24). The
`ExecResult` contract only requires a non-zero code for a failed/killed run; callers must not
depend on the sign for a killed process.

Why one container per call instead of one per run. The `Executor` limits are per call: a request
may narrow the timeout/memory of a single run, and the platform contract for a timeout is "the
container and its children are killed". A fresh container reproduces the exact semantics of
`LocalExecutor` (new pid namespace, no leftover processes, per-call cgroup values) while the
bind-mounted workspace provides the persistence the agent needs. The cost is container startup
per call (roughly 0.3-1 s on a warm image) and no warm package cache across calls, except what
code installs into the workspace or HOME — both live in the bind-mounted workspace.

What the container does NOT give (honest boundaries):

- the workspace is shared read-write with the worker, so code can read every task file of the
  run; a hostile program can also use the free network to send anywhere what it can read;
- container escape or a Docker/kernel bug is a residual risk; this is a two-person pet project
  and the sandbox is a boundary against accidents and casual misuse, not against a targeted
  attacker with kernel exploits;
- the sandbox is not on a rate limit: one call = one container, no warm pool.

Path mapping. The bind-mount source is the absolute path of the workspace **as the worker sees
it**, so the path must exist identically on the Docker host. In production the worker container
must bind-mount the host workspace root at the same path (for example
`/srv/botai/agent-workspaces:/srv/botai/agent-workspaces`) and use it as
`BOTAI_AGENT_WORKSPACE_ROOT`; this executor never guesses a path mapping. When a mapping is
unavoidable it has to be configured explicitly, not inferred.

Ownership. The sandbox runs as uid 10001, so the workspace must be writable by that uid. In
production the worker itself is uid 10001 and the host directory is owned by it, so nothing else is
needed. On a local dev host where the worker runs as a different user (or on CI), the workspace or
its parent has to be prepared for the sandbox uid first; otherwise the executor fails loudly on the
mount probe instead of running against a workspace the sandbox cannot write.
"""

# The container executor deliberately reuses the local executor's environment policy: the same
# names must be refused or passed in both modes, so a divergence would be a security regression.
# Those helpers are private to their module; the reuse is intentional, not accidental coupling.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import shlex
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from botai.agent.v4.executor.local import (
    _FIXED_ENV,
    _INHERITED_ENV,
    SCRIPT_NAMES,
    _sanitized_env,
)
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

DEFAULT_IMAGE = "ghcr.io/mattoyuzuru/botai-sandbox:latest"

# The workspace is always mounted at this fixed path inside the sandbox, so agent-visible paths
# do not change when the worker's own workspace root changes.
CONTAINER_WORKSPACE = "/workspace"
# User/group of the image; the workspace bind mount is owned by the same uid in production.
SANDBOX_UID = 10001
SANDBOX_GID = 10001
# Docker label on every container this executor starts; a deploy can sweep leftovers with
# `docker ps -aq --filter label=botai.agent.sandbox=1 | xargs -r docker rm -f`.
SANDBOX_LABEL = "botai.agent.sandbox"

_PREFLIGHT_TIMEOUT_S = 15.0
_CLEANUP_TIMEOUT_S = 15.0
_READ_CHUNK = 64 * 1024

# The image's own PATH and HOME: the sandbox never inherits the worker's filesystem layout.
_CONTAINER_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
_CONTAINER_TMP = "/tmp"  # noqa: S108 - path inside the sandbox container, not a host temp dir

_INTERPRETERS: dict[Language, str] = {"python": "python3", "js": "node", "bash": "bash"}

# `docker run` options that define the security boundary. They are constants of the executor, not
# settings: a deployer chooses where code runs, not how isolated it is.
_SANDBOX_FLAGS = (
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges:true",
    "--user",
    f"{SANDBOX_UID}:{SANDBOX_GID}",
)

_DAEMON_ERROR_MARKERS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "permission denied while trying to connect",
    "docker daemon is not running",
)


def _first_lines(data: bytes, limit: int = 3) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:limit])[:800]


def _looks_like_docker_failure(exit_code: int, stderr: bytes) -> bool:
    """True when the Docker CLI itself failed, not the code running in the container."""
    if exit_code != 125 or not stderr:
        return False
    text = stderr.decode("utf-8", errors="replace").lower()
    return text.startswith("docker:") or "error response from daemon" in text


class ContainerExecutor:
    """Runs agent code in a bind-mounted, resource-limited Docker sandbox (see module docstring).

    `docker` overrides the CLI path (a test seam); by default it is looked up in `PATH` and the
    failure is reported on the first call, not silently degraded to a host process. `docker_host`
    is passed to the Docker CLI as `DOCKER_HOST` when set (a unix socket path, `tcp://…` or an
    `ssh://` endpoint according to the Docker documentation).
    """

    def __init__(
        self,
        *,
        workspace: Workspace | None = None,
        limits: ExecLimits | None = None,
        image: str = DEFAULT_IMAGE,
        docker: str | None = None,
        docker_host: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        pids_limit: int = 128,
        cpus: float = 1.0,
        tmpfs_size_mb: int = 64,
    ) -> None:
        if not image:
            raise ValueError("the sandbox image name is required")
        if not 8 <= pids_limit <= 4096:
            raise ValueError("pids_limit must be 8..4096")
        if not 0.1 <= cpus <= 32:
            raise ValueError("cpus must be 0.1..32")
        if not 1 <= tmpfs_size_mb <= 4096:
            raise ValueError("tmpfs_size_mb must be 1..4096")
        self._limits = limits or ExecLimits()
        self._owns_workspace = workspace is None
        self._workspace = workspace or Workspace.create()
        self._image = image
        self._docker = docker or shutil.which("docker")
        self._docker_host = docker_host
        self._extra_env = _sanitized_env(extra_env)
        self._pids_limit = pids_limit
        self._cpus = cpus
        self._tmpfs_size_mb = tmpfs_size_mb
        # One workspace is one task: calls are serialised so two runs never write the same
        # workspace files (and their snapshots) at once.
        self._lock = asyncio.Lock()
        self._ready = False
        self._closed = False

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
        if self._closed:
            raise ExecutorUnavailable("the executor is closed")
        async with self._lock:
            return await self._run_locked(request)

    def close(self) -> None:
        """Release the executor. Idempotent; no container outlives a call, so this only drops a
        self-created workspace (a caller-provided one is kept, like `LocalExecutor`)."""
        if self._closed:
            return
        self._closed = True
        if self._owns_workspace:
            self._workspace.cleanup()

    # --- one call ---------------------------------------------------------------------------

    async def _run_locked(self, request: ExecRequest) -> ExecResult:
        limits, workspace = self._limits, self._workspace
        self._check_request(request)
        # Validate every destination before writing anything: a refused path must not leave a
        # half-written request in the workspace.
        for relative in request.files:
            workspace.resolve(relative)
        # The sandbox is a deployment dependency: fail before touching the workspace, never fall
        # back to a host process.
        await self._ensure_ready()
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
        script_container = self._container_path(script.relative_to(workspace.root))
        before = workspace.snapshot(limit=limits.max_files)
        started = time.monotonic()
        try:
            exit_code, stdout, stderr, truncated, timed_out = await self._exec(
                language=request.language,
                script=script_container,
                environment=environment,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
            )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        duration_ms = int((time.monotonic() - started) * 1000)
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

    def _environment(self, workspace: Workspace, language: Language) -> dict[str, str]:
        home = self._container_path(workspace.internal_dir("home").relative_to(workspace.root))
        environment: dict[str, str] = {
            "PATH": _CONTAINER_PATH,
            "HOME": home,
            "TMPDIR": _CONTAINER_TMP,
            "XDG_CACHE_HOME": f"{home}/.cache",
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_DATA_HOME": f"{home}/.local/share",
            **_FIXED_ENV,
            **self._extra_env,
        }
        for name in _INHERITED_ENV:
            value = os.environ.get(name)
            if value:
                environment.setdefault(name, value)
        if language == "python":
            # The agent can import a helper it wrote in an earlier call from the workspace cwd.
            environment["PYTHONPATH"] = CONTAINER_WORKSPACE
        return environment

    @staticmethod
    def _container_path(relative: Path) -> str:
        return f"{CONTAINER_WORKSPACE}/{relative.as_posix()}"

    # --- one Docker run ---------------------------------------------------------------------

    async def _exec(
        self,
        *,
        language: Language,
        script: str,
        environment: Mapping[str, str],
        timeout_s: float,
        memory_mb: int,
    ) -> tuple[int, bytes, bytes, bool, bool]:
        limits = self._limits
        name = f"botai-sandbox-{uuid.uuid4().hex}"
        cpu_s = limits.cpu_s if limits.cpu_s is not None else max(1, math.ceil(timeout_s))
        fsize = limits.fsize_mb * MEBIBYTE
        argv = [
            self._require_docker(),
            "run",
            "--rm",
            # A missing image is a deployment error, not something to pull silently mid-task.
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{SANDBOX_LABEL}=1",
            "--label",
            f"{SANDBOX_LABEL}.workspace={self._workspace_label()}",
            *_SANDBOX_FLAGS,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self._tmpfs_size_mb}m,mode=1777",  # noqa: S108 - tmpfs in the sandbox
            "--pids-limit",
            str(self._pids_limit),
            "--cpus",
            str(self._cpus),
            "--memory",
            f"{memory_mb}m",
            "--memory-swap",
            f"{memory_mb}m",
            "--ulimit",
            f"cpu={cpu_s}:{cpu_s + 1}",
            "--ulimit",
            f"nofile={limits.nofile}:{limits.nofile}",
            "--ulimit",
            f"fsize={fsize}:{fsize}",
            "--volume",
            f"{self._workspace.root}:{CONTAINER_WORKSPACE}:rw",
            "--workdir",
            CONTAINER_WORKSPACE,
        ]
        for key, value in environment.items():
            argv.extend(("--env", f"{key}={value}"))
        argv.append(self._image)
        argv.extend((_INTERPRETERS[language], script))

        process = await self._spawn_docker(argv)
        try:
            exit_code, stdout, stderr, truncated, timed_out = await self._collect(
                process, name, timeout_s
            )
            failure = self._docker_run_failure(exit_code, stderr, timed_out)
            if failure is not None:
                raise ExecutorUnavailable(failure)
        finally:
            await self._reap(process)
            await self._remove_container(name)
        return exit_code, stdout, stderr, truncated, timed_out

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        """Kill a Docker CLI process that is still running (never a container operation)."""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    async def _spawn_docker(self, argv: list[str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutorUnavailable(
                f"cannot start the Docker CLI {argv[0]!r}: {exc.strerror or exc}"
            ) from exc

    async def _collect(
        self, process: asyncio.subprocess.Process, name: str, timeout_s: float
    ) -> tuple[int, bytes, bytes, bool, bool]:
        """Drain both streams (capped), wait for the run, kill the container on timeout."""
        cap = self._limits.max_output_bytes
        stdout, stderr = bytearray(), bytearray()
        truncated = [False]
        stdout_stream, stderr_stream = process.stdout, process.stderr
        if stdout_stream is None or stderr_stream is None:  # pragma: no cover - PIPE was passed
            raise ExecutorError("the Docker CLI has no output pipes")
        readers = [
            asyncio.create_task(self._read_capped(stdout_stream, stdout, cap, truncated)),
            asyncio.create_task(self._read_capped(stderr_stream, stderr, cap, truncated)),
        ]
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                # SIGKILL to the container's init: every process in the sandbox dies with it,
                # including children that left the run's process group.
                await self._kill_container(name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            # The container is gone: its pipes reach EOF and the readers finish. A hung pipe must
            # not hold the call open, so cancel after a short grace period.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True), timeout=5.0
                )
            except TimeoutError:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
        exit_code = process.returncode if process.returncode is not None else -1
        return exit_code, bytes(stdout), bytes(stderr), truncated[0], timed_out

    @staticmethod
    async def _read_capped(
        stream: asyncio.StreamReader, sink: bytearray, cap: int, truncated: list[bool]
    ) -> None:
        """Read a pipe until EOF, keeping at most `cap` bytes; the rest is drained and dropped."""
        try:
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    return
                room = cap - len(sink)
                if room > 0:
                    sink.extend(chunk[:room])
                if len(chunk) > room:
                    truncated[0] = True
        except (OSError, ValueError):  # the pipe was closed when the container died
            return

    def _docker_run_failure(self, exit_code: int, stderr: bytes, timed_out: bool) -> str | None:
        """An actionable `ExecutorUnavailable` message for a Docker-level failure, if there is one."""
        if timed_out:
            return None  # our own kill: the exit code is the killed command's 128+SIGKILL
        text = stderr.decode("utf-8", errors="replace").lower()
        if any(marker in text for marker in _DAEMON_ERROR_MARKERS):
            return self._describe_preflight_failure(stderr)
        if _looks_like_docker_failure(exit_code, stderr):
            detail = _first_lines(stderr) or "no error output"
            return (
                f"the Docker CLI could not start the sandbox container ({detail}). Check that "
                f"the image {self._image!r} is available and the daemon is healthy; this "
                "executor never falls back to running code on the host."
            )
        return None

    # --- Docker plumbing --------------------------------------------------------------------

    def _workspace_label(self) -> str:
        """A stable, groupable id of the workspace path (safe as a Docker label value)."""
        digest = hashlib.sha256(str(self._workspace.root).encode("utf-8")).hexdigest()
        return digest[:16]

    def _subprocess_env(self) -> dict[str, str]:
        """The environment of the trusted Docker CLI process (never of the sandbox container).

        The CLI needs the worker's `HOME`/Docker context/`DOCKER_HOST` to reach the right daemon;
        the container gets only the explicit `--env` allowlist built in `_environment`.
        """
        environment = dict(os.environ)
        if self._docker_host:
            environment["DOCKER_HOST"] = self._docker_host
        return environment

    def _require_docker(self) -> str:
        if self._docker is None:
            raise ExecutorUnavailable(
                "the docker CLI is not available in PATH, so agent code cannot run in the "
                "sandbox. Install Docker on the worker host and make the CLI visible, or set "
                "BOTAI_SANDBOX_DOCKER_HOST to a reachable daemon; this executor never falls back "
                "to running code on the host."
            )
        return self._docker

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        self._require_docker()
        code, _, stderr = await self._docker_cmd(
            "image", "inspect", self._image, "--format", "{{.Id}}", timeout_s=_PREFLIGHT_TIMEOUT_S
        )
        if code != 0:
            raise ExecutorUnavailable(self._describe_preflight_failure(stderr))
        # One probe container per executor: proves that the Docker daemon bind-mounts the very
        # path the worker sees and that uid 10001 can write it. Without it a wrong host path in
        # production would silently give the sandbox an empty directory instead of the task files.
        await self._verify_workspace_mount()
        self._ready = True

    async def _verify_workspace_mount(self) -> None:
        probe_dir = self._workspace.internal_dir("sandbox")
        marker = probe_dir / "mount-id"
        token = uuid.uuid4().hex
        marker.write_text(token, encoding="utf-8")
        probe = probe_dir / "write-probe"
        probe.unlink(missing_ok=True)
        marker_inside = self._container_path(marker.relative_to(self._workspace.root))
        probe_inside = self._container_path(probe.relative_to(self._workspace.root))
        name = f"botai-sandbox-probe-{uuid.uuid4().hex}"
        check = (
            f"test -f {shlex.quote(marker_inside)} && cat {shlex.quote(marker_inside)} "
            f"&& touch {shlex.quote(probe_inside)} && echo BOTAI_MOUNT_OK"
        )
        argv = [
            self._require_docker(),
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{SANDBOX_LABEL}=1",
            "--label",
            f"{SANDBOX_LABEL}.workspace={self._workspace_label()}",
            *_SANDBOX_FLAGS,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self._tmpfs_size_mb}m,mode=1777",  # noqa: S108 - tmpfs in the sandbox
            "--network",
            "none",
            "--volume",
            f"{self._workspace.root}:{CONTAINER_WORKSPACE}:rw",
            "--workdir",
            CONTAINER_WORKSPACE,
            self._image,
            "sh",
            "-c",
            check,
        ]
        process = await self._spawn_docker(argv)
        try:
            code, stdout, stderr = await self._collect_process(
                process, timeout_s=_PREFLIGHT_TIMEOUT_S
            )
        finally:
            await self._reap(process)
            await self._remove_container(name)
        output = stdout.decode("utf-8", errors="replace")
        if code != 0 or token not in output or not probe.exists():
            detail = _first_lines(stderr) or _first_lines(stdout) or "no output"
            raise ExecutorUnavailable(
                "the sandbox cannot see and write the task workspace at "
                f"{str(self._workspace.root)!r} ({detail}). The Docker daemon must be able to "
                "bind-mount that absolute path: it has to exist identically on the Docker host "
                "and on the worker (set BOTAI_AGENT_WORKSPACE_ROOT to such a path in production; "
                "the executor never guesses a mapping). This executor never falls back to running "
                "code on the host."
            )
        probe.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)

    async def _collect_process(
        self, process: asyncio.subprocess.Process, *, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        """Wait for a Docker CLI command to finish and return its (exit code, stdout, stderr)."""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise ExecutorUnavailable(
                f"the Docker CLI did not answer within {timeout_s:g} s"
            ) from None
        code = process.returncode if process.returncode is not None else -1
        return code, stdout, stderr

    def _describe_preflight_failure(self, stderr: bytes) -> str:
        text = stderr.decode("utf-8", errors="replace")
        lower = text.lower()
        if any(marker in lower for marker in _DAEMON_ERROR_MARKERS):
            return (
                "the Docker daemon is not reachable, so agent code cannot run in the sandbox: "
                f"{_first_lines(stderr)}. Start Docker on the worker host (or fix "
                "BOTAI_SANDBOX_DOCKER_HOST); in production the worker container needs "
                "/var/run/docker.sock mounted and the worker user in the host docker group "
                "(compose group_add). This executor never falls back to running code on the host."
            )
        if "no such image" in lower or "not found" in lower:
            return (
                f"the sandbox image {self._image!r} is not present on this Docker host. Build and "
                "tag it: docker build -f ops/docker/sandbox.Dockerfile -t "
                f"{self._image} . ; in CI/deploy, push it to the registry and pull it explicitly "
                "(the executor uses docker run --pull never). This executor never falls back to "
                "running code on the host."
            )
        return (
            f"cannot inspect the sandbox image {self._image!r}: "
            f"{_first_lines(stderr) or 'no output from the Docker CLI'}"
        )

    async def _docker_cmd(
        self, *args: str, timeout_s: float = _CLEANUP_TIMEOUT_S
    ) -> tuple[int, bytes, bytes]:
        """Run one Docker CLI command to completion and return (exit code, stdout, stderr)."""
        process = await self._spawn_docker([self._require_docker(), *args])
        try:
            return await self._collect_process(process, timeout_s=timeout_s)
        finally:
            await self._reap(process)

    async def _kill_container(self, name: str) -> None:
        with contextlib.suppress(ExecutorUnavailable):
            await self._docker_cmd("kill", name, timeout_s=_CLEANUP_TIMEOUT_S)

    async def _remove_container(self, name: str) -> None:
        """Remove the container if it still exists. `--rm` normally beats us to it."""
        with contextlib.suppress(ExecutorUnavailable):
            await self._docker_cmd("rm", "--force", name, timeout_s=_CLEANUP_TIMEOUT_S)


__all__ = [
    "CONTAINER_WORKSPACE",
    "DEFAULT_IMAGE",
    "SANDBOX_GID",
    "SANDBOX_LABEL",
    "SANDBOX_UID",
    "ContainerExecutor",
]
