"""The running tasks of a student as the agent sees them: kind, state, stage, what they wait for.

Universities and sources come from `botai.spaces.service.list_sources`; this adds what an
agent needs about the tasks on them (`list_universities`, `task_status`, the turn context).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Connection, Job
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import TASK_KINDS
from botai.jobs.tasks import task_title
from botai.spaces import views


@dataclass(slots=True)
class TaskView:
    job_id: str
    kind: str
    state: str
    thread_id: str | None
    connection_id: str | None
    title: str
    stage: str | None
    question: str | None  # the non-secret text of what the task is waiting for


async def active_tasks(
    session: AsyncSession, user_id: str, *, thread_id: str | None = None
) -> list[TaskView]:
    """In-flight tasks of the student (of one thread when given), newest first."""
    query = select(Job).where(
        Job.user_id == user_id, Job.kind.in_(TASK_KINDS), Job.state.in_(sorted(IN_FLIGHT))
    )
    if thread_id is not None:
        query = query.where(Job.thread_id == thread_id)
    jobs = list(
        await session.scalars(
            query.order_by(Job.created_at.desc(), Job.id.desc()).execution_options(
                populate_existing=True
            )
        )
    )
    live_ids = {job.id for job in jobs}
    jobs = [job for job in jobs if job.payload.get("continuation_job_id") not in live_ids]
    titles = {
        c.id: views.display_title(c)
        for c in await session.scalars(
            select(Connection).where(
                Connection.user_id == user_id,
                Connection.id.in_([j.connection_id for j in jobs if j.connection_id]),
            )
        )
    }
    return [task_view(job, titles.get(job.connection_id or "")) for job in jobs]


def task_view(job: Job, connection_title: str | None) -> TaskView:
    pending = dialogue.state_of(job).get("pending")
    question = None
    if job.state == "waiting_user":
        # Only the kind of wait is public to the agent: never the text of a secret prompt.
        secret = isinstance(pending, dict) and bool(pending.get("secret"))
        channel = pending.get("channel") if isinstance(pending, dict) else None
        question = (
            "защищённый ввод пароля или кода в приложении"
            if secret
            else "вход в браузере в приложении"
            if channel == "manual"
            else "ответ студента в диалоге"
            if channel == "dialogue"
            else "выбор или подтверждение в приложении"
        )
    return TaskView(
        job_id=job.id,
        kind=job.kind,
        state=job.state,
        thread_id=job.thread_id,
        connection_id=job.connection_id,
        title=task_title(job.kind, connection_title),
        stage=job.stage,
        question=question,
    )
