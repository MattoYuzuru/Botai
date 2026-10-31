"""Production smoke of the v4 code sandbox, run inside the worker (or api) container.

    docker exec -i botai-worker-1 python - < ops/smoke/sandbox_smoke.py

Proves on the real host that the Docker sandbox works as deployed: a sibling container starts from
`BOTAI_SANDBOX_IMAGE`, runs as uid 10001, shares the bind-mounted workspace between two calls,
does not see the worker's secrets/docker socket, and leaves no container or workspace directory
behind. It does not enable the v4 engine or call a model.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from botai.agent.v4.executor import ContainerExecutor, ExecLimits, ExecRequest, Workspace

ROOT = Path(os.environ.get("BOTAI_AGENT_WORKSPACE_ROOT") or "/srv/botai/agent-workspaces")
IMAGE = os.environ.get("BOTAI_SANDBOX_IMAGE", "")
assert IMAGE, "BOTAI_SANDBOX_IMAGE must be set in the container environment"


async def main() -> None:
    ws = Workspace.create(ROOT / f"smoke-{uuid.uuid4().hex[:10]}")
    executor = ContainerExecutor(workspace=ws, image=IMAGE, limits=ExecLimits(timeout_s=30))
    try:
        first = await executor.run(
            ExecRequest(
                language="python",
                code=(
                    "import os, socket, pathlib\n"
                    "print('host', socket.gethostname())\n"
                    "print('uid', os.getuid())\n"
                    "leaked = [k for k in os.environ if k.startswith(('BOTAI_', 'DATABASE_'))]\n"
                    "print('leaked_env', leaked)\n"
                    "print('docker_socket', pathlib.Path('/var/run/docker.sock').exists())\n"
                    "pathlib.Path('smoke.txt').write_text('from-sandbox')\n"
                    "print('answer', 6 * 7)\n"
                ),
            )
        )
        second = await executor.run(
            ExecRequest(language="python", code="print('second', open('smoke.txt').read())")
        )
        print("first_ok", first.ok, "|", first.stdout.strip().replace("\n", " ; "))
        print("second_ok", second.ok, "|", second.stdout.strip())
        print("created_files", first.files)
    finally:
        executor.close()
        ws.cleanup()


asyncio.run(main())
