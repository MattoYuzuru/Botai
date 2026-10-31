"""The local code executor: languages, POSIX limits, timeout, env isolation, workspace, paths."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from botai.agent.v4.executor import (
    INTERNAL_DIR,
    ExecLimits,
    ExecRequest,
    Executor,
    ExecutorError,
    ExecutorUnavailable,
    LocalExecutor,
    Workspace,
    WorkspacePathError,
)

NODE = shutil.which("node")
PGREP = shutil.which("pgrep")
MIB = 1024 * 1024


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A caller-owned workspace inside the test's temporary directory."""
    return Workspace.create(tmp_path / "task")


def python(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="python", code=code, **kwargs)  # type: ignore[arg-type]


def js(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="js", code=code, **kwargs)  # type: ignore[arg-type]


def bash(code: str, **kwargs: object) -> ExecRequest:
    return ExecRequest(language="bash", code=code, **kwargs)  # type: ignore[arg-type]


# --- languages --------------------------------------------------------------------------------


async def test_python_prints_and_reports_the_files_it_created(workspace: Workspace) -> None:
    code = (
        "from pathlib import Path\n"
        "Path('out').mkdir()\n"
        "Path('out/data.txt').write_text('ok', encoding='utf-8')\n"
        "print('привет', 40 + 2)\n"
    )
    result = await LocalExecutor(workspace=workspace).run(python(code))

    assert result.ok
    assert not result.truncated
    assert not result.timed_out
    assert result.stdout == "привет 42\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.duration_ms >= 0
    assert result.files == ["out/data.txt"]


async def test_python_failure_keeps_the_traceback_in_stderr(workspace: Workspace) -> None:
    result = await LocalExecutor(workspace=workspace).run(
        python("raise ValueError('boom-fixture')")
    )

    assert not result.ok
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "ValueError: boom-fixture" in result.stderr


async def test_bash_runs_and_reports_its_exit_code(workspace: Workspace) -> None:
    result = await LocalExecutor(workspace=workspace).run(
        bash("echo from-bash; echo fixture-err >&2; exit 3")
    )

    assert result.exit_code == 3
    assert not result.ok
    assert result.stdout == "from-bash\n"
    assert result.stderr == "fixture-err\n"


@pytest.mark.skipif(NODE is None, reason="node.js is not installed")
async def test_js_runs_when_node_is_available(workspace: Workspace) -> None:
    result = await LocalExecutor(workspace=workspace).run(js("console.log(2 + 2)"))

    assert result.ok
    assert result.stdout == "4\n"


@pytest.mark.skipif(NODE is None, reason="node.js is not installed")
async def test_js_failure_reports_a_nonzero_exit(workspace: Workspace) -> None:
    result = await LocalExecutor(workspace=workspace).run(js("throw new Error('js-boom')"))

    assert result.exit_code != 0
    assert "js-boom" in result.stderr


async def test_a_missing_runtime_is_reported_not_ignored(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace, node="/nonexistent/node-fixture")
    with pytest.raises(ExecutorUnavailable, match="is not available"):
        await executor.run(js("console.log(1)"))


# --- limits: timeout, CPU, memory, POSIX values ------------------------------------------------


async def test_timeout_kills_the_whole_process_group(workspace: Workspace) -> None:
    limits = ExecLimits(timeout_s=0.5)
    result = await LocalExecutor(workspace=workspace, limits=limits).run(bash("sleep 9971 & wait"))

    assert result.timed_out
    assert not result.ok
    assert result.exit_code != 0
    assert result.duration_ms < 5000, "a timeout must not wait for the language runtime to finish"
    if PGREP is not None:
        await asyncio.sleep(0.3)
        leftover = await asyncio.to_thread(
            subprocess.run,
            [PGREP, "-f", "sleep 9971"],
            capture_output=True,
            text=True,
        )
        assert leftover.returncode != 0, leftover.stdout


async def test_cpu_limit_terminates_a_runaway_loop(workspace: Workspace) -> None:
    # The wall timeout is long; only RLIMIT_CPU can stop this run.
    limits = ExecLimits(cpu_s=1, timeout_s=20)
    result = await LocalExecutor(workspace=workspace, limits=limits).run(
        python("while True:\n    pass")
    )

    assert not result.timed_out
    assert result.exit_code == -signal.SIGXCPU


async def test_posix_limits_are_applied_inside_the_child(workspace: Workspace) -> None:
    code = (
        "import json, resource\n"
        "print(json.dumps({\n"
        "    'cpu': resource.getrlimit(resource.RLIMIT_CPU)[0],\n"
        "    'nofile': resource.getrlimit(resource.RLIMIT_NOFILE)[0],\n"
        "    'as': resource.getrlimit(resource.RLIMIT_AS)[0],\n"
        "}))\n"
    )
    limits = ExecLimits(memory_mb=128, nofile=128)
    result = await LocalExecutor(workspace=workspace, limits=limits).run(python(code))

    assert result.ok
    applied = json.loads(result.stdout)
    assert applied["nofile"] == 128
    assert applied["cpu"] >= 1
    if sys.platform.startswith("linux"):
        assert applied["as"] == 128 * MIB


async def test_a_request_cannot_raise_the_executor_timeout_limit(workspace: Workspace) -> None:
    # The executor caps the whole task at 0.5 s; the request asks for 60 s and must be clamped.
    limits = ExecLimits(timeout_s=0.5)
    result = await LocalExecutor(workspace=workspace, limits=limits).run(
        bash("sleep 9971 & wait", timeout_s=60)
    )

    assert result.timed_out
    assert result.duration_ms < 5000


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="macOS refuses RLIMIT_AS/RLIMIT_DATA, memory limits need Linux or the container",
)
async def test_a_request_cannot_raise_the_executor_memory_limit(workspace: Workspace) -> None:
    limits = ExecLimits(memory_mb=128)
    code = "import resource\nprint(resource.getrlimit(resource.RLIMIT_AS)[0])"
    result = await LocalExecutor(workspace=workspace, limits=limits).run(
        python(code, memory_mb=4096)
    )

    assert result.ok
    assert int(result.stdout) == 128 * MIB


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="macOS refuses RLIMIT_AS/RLIMIT_DATA, memory limits need Linux or the container",
)
async def test_memory_limit_stops_a_large_allocation(workspace: Workspace) -> None:
    limits = ExecLimits(memory_mb=128)
    result = await LocalExecutor(workspace=workspace, limits=limits).run(
        python("data = bytearray(512 * 1024 * 1024)\nprint(len(data))")
    )

    assert not result.ok
    assert result.exit_code != 0


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="invalid executor limits"):
        ExecLimits(memory_mb=1)
    with pytest.raises(ValueError, match="invalid executor limits"):
        ExecLimits(max_output_bytes=10)


def test_requests_are_validated() -> None:
    with pytest.raises(ValidationError):
        ExecRequest(language="ruby", code="puts 1")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ExecRequest(language="python", code="")


# --- environment isolation ---------------------------------------------------------------------


async def test_parent_environment_and_secrets_are_not_visible(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTAI_EXECUTOR_FIXTURE", "top-secret-fixture")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fixture-secret")
    code = (
        "import json, os\n"
        "print(json.dumps({name: name in os.environ for name in (\n"
        "    'BOTAI_EXECUTOR_FIXTURE', 'DATABASE_URL', 'DEEPSEEK_API_KEY')}))\n"
        "print(os.environ.get('PATH') is not None, os.environ.get('HOME', ''))\n"
    )
    result = await LocalExecutor(workspace=workspace).run(python(code))

    assert result.ok
    seen = json.loads(result.stdout.splitlines()[0])
    assert seen == {
        "BOTAI_EXECUTOR_FIXTURE": False,
        "DATABASE_URL": False,
        "DEEPSEEK_API_KEY": False,
    }
    assert "top-secret-fixture" not in result.stdout + result.stderr
    assert "fixture-secret" not in result.stdout + result.stderr
    has_path, home = result.stdout.splitlines()[1].split(" ", 1)
    assert has_path == "True"
    assert home.startswith(str(workspace.root))


@pytest.mark.parametrize(
    "name",
    ["BOTAI_SECRET_FIXTURE", "DATABASE_URL", "OPENAI_API_KEY", "MY_PASSWORD", "KEK_ID"],
)
def test_secret_like_extra_env_is_refused(workspace: Workspace, name: str) -> None:
    with pytest.raises(ValueError, match="secret or a backend setting"):
        LocalExecutor(workspace=workspace, extra_env={name: "value"})


@pytest.mark.parametrize("name", ["HOME", "PATH", "PYTHONPATH", "LD_PRELOAD", "NODE_OPTIONS"])
def test_executor_owned_env_names_cannot_be_overridden(workspace: Workspace, name: str) -> None:
    with pytest.raises(ValueError, match="controlled by the executor"):
        LocalExecutor(workspace=workspace, extra_env={name: "/the/worker/home"})


async def test_extra_env_is_passed_to_the_code(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace, extra_env={"MY_ALLOWED_FLAG": "on"})
    result = await executor.run(python("import os\nprint(os.environ.get('MY_ALLOWED_FLAG'))"))

    assert result.ok
    assert result.stdout == "on\n"


# --- workspace files and paths -----------------------------------------------------------------


async def test_workspace_files_persist_between_calls(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace)
    first = await executor.run(
        python(
            "from pathlib import Path\n"
            "Path('notes').mkdir()\n"
            "Path('notes/a.txt').write_text('kept', encoding='utf-8')\n"
        )
    )
    second = await executor.run(
        python(
            "from pathlib import Path\n"
            "print(Path('notes/a.txt').read_text(encoding='utf-8'))\n"
            "Path('b.txt').write_text('new', encoding='utf-8')\n"
        )
    )

    assert first.files == ["notes/a.txt"]
    assert second.ok
    assert second.stdout == "kept\n"
    assert second.files == ["b.txt"]  # only what this call created
    assert workspace.list_files() == ["b.txt", "notes/a.txt"]


async def test_request_files_are_written_before_the_run(workspace: Workspace) -> None:
    result = await LocalExecutor(workspace=workspace).run(
        python(
            "print(open('input.txt', encoding='utf-8').read())",
            files={"input.txt": "payload"},
        )
    )

    assert result.ok
    assert result.stdout == "payload\n"
    assert result.files == []  # provided by the request, not created by the run


async def test_the_private_executor_directory_is_hidden_from_the_file_listing(
    workspace: Workspace,
) -> None:
    # HOME lives in the private subdirectory: a file written to "~" must not look like workspace
    # material, and it must never land in the worker's real home.
    await LocalExecutor(workspace=workspace).run(
        python("import os\nopen(os.path.expanduser('~/hidden.txt'), 'w').write('x')")
    )

    assert (workspace.root / INTERNAL_DIR).is_dir()
    assert workspace.list_files() == []
    internal = workspace.list_files(include_internal=True)
    assert any(path.startswith(f"{INTERNAL_DIR}/home/") for path in internal)


async def test_path_traversal_is_refused_before_anything_is_written(
    workspace: Workspace, tmp_path: Path
) -> None:
    marker = uuid.uuid4().hex
    absolute = Path("/tmp") / f"botai-executor-escape-{marker}.txt"
    cases = [
        ("../escape.txt", tmp_path / "escape.txt"),
        (f"a/../../escape-{marker}.txt", tmp_path / f"escape-{marker}.txt"),
        (str(absolute), absolute),
    ]
    executor = LocalExecutor(workspace=workspace)
    for relative, outside in cases:
        with pytest.raises(WorkspacePathError):
            await executor.run(python("print('x')", files={relative: "escape"}))
        assert not outside.exists(), f"{relative!r} escaped the workspace"
    assert workspace.list_files() == []


@pytest.mark.skipif(os.name != "posix", reason="symlinks are a POSIX fixture")
async def test_a_symlink_cannot_be_used_to_leave_the_workspace(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace.root / "link")

    with pytest.raises(WorkspacePathError):
        await LocalExecutor(workspace=workspace).run(
            python("print('x')", files={"link/escape.txt": "escape"})
        )
    assert not (outside / "escape.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlinks are a POSIX fixture")
async def test_the_private_directory_cannot_be_hijacked_by_a_symlink(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "worker-home"
    outside.mkdir()
    executor = LocalExecutor(workspace=workspace)

    # Code of an earlier call (or the agent itself) may leave a symlink under the reserved name:
    # HOME/TMPDIR/scripts must never be created through it.
    os.symlink(outside, workspace.root / INTERNAL_DIR)
    with pytest.raises(WorkspacePathError):
        await executor.run(python("print('x')"))
    assert list(outside.iterdir()) == []

    (workspace.root / INTERNAL_DIR).unlink()
    workspace.internal_dir()  # a real private directory
    os.symlink(outside, workspace.root / INTERNAL_DIR / "home")
    with pytest.raises(WorkspacePathError):
        await executor.run(python("print('x')"))
    assert list(outside.iterdir()) == []


def test_a_workspace_path_guard_and_reading_helpers(workspace: Workspace, tmp_path: Path) -> None:
    assert workspace.resolve("a/b.txt") == workspace.root / "a" / "b.txt"
    for invalid in (
        "../outside.txt",
        str(tmp_path / "outside.txt"),
        "",
        INTERNAL_DIR,
        f"{INTERNAL_DIR}/x",
    ):
        with pytest.raises(WorkspacePathError):
            workspace.resolve(invalid)
    workspace.write_text("a/b.txt", "hello")
    assert workspace.read_text("a/b.txt") == "hello"
    with pytest.raises(ExecutorError, match="larger than"):
        workspace.write_text("big.txt", "x" * 100, max_bytes=10)
    with pytest.raises(WorkspacePathError):
        workspace.write_text("../escape.txt", "nope")


# --- output ------------------------------------------------------------------------------------


async def test_output_is_capped_and_the_rest_is_drained(workspace: Workspace) -> None:
    limits = ExecLimits(max_output_bytes=1024)
    code = "import sys\nprint('o' * 200000)\nprint('e' * 200000, file=sys.stderr)\n"
    result = await LocalExecutor(workspace=workspace, limits=limits).run(python(code))

    assert result.ok  # the process is not killed by its own output
    assert result.truncated
    assert len(result.stdout) == 1024
    assert len(result.stderr) == 1024


# --- executor lifecycle and protocol -----------------------------------------------------------


async def test_a_self_created_workspace_is_cleaned_up_and_closed() -> None:
    executor = LocalExecutor()
    root = executor.workspace.root
    assert root.is_dir()

    assert (await executor.run(python("print(1)"))).ok
    executor.close()

    assert not root.exists()
    with pytest.raises(ExecutorUnavailable, match="closed"):
        await executor.run(python("print(1)"))


def test_local_executor_implements_the_executor_protocol(workspace: Workspace) -> None:
    executor = LocalExecutor(workspace=workspace)
    assert isinstance(executor, Executor)
    assert executor.workspace is workspace
    assert isinstance(executor.limits, ExecLimits)
