"""Thread list entries and their small mutations: unread, attention, active task, preview, delete."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.schemas import ActiveTask, Attention, ThreadOut
from botai.core.errors import BotaiError, ErrorCode
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Connection, Job
from botai.jobs import answers
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import TASK_KINDS
from botai.jobs.tasks import task_title
from botai.spaces.conversations import owned_thread

PREVIEW_CHARS = 120
LIST_LIMIT = 500
_SEEN_KINDS = ("thinking",)  # a progress line is not a message the student has to read


def primary_task(jobs: Sequence[Job]) -> Job | None:
    """The task that needs attention first among a thread's jobs: one waiting for the student,
    else the newest running one. A task that continued into a newer one yields to it."""
    active = [j for j in jobs if j.kind in TASK_KINDS and j.state in IN_FLIGHT]
    live_ids = {j.id for j in active}
    live = [j for j in active if j.payload.get("continuation_job_id") not in live_ids]
    live.sort(key=lambda j: (j.created_at, j.id), reverse=True)
    live.sort(key=lambda j: j.state != "waiting_user")  # stable: waiting first, newest first
    return live[0] if live else None


def _preview(blocks: list[dict[str, object]]) -> str | None:
    if not blocks or blocks[0].get("type") != "text":
        return None
    text = " ".join(str(blocks[0].get("markdown", "")).split())
    if not text:
        return None
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "…"


async def thread_outs(
    session: AsyncSession, user_id: str, threads: Sequence[ChatThread]
) -> list[ThreadOut]:
    ids = [t.id for t in threads]
    if not ids:
        return []
    newest_agent = {
        thread_id: at
        for thread_id, at in await session.execute(
            select(ChatMessage.thread_id, func.max(ChatMessage.created_at))
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.thread_id.in_(ids),
                ChatMessage.role == "agent",
                ChatMessage.blocks[0]["type"].astext.not_in(_SEEN_KINDS),
            )
            .group_by(ChatMessage.thread_id)
        )
    }
    previews = {
        thread_id: _preview(blocks)
        for thread_id, blocks in (
            await session.execute(
                select(ChatMessage.thread_id, ChatMessage.blocks)
                .where(
                    ChatMessage.user_id == user_id,
                    ChatMessage.thread_id.in_(ids),
                    ChatMessage.blocks[0]["type"].astext == "text",
                )
                .ext(distinct_on(ChatMessage.thread_id))
                .order_by(
                    ChatMessage.thread_id, ChatMessage.created_at.desc(), ChatMessage.id.desc()
                )
            )
        )
    }
    in_flight: dict[str, list[Job]] = {}
    for job in await session.scalars(
        select(Job).where(
            Job.user_id == user_id, Job.thread_id.in_(ids), Job.state.in_(sorted(IN_FLIGHT))
        )
    ):
        in_flight.setdefault(cast(str, job.thread_id), []).append(job)
    latest = {
        job.thread_id: job
        for job in await session.scalars(
            select(Job)
            .where(Job.user_id == user_id, Job.thread_id.in_(ids))
            .ext(distinct_on(Job.thread_id))
            .order_by(Job.thread_id, Job.created_at.desc(), Job.id.desc())
        )
    }
    titles = {
        cid: title
        for cid, title in (
            await session.execute(
                select(Connection.id, Connection.title).where(
                    Connection.user_id == user_id,
                    Connection.id.in_(
                        [
                            j.connection_id
                            for jobs in in_flight.values()
                            for j in jobs
                            if j.connection_id
                        ]
                    ),
                )
            )
        )
    }
    outs: list[ThreadOut] = []
    for thread in threads:
        jobs = in_flight.get(thread.id, [])
        task = primary_task(jobs)
        seen = thread.last_read_at
        agent_at = newest_agent.get(thread.id)
        failed = latest.get(thread.id)
        attention: Attention | None = None
        if any(j.state == "waiting_user" for j in jobs):
            attention = "waiting_user"
        elif jobs:
            attention = "running"
        elif (
            failed is not None
            and failed.state == "failed"
            and (seen is None or (failed.finished_at or failed.created_at) > seen)
        ):
            attention = "failed"
        outs.append(
            ThreadOut(
                id=thread.id,
                space_id=thread.space_id,
                title=thread.title,
                created_at=thread.created_at,
                last_message_at=thread.last_message_at,
                unread=agent_at is not None and (seen is None or agent_at > seen),
                attention=attention,
                active_task=ActiveTask.model_validate(
                    {
                        "job_id": task.id,
                        "kind": task.kind,
                        "connection_id": task.connection_id,
                        "state": task.state,
                        "title": task_title(task.kind, titles.get(task.connection_id or "")),
                    }
                )
                if task is not None
                else None,
                preview=previews.get(thread.id),
            )
        )
    return outs


async def thread_out(session: AsyncSession, thread: ChatThread) -> ThreadOut:
    (out,) = await thread_outs(session, thread.user_id, [thread])
    return out


async def list_threads(session: AsyncSession, user_id: str) -> list[ThreadOut]:
    threads = list(
        await session.scalars(
            select(ChatThread)
            .where(ChatThread.user_id == user_id)
            .order_by(ChatThread.last_message_at.desc(), ChatThread.id.desc())
            .limit(LIST_LIMIT)
        )
    )
    return await thread_outs(session, user_id, threads)


async def rename_thread(
    session: AsyncSession, user_id: str, thread_id: str, title: str
) -> ThreadOut:
    thread = await owned_thread(session, user_id, thread_id)
    clean = " ".join(title.split())
    if not clean:
        raise BotaiError(ErrorCode.VALIDATION, "Название не может быть пустым")
    thread.title = clean[:200]
    await session.commit()
    return await thread_out(session, thread)


async def mark_read(
    session: AsyncSession, services: Services, user_id: str, thread_id: str
) -> None:
    thread = await owned_thread(session, user_id, thread_id)
    thread.last_read_at = services.clock.now()
    await session.commit()


async def delete_thread(
    session: AsyncSession, services: Services, user_id: str, thread_id: str
) -> None:
    """Stops the tasks and the turn of the thread, then removes it with its messages."""
    thread = await owned_thread(session, user_id, thread_id)
    running = list(
        await session.scalars(
            select(Job.id).where(
                Job.user_id == user_id, Job.thread_id == thread.id, Job.state.in_(sorted(IN_FLIGHT))
            )
        )
    )
    # Detach first: a stopped task must not write its last words into a thread being deleted.
    await session.execute(
        update(Job).where(Job.user_id == user_id, Job.thread_id == thread.id).values(thread_id=None)
    )
    await session.commit()
    for job_id in running:
        await answers.cancel_job(session, services.vault, services.clock, user_id, job_id)
    thread = await owned_thread(session, user_id, thread_id)
    await session.delete(thread)
    await session.commit()
