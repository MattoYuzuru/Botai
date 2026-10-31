"""Production smoke of the v4 free harness: one tiny `AGENT` job through the real queue.

Run inside the api container right after `BOTAI_AGENT_ENGINE=v4` is enabled:

    docker exec -i botai-api-1 python - < ops/smoke/v4_agent_smoke.py

It enqueues one AGENT job without a browser (code_exec only), waits for a terminal state, prints
status/steps/tokens/cost and verifies that the run left an `agent_runs` row + DB trace events.
The operator separately checks that no `label=botai.agent.sandbox=1` container is left behind
(see docs/plan/deployment.md, «Проверка v4 после включения»).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from botai.agent.v4.factory import enqueue_agent_run
from botai.core.jobs import JobState
from botai.db.models import AgentRun, AgentRunEvent, Job, User
from botai.wiring import build_services

TASK = (
    "Выполни инструмент code_exec (python): посчитай 6*7, сохрани результат строкой "
    "в файл smoke.txt в workspace, подожди 5 секунд (time.sleep) и ответь одним числом."
)
TERMINAL = {JobState.DONE.value, JobState.FAILED.value, JobState.CANCELLED.value}


async def main() -> None:
    services = build_services()
    settings = services.settings
    print(f"engine={settings.agent_engine} executor={settings.agent_executor} env={settings.env}")
    if settings.agent_engine != "v4":
        print("engine is not v4: smoke not attempted")
        return
    async with services.db() as session:
        user = (
            await session.scalars(select(User).where(User.disabled.is_(False)).limit(1))
        ).first()
    if user is None:
        print("no enabled user in this database; aborting")
        return
    job_id = await enqueue_agent_run(
        services, user_id=user.id, task=TASK, payload={"_browser": False}
    )
    print(f"job_id={job_id}")
    job = None
    for _ in range(120):
        async with services.db() as session:
            job = await session.get(Job, job_id)
        if job is not None and str(job.state) in TERMINAL:
            break
        await asyncio.sleep(2)
    if job is None:
        print("job row not found")
        return
    print(f"state={job.state} stage={job.stage}")
    result = job.result if isinstance(job.result, dict) else {}
    for key in ("status", "run_id", "steps", "tool_calls", "total_tokens", "cost_usd"):
        print(f"result.{key}={result.get(key)}")
    run_id = result.get("run_id")
    if isinstance(run_id, str):
        async with services.db() as session:
            events = await session.scalar(
                select(func.count(AgentRunEvent.id)).where(AgentRunEvent.run_id == run_id)
            )
            run = await session.get(AgentRun, run_id)
        print(f"agent_run={'present' if run is not None else 'MISSING'} events={events}")
        print(f"run_status={getattr(run, 'status', None)}")
    if job.error:
        print(f"job_error={job.error.get('code')}: {str(job.error.get('detail'))[:200]}")
    print("smoke done; verify no leftover sandbox containers separately")


asyncio.run(main())
