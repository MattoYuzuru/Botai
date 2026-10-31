"""The Docker container executor: real sandbox runs, limits, isolation, cleanup and loud failures.

Docker-gated: the tests that start a sandbox are skipped when the Docker CLI, the daemon or the
sandbox image is absent. Build the image locally with

    docker build -f ops/docker/sandbox.Dockerfile -t botai-sandbox:test .

The image under test is `BOTAI_TEST_SANDBOX_IMAGE`, else the real setting `BOTAI_SANDBOX_IMAGE`
(the CI job builds `botai-sandbox:ci` and exports it), else `botai-sandbox:test`.
The workspace used by the sandbox tests must be on a path the Docker daemon can bind-mount (on
macOS Colima/Docker Desktop only selected host directories are shared): `~/.cache/botai-v4-
sandbox-tests` by default, overridable with `BOTAI_TEST_SANDBOX_WORKSPACE_ROOT`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from botai.agent.v4.executor import (
    ContainerExecutor,
    ExecLimits,
    ExecRequest,
    Executor,
    ExecutorUnavailable,
    LocalExecutor,
    Workspace,
)
from botai.agent.v4.factory import AgentWiringError, build_executor
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.code import build_code_tools
from botai.settings import Settings
from tests.agent.v4.agent_wiring_support import fixture_settings

TEST_IMAGE = (
    os.environ.get("BOTAI_TEST_SANDBOX_IMAGE")
    or os.environ.get("BOTAI_SANDBOX_IMAGE")
    or "botai-sandbox:test"
)
DOCKER = shutil.which("docker")
MIB = 1024 * 1024
# One workspace root for the whole module; removed as root in a container so files created by the
# sandbox user (uid 10001) can be deleted on Linux CI too.
SHARED_ROOT = Path(
    os.environ.get("BOTAI_TEST_SANDBOX_WORKSPACE_ROOT")
    or (Path.home() / ".cache" / "botai-v4-sandbox-tests")
)


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    assert DOCKER is not None  # callers are docker-gated; this is the only subprocess seam
    return subprocess.run(  # noqa: S603 - fixed docker CLI and test-controlled arguments
        [DOCKER, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_skip_reason() -> str | None:
    if DOCKER is None:
        return "the docker CLI is not installed"
    try:
        info = _docker("info", "--format", "{{.ServerVersion}}")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment specific
        return f"the docker daemon is not reachable: {exc}"
    if info.returncode != 0:
        return f"the docker daemon is not reachable: {info.stderr.strip()[:200]}"
    image = _docker("image", "inspect", TEST_IMAGE)
    if image.returncode != 0:
        return (
            f"the sandbox image {TEST_IMAGE!r} is not built: "
            "docker build -f ops/docker/sandbox.Dockerfile -t botai-sandbox:test ."
        )
    return None


DOCKER_SKIP_REASON = _docker_skip_reason()
requires_docker = pytest.mark.skipif(
    DOCKER_SKIP_REASON is not None, reason=DOCKER_SKIP_REASON or "docker is required"
)


@pytest.fixture(scope="module")
def sandbox_root() -> Iterator[Path]:
    """A daemon-visible root for sandbox workspaces, cleaned as root after the module."""
    if DOCKER_SKIP_REASON is not None:  # pragma: no cover - tests using it are skipped anyway
        pytest.skip(DOCKER_SKIP_REASON)
    root = SHARED_ROOT / f"pytest-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        cleanup = _docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--volume",
            f"{root}:/cleanup",
            TEST_IMAGE,
            "sh",
            "-c",
            "rm -rf /cleanup/*",
        )
        if cleanup.returncode != 0:  # pragma: no cover - best effort, the host can still try
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _prepare_for_sandbox(workspace: Workspace) -> None:
    """Let the non-root sandbox uid write the workspace and its executor plumbing.

    In production the worker runs as uid 10001, the same user as the sandbox, so this is implicit.
    On a Linux CI/dev host pytest runs as another user: its directories are 0755/0700 and uid 10001
    could not create the probe file or HOME. The directories are chmodded once here; files created
    later by the sandbox are owned by uid 10001 and only the module-root cleanup (as root in a
    container) removes them.
    """
    workspace.root.chmod(0o777)
    for parts in (("runs",), ("sandbox",), ("home",)):
        workspace.internal_dir(*parts).chmod(0o777)


def _workspace(sandbox_root: Path, name: str = "task") -> Workspace:
    workspace = Workspace.create(sandbox_root / f"{name}-{uuid.uuid4().hex[:8]}")
    _prepare_for_sandbox(workspace)
    return workspace


@pytest.fixture
def workspace(sandbox_root: Path) -> Workspace:
    """A caller-owned workspace on a path the Docker daemon can bind-mount."""
    return _workspace(sandbox_root)


def _python(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="python", code=code, **kwargs)  # type: ignore[arg-type]


def _js(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="js", code=code, **kwargs)  # type: ignore[arg-type]


def _bash(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="bash", code=code, **kwargs)  # type: ignore[arg-type]


def _workspace_label(workspace: Workspace) -> str:
    digest = hashlib.sha256(str(workspace.root).encode("utf-8")).hexdigest()
    return digest[:16]


def _containers_for(workspace: Workspace) -> list[str]:
    result = _docker(
        "ps",
        "-aq",
        "--filter",
        f"label=botai.agent.sandbox.workspace={_workspace_label(workspace)}",
    )
    return result.stdout.split()


# --- languages and results --------------------------------------------------------------------


@requires_docker
async def test_python_prints_and_reports_the_files_it_created(workspace: Workspace) -> None:
    code = (
        "from pathlib import Path\n"
        "Path('out').mkdir()\n"
        "Path('out/data.txt').write_text('ok', encoding='utf-8')\n"
        "print('привет', 40 + 2)\n"
    )
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_python(code))
    finally:
        executor.close()

    assert result.ok
    assert not result.truncated
    assert not result.timed_out
    assert result.stdout == "привет 42\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.duration_ms >= 0
    assert result.files == ["out/data.txt"]
    assert _containers_for(workspace) == [], "a finished call must remove its container"


@requires_docker
async def test_python_failure_keeps_the_traceback_in_stderr(workspace: Workspace) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_python("raise ValueError('boom-fixture')"))
    finally:
        executor.close()

    assert not result.ok
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "ValueError: boom-fixture" in result.stderr


@requires_docker
async def test_bash_runs_and_reports_its_exit_code(workspace: Workspace) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_bash("echo from-bash; echo fixture-err >&2; exit 3"))
    finally:
        executor.close()

    assert result.exit_code == 3
    assert not result.ok
    assert result.stdout == "from-bash\n"
    assert result.stderr == "fixture-err\n"


@requires_docker
async def test_js_runs_inside_the_image(workspace: Workspace) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_js("console.log(2 + 2)"))
    finally:
        executor.close()

    assert result.ok
    assert result.stdout == "4\n"


# --- sandbox boundary -------------------------------------------------------------------------


@requires_docker
async def test_the_sandbox_is_non_root_read_only_and_has_a_writable_tmp(
    workspace: Workspace,
) -> None:
    code = (
        "import os, pathlib\n"
        "print(os.getuid(), os.getgid())\n"
        "try:\n"
        "    pathlib.Path('/botai-root-probe').write_text('x')\n"
        "except OSError:\n"
        "    print('root-read-only')\n"
        "else:\n"
        "    print('root-writable')\n"
        "pathlib.Path('/tmp/probe.txt').write_text('ok')\n"
        "print(pathlib.Path('/tmp/probe.txt').read_text())\n"
    )
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_python(code))
    finally:
        executor.close()

    assert result.ok
    lines = result.stdout.splitlines()
    assert lines[0] == "10001 10001"
    assert lines[1] == "root-read-only"
    assert lines[2] == "ok"


@requires_docker
async def test_worker_environment_secrets_and_the_docker_socket_are_not_visible(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTAI_EXECUTOR_FIXTURE", "top-secret-fixture")
    monkeypatch.setenv("BOTAI_KEK", "kek-fixture-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fixture-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fixture")
    code = (
        "import json, os, shutil\n"
        "print(json.dumps({\n"
        "    'forbidden': {name: name in os.environ for name in (\n"
        "        'BOTAI_EXECUTOR_FIXTURE', 'BOTAI_KEK', 'DATABASE_URL',\n"
        "        'DEEPSEEK_API_KEY', 'OPENAI_API_KEY')},\n"
        "    'home': os.environ.get('HOME'),\n"
        "    'tmpdir': os.environ.get('TMPDIR'),\n"
        "    'docker_socket': os.path.exists('/var/run/docker.sock'),\n"
        "    'docker_cli': shutil.which('docker'),\n"
        "    'cwd': os.getcwd(),\n"
        "}))\n"
    )
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_python(code))
    finally:
        executor.close()

    assert result.ok
    seen = json.loads(result.stdout)
    assert seen["forbidden"] == {
        "BOTAI_EXECUTOR_FIXTURE": False,
        "BOTAI_KEK": False,
        "DATABASE_URL": False,
        "DEEPSEEK_API_KEY": False,
        "OPENAI_API_KEY": False,
    }
    assert "fixture-secret" not in result.stdout + result.stderr
    assert seen["docker_socket"] is False
    assert seen["docker_cli"] is None
    assert seen["cwd"] == "/workspace"
    assert seen["home"].startswith("/workspace/")
    assert seen["tmpdir"] == "/tmp"


@requires_docker
async def test_the_sandbox_has_network_configured(workspace: Workspace) -> None:
    # D-003: the sandbox is a free harness, not an egress filter. The container runs on Docker's
    # default bridge (no --network none): an interface and a default route must exist. Whether the
    # host itself has egress is a deployment concern and is not asserted here.
    code = (
        "import pathlib\n"
        "interfaces = [p.name for p in pathlib.Path('/sys/class/net').iterdir()]\n"
        "routes = pathlib.Path('/proc/net/route').read_text().splitlines()[1:]\n"
        "default = [line for line in routes if line.split()[1] == '00000000']\n"
        "print('eth0' in interfaces, bool(default))\n"
    )
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(_python(code))
    finally:
        executor.close()

    assert result.ok
    assert result.stdout.strip() == "True True"


@requires_docker
async def test_extra_env_is_passed_to_the_code(workspace: Workspace) -> None:
    executor = ContainerExecutor(
        workspace=workspace, image=TEST_IMAGE, extra_env={"MY_ALLOWED_FLAG": "on"}
    )
    try:
        result = await executor.run(_python("import os\nprint(os.environ.get('MY_ALLOWED_FLAG'))"))
    finally:
        executor.close()

    assert result.ok
    assert result.stdout == "on\n"


@pytest.mark.parametrize(
    "name", ["BOTAI_SECRET_FIXTURE", "DATABASE_URL", "OPENAI_API_KEY", "MY_PASSWORD", "KEK_ID"]
)
def test_secret_like_extra_env_is_refused(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="secret or a backend setting"):
        ContainerExecutor(
            workspace=Workspace.create(tmp_path / "task"),
            image=TEST_IMAGE,
            extra_env={name: "value"},
        )


@pytest.mark.parametrize("name", ["HOME", "PATH", "PYTHONPATH", "LD_PRELOAD", "NODE_OPTIONS"])
def test_executor_owned_env_names_cannot_be_overridden(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="controlled by the executor"):
        ContainerExecutor(
            workspace=Workspace.create(tmp_path / "task"),
            image=TEST_IMAGE,
            extra_env={name: "/the/worker/home"},
        )


# --- workspace files and persistence ----------------------------------------------------------


@requires_docker
async def test_workspace_files_persist_between_calls(workspace: Workspace) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        first = await executor.run(
            _python(
                "from pathlib import Path\n"
                "Path('notes').mkdir()\n"
                "Path('notes/a.txt').write_text('kept', encoding='utf-8')\n"
            )
        )
        second = await executor.run(
            _python(
                "from pathlib import Path\n"
                "print(Path('notes/a.txt').read_text(encoding='utf-8'))\n"
                "Path('b.txt').write_text('new', encoding='utf-8')\n"
            )
        )
    finally:
        executor.close()

    assert first.files == ["notes/a.txt"]
    assert second.ok
    assert second.stdout == "kept\n"
    assert second.files == ["b.txt"]
    assert workspace.list_files() == ["b.txt", "notes/a.txt"]


@requires_docker
async def test_request_files_are_written_before_the_run(workspace: Workspace) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        result = await executor.run(
            _python(
                "print(open('input.txt', encoding='utf-8').read())",
                files={"input.txt": "payload"},
            )
        )
    finally:
        executor.close()

    assert result.ok
    assert result.stdout == "payload\n"
    assert result.files == [], "request files were provided, not created by the run"


@requires_docker
async def test_the_run_starts_with_no_processes_from_previous_calls(
    workspace: Workspace,
) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        first = await executor.run(_bash("sleep 9971 >/dev/null 2>&1 & echo started"))
        second = await executor.run(_python("import glob\nprint(len(glob.glob('/proc/[0-9]*')))"))
    finally:
        executor.close()

    assert first.ok
    assert second.ok
    # A fresh pid namespace: the interpreter is the only process in the container.
    assert int(second.stdout) <= 2, "a background process survived into the next call"


@requires_docker
async def test_the_agent_code_exec_tool_uses_the_sandbox_end_to_end(workspace: Workspace) -> None:
    """Acceptance path: a real `code_exec` tool call creates a file and a later call reads it."""
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    tool = build_code_tools(executor)[0]
    context = ToolContext(run_id="01M68Z000000000000000000A0", workspace=workspace.root)
    try:
        first = await tool.run(
            {
                "language": "python",
                "code": (
                    "from pathlib import Path\n"
                    "Path('schedule.json').write_text('{\"rows\": 3}', encoding='utf-8')\n"
                    "print('saved')\n"
                ),
            },
            context,
        )
        second = await tool.run(
            {
                "language": "python",
                "code": ("import json\nprint(json.load(open('schedule.json'))['rows'])\n"),
            },
            context,
        )
    finally:
        executor.close()

    assert tool.spec.name == "code_exec"
    assert first.ok
    assert first.data["stdout"] == "saved\n"
    assert first.data["files"] == ["schedule.json"]
    assert second.ok
    assert second.data["stdout"] == "3\n"


# --- limits -----------------------------------------------------------------------------------


@requires_docker
async def test_timeout_kills_the_container_and_every_child(workspace: Workspace) -> None:
    limits = ExecLimits(timeout_s=1.5)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        result = await executor.run(_bash("sleep 9971 & wait"))
        # The executor is reusable after a killed call.
        follow_up = await executor.run(_python("print('alive')"))
    finally:
        executor.close()

    assert result.timed_out
    assert not result.ok
    assert result.exit_code != 0
    assert result.duration_ms < 8000, "a timeout must not wait for the container to finish"
    assert _containers_for(workspace) == [], "a timed-out container is killed and removed"
    assert follow_up.ok
    assert follow_up.stdout == "alive\n"


@requires_docker
async def test_timeout_kills_a_child_that_left_the_process_group(workspace: Workspace) -> None:
    limits = ExecLimits(timeout_s=1.5)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        # setsid detaches the child from the run's process group; only killing the whole container
        # (and with it the pid namespace) is guaranteed to end it.
        result = await executor.run(_bash("setsid sleep 9971 >/dev/null 2>&1 & wait"))
    finally:
        executor.close()

    assert result.timed_out
    assert _containers_for(workspace) == []


@requires_docker
async def test_a_request_cannot_raise_the_executor_timeout_limit(workspace: Workspace) -> None:
    limits = ExecLimits(timeout_s=1.5)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        result = await executor.run(_bash("sleep 9971 & wait", timeout_s=300))
    finally:
        executor.close()

    assert result.timed_out
    assert result.duration_ms < 8000


@requires_docker
async def test_cgroup_memory_limit_is_visible_and_enforced(workspace: Workspace) -> None:
    limits = ExecLimits(memory_mb=128)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        probe = await executor.run(
            _python(
                "import pathlib\n"
                "for path in ('/sys/fs/cgroup/memory.max',\n"
                "             '/sys/fs/cgroup/memory/memory.limit_in_bytes'):\n"
                "    file = pathlib.Path(path)\n"
                "    if file.exists():\n"
                "        print(file.read_text().strip())\n"
                "        break\n"
                "else:\n"
                "    print('NO_CGROUP_LIMIT')\n"
            )
        )
        if probe.stdout.strip() == "NO_CGROUP_LIMIT":  # pragma: no cover - cgroup v1/v2 hosts
            pytest.skip("this Docker daemon does not expose cgroup memory limits")
        allocation = await executor.run(
            _python("data = bytearray(512 * 1024 * 1024)\nprint(len(data))")
        )
    finally:
        executor.close()

    assert int(probe.stdout) == 128 * MIB
    assert not allocation.ok, "a 512 MiB allocation must not survive a 128 MiB cgroup"


@requires_docker
async def test_pids_limit_is_visible_and_enforced(workspace: Workspace) -> None:
    limits = ExecLimits(timeout_s=20)
    executor = ContainerExecutor(
        workspace=workspace, image=TEST_IMAGE, limits=limits, pids_limit=16
    )
    try:
        probe = await executor.run(
            _python(
                "import pathlib\n"
                "for path in ('/sys/fs/cgroup/pids.max',\n"
                "             '/sys/fs/cgroup/pids/pids.max'):\n"
                "    file = pathlib.Path(path)\n"
                "    if file.exists():\n"
                "        print(file.read_text().strip())\n"
                "        break\n"
                "else:\n"
                "    print('NO_CGROUP_LIMIT')\n"
            )
        )
        if probe.stdout.strip() == "NO_CGROUP_LIMIT":  # pragma: no cover - cgroup v1/v2 hosts
            pytest.skip("this Docker daemon does not expose cgroup pids limits")
        fork_storm = await executor.run(
            _python(
                "import os, time\n"
                "spawned = 0\n"
                "while spawned < 100:\n"
                "    try:\n"
                "        pid = os.fork()\n"
                "    except OSError:\n"
                "        break\n"
                "    if pid == 0:\n"
                "        time.sleep(2)\n"
                "        os._exit(0)\n"
                "    spawned += 1\n"
                "print(spawned)\n"
            )
        )
    finally:
        executor.close()

    assert probe.stdout.strip() == "16"
    assert fork_storm.ok
    assert 0 < int(fork_storm.stdout) < 16, "the pids cgroup must stop the fork storm"


@requires_docker
async def test_cpu_ulimit_terminates_a_runaway_loop(workspace: Workspace) -> None:
    limits = ExecLimits(cpu_s=1, timeout_s=30)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        result = await executor.run(
            _python(
                "import resource\n"
                "print(resource.getrlimit(resource.RLIMIT_CPU)[0])\n"
                "while True:\n"
                "    pass\n"
            )
        )
    finally:
        executor.close()

    assert not result.timed_out
    assert result.exit_code != 0
    assert int(result.stdout.splitlines()[0]) == 1
    assert result.duration_ms < 15_000


@requires_docker
async def test_output_is_capped_and_the_rest_is_drained(workspace: Workspace) -> None:
    limits = ExecLimits(max_output_bytes=1024)
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE, limits=limits)
    try:
        result = await executor.run(
            _python("import sys\nprint('o' * 200000)\nprint('e' * 200000, file=sys.stderr)\n")
        )
    finally:
        executor.close()

    assert result.ok  # the process is not killed by its own output
    assert result.truncated
    assert len(result.stdout) == 1024
    assert len(result.stderr) == 1024


# --- isolation between runs -------------------------------------------------------------------


@requires_docker
async def test_parallel_runs_are_isolated(sandbox_root: Path) -> None:
    first_workspace = _workspace(sandbox_root, "parallel-a")
    second_workspace = _workspace(sandbox_root, "parallel-b")
    first = ContainerExecutor(
        workspace=first_workspace, image=TEST_IMAGE, extra_env={"RUN_MARK": "first"}
    )
    second = ContainerExecutor(
        workspace=second_workspace, image=TEST_IMAGE, extra_env={"RUN_MARK": "second"}
    )
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "Path('run.txt').write_text(os.environ['RUN_MARK'])\n"
        "print(os.environ['RUN_MARK'])\n"
    )
    try:
        first_result, second_result = await asyncio.gather(
            first.run(_python(code)), second.run(_python(code))
        )
    finally:
        first.close()
        second.close()

    assert first_result.ok
    assert second_result.ok
    assert first_result.stdout == "first\n"
    assert second_result.stdout == "second\n"
    assert first_result.files == ["run.txt"]
    assert second_result.files == ["run.txt"]
    assert first_workspace.read_text("run.txt") == "first"
    assert second_workspace.read_text("run.txt") == "second"


# --- lifecycle, protocol, loud failures -------------------------------------------------------


@requires_docker
async def test_close_leaves_no_container_and_keeps_a_caller_owned_workspace(
    workspace: Workspace,
) -> None:
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    assert (await executor.run(_python("print(1)"))).ok
    executor.close()
    executor.close()  # idempotent

    assert _containers_for(workspace) == []
    assert workspace.root.is_dir(), "a caller-owned workspace is not removed by close()"
    with pytest.raises(ExecutorUnavailable, match="closed"):
        await executor.run(_python("print(1)"))


def test_close_cleans_a_self_created_workspace() -> None:
    executor = ContainerExecutor(image=TEST_IMAGE)  # would create its own temp workspace
    root = executor.workspace.root
    assert root.is_dir()

    executor.close()
    executor.close()  # idempotent

    assert not root.exists()
    with pytest.raises(ExecutorUnavailable, match="closed"):
        asyncio.run(executor.run(_python("print(1)")))


def test_container_executor_implements_the_executor_protocol(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)

    assert isinstance(executor, Executor)
    assert executor.workspace is workspace
    assert isinstance(executor.limits, ExecLimits)
    assert executor.image == TEST_IMAGE


def test_a_missing_docker_cli_is_reported_loudly(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = ContainerExecutor(
        workspace=workspace, image=TEST_IMAGE, docker="/nonexistent/docker-fixture"
    )
    with pytest.raises(ExecutorUnavailable, match="Docker CLI"):
        asyncio.run(executor.run(_python("print(1)")))


@pytest.mark.skipif(DOCKER is None, reason="the docker CLI is not installed")
def test_an_unreachable_daemon_is_reported_loudly(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = ContainerExecutor(
        workspace=workspace,
        image=TEST_IMAGE,
        docker_host="unix:///nonexistent/botai-docker-daemon-fixture.sock",
    )
    with pytest.raises(ExecutorUnavailable, match=r"daemon|Docker"):
        asyncio.run(executor.run(_python("print(1)")))


@requires_docker
def test_a_missing_image_is_reported_loudly(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = ContainerExecutor(workspace=workspace, image="botai-sandbox-missing-fixture:test")
    with pytest.raises(ExecutorUnavailable, match="not present"):
        asyncio.run(executor.run(_python("print(1)")))


@pytest.mark.skipif(sys.platform != "darwin", reason="only macOS daemons restrict shared paths")
@requires_docker
async def test_a_workspace_path_the_daemon_cannot_mount_fails_loudly(tmp_path: Path) -> None:
    # On macOS Colima/Docker Desktop the daemon sees only selected host directories: a workspace
    # in /private/var/folders is invisible to it. The executor must say so instead of silently
    # running against an empty directory.
    workspace = Workspace.create(tmp_path / "not-shared")
    executor = ContainerExecutor(workspace=workspace, image=TEST_IMAGE)
    try:
        with pytest.raises(ExecutorUnavailable, match="workspace"):
            await executor.run(_python("print(1)"))
    finally:
        executor.close()

    # The failed preflight container is labelled and removed: no residue after a failure.
    assert _containers_for(workspace) == []


# --- wiring by config -------------------------------------------------------------------------


def test_dev_defaults_to_the_local_executor(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = build_executor(fixture_settings(agent_executor="auto"), workspace)
    assert isinstance(executor, LocalExecutor)


def test_dev_can_opt_into_the_container_executor(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = build_executor(
        fixture_settings(agent_executor="container", botai_sandbox_image="image:fixture"),
        workspace,
    )
    assert isinstance(executor, ContainerExecutor)
    assert executor.image == "image:fixture"


def test_prod_selects_the_container_executor(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    executor = build_executor(
        fixture_settings(
            env="prod",
            botai_sandbox_image="image:fixture",
            botai_sandbox_docker_host="unix:///run/fixture.sock",
        ),
        workspace,
    )
    assert isinstance(executor, ContainerExecutor)
    assert executor.image == "image:fixture"


def test_prod_refuses_the_local_executor_loudly(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    with pytest.raises(AgentWiringError, match="ENV=prod"):
        build_executor(fixture_settings(env="prod", agent_executor="local"), workspace)


def test_sandbox_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOTAI_AGENT_EXECUTOR", "container")
    monkeypatch.setenv("BOTAI_SANDBOX_IMAGE", "example/sandbox:fixture")
    monkeypatch.setenv("BOTAI_SANDBOX_DOCKER_HOST", "unix:///run/fixture.sock")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.agent_executor == "container"
    assert settings.botai_sandbox_image == "example/sandbox:fixture"
    assert settings.botai_sandbox_docker_host == "unix:///run/fixture.sock"


def test_sandbox_settings_have_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BOTAI_AGENT_EXECUTOR",
        "BOTAI_SANDBOX_IMAGE",
        "BOTAI_SANDBOX_DOCKER_HOST",
        "BOTAI_AGENT_WORKSPACE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_executor == "auto"
    assert settings.botai_sandbox_image == "ghcr.io/mattoyuzuru/botai-sandbox:latest"
    assert settings.botai_sandbox_docker_host is None


def test_invalid_sandbox_parameters_are_rejected(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "task")
    with pytest.raises(ValueError, match="pids_limit"):
        ContainerExecutor(workspace=workspace, pids_limit=2)
    with pytest.raises(ValueError, match="cpus"):
        ContainerExecutor(workspace=workspace, cpus=0)
    with pytest.raises(ValueError, match="tmpfs_size_mb"):
        ContainerExecutor(workspace=workspace, tmpfs_size_mb=0)
    with pytest.raises(ValueError, match="image"):
        ContainerExecutor(workspace=workspace, image="")
