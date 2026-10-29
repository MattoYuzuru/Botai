"""Threads: creation, routing of a student's message, and the live runtime of a thread.

A thread has one agent. A message goes to a task of the thread only when that task is waiting for
a plain, non-secret answer in the dialogue; every other message starts an agent turn (`CHAT`),
whatever background tasks are running. Nothing here knows what the thread was "for".
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.schemas import (
    DialogueReceipt,
    MessageAccepted,
    MessageIn,
    MessageOut,
    PendingRef,
    ThreadCreate,
    ThreadRuntime,
)
from botai.agent.threads import primary_task
from botai.contracts.agent_ui import AgentUiBlock, TextBlock
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Job, JobAnswer, JobEvent
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT
from botai.jobs.notify import CHANNEL_ANSWERS
from botai.jobs.queue import TASK_KINDS, enqueue_in_session
from botai.jobs.schemas import JobOut
from botai.spaces import repo
from botai.spaces.conversations import owned_thread, safe_text

_BLOCK = TypeAdapter(AgentUiBlock)
RUNTIME_TASKS = 20
RUNTIME_RECEIPTS = 200
_ASKING = {"input", "choice", "confirm", "approval", "login", "wait"}
SECRET_IN_CHAT = "Пароль и код вводятся только в защищённое поле, не в чат"  # noqa: S105


def message_out(message: ChatMessage) -> MessageOut:
    blocks: list[dict[str, Any]] = []
    for raw in message.blocks:
        try:
            block = _BLOCK.validate_python(raw)
        except ValidationError:
            # Preserve future or malformed blocks for the PWA's explicit unknown-block fallback.
            blocks.append(raw)
        else:
            blocks.append(block.model_dump(by_alias=True, mode="json", exclude_none=True))
    return MessageOut(
        id=message.id,
        role="user" if message.role == "user" else "agent",
        blocks=blocks,
        job_id=message.job_id,
        created_at=message.created_at,
    )


async def create_thread(
    session: AsyncSession, services: Services, user_id: str, body: ThreadCreate
) -> ChatThread:
    if body.space_id is not None:
        await repo.get_space(session, user_id, body.space_id)  # a foreign space is a 404
    title = " ".join((body.title or "").split())[:200] or "Новый чат"
    if body.client_request_id:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"thread:{user_id}:{body.client_request_id}", 0)
                )
            )
        )
        existing = await session.scalar(
            select(ChatThread).where(
                ChatThread.user_id == user_id,
                ChatThread.client_request_id == body.client_request_id,
            )
        )
        if existing is not None:
            if existing.space_id != body.space_id or (
                body.title is not None and existing.title != title
            ):
                raise BotaiError(
                    ErrorCode.CONFLICT, "Этот запрос уже использован для другого разговора"
                )
            return existing
    now = services.clock.now()
    thread = ChatThread(
        id=new_id(),
        user_id=user_id,
        space_id=body.space_id,
        title=title,
        client_request_id=body.client_request_id,
        created_at=now,
        last_message_at=now,
    )
    session.add(thread)
    await session.commit()
    diagnostic_event("chat.created", thread_id=thread.id)
    return thread


# --- runtime ------------------------------------------------------------------------------------


async def thread_jobs(
    session: AsyncSession, user_id: str, thread_id: str, *, limit: int | None = None
) -> list[Job]:
    """Tasks of the thread, newest first."""
    query = (
        select(Job)
        .where(Job.user_id == user_id, Job.thread_id == thread_id, Job.kind.in_(TASK_KINDS))
        .order_by(Job.created_at.desc(), Job.id.desc())
        .execution_options(populate_existing=True)
    )
    if limit is not None:
        query = query.limit(limit)
    return list(await session.scalars(query))


async def _pending_of(session: AsyncSession, job: Job, answered: set[str]) -> PendingRef | None:
    value = dialogue.state_of(job).get("pending")
    if isinstance(value, dict):
        return PendingRef.model_validate(value)
    if job.state != "waiting_user":
        return None
    events = await session.scalars(
        select(JobEvent)
        .where(JobEvent.job_id == job.id, JobEvent.type == "block")
        .order_by(JobEvent.seq.desc())
    )
    for event in events:
        try:
            block = _BLOCK.validate_python(event.data)
        except ValidationError:
            continue
        if block.id in answered or block.type not in _ASKING:
            continue
        return PendingRef(
            job_id=job.id,
            block_id=block.id,
            channel=dialogue.channel_of(block),  # type: ignore[arg-type]
            secret=block.type == "input" and block.secret,
        )
    return None


async def runtime(
    session: AsyncSession, services: Services, user_id: str, thread_id: str
) -> ThreadRuntime:
    thread = await owned_thread(session, user_id, thread_id)
    tasks = await thread_jobs(session, user_id, thread.id)
    in_flight = [job for job in tasks if job.state in IN_FLIGHT]
    active = primary_task(in_flight)
    if active is None:
        active = await session.scalar(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.thread_id == thread.id,
                Job.kind == JobKind.CHAT.value,
                Job.state.in_(sorted(IN_FLIGHT)),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
    receipts: list[DialogueReceipt] = []
    receipt_keys: set[tuple[str, str]] = set()
    for item in tasks:
        for value in (dialogue.state_of(item).get("receipts") or {}).values():
            receipt = DialogueReceipt.model_validate(value)
            receipts.append(receipt)
            receipt_keys.add((item.id, str(receipt.block_id or receipt.message_id)))
    if tasks:
        answers = await session.scalars(
            select(JobAnswer).where(JobAnswer.job_id.in_([item.id for item in tasks]))
        )
        for answer in answers:
            if (answer.job_id, answer.block_id) not in receipt_keys:
                receipts.append(
                    DialogueReceipt(
                        job_id=answer.job_id,
                        message_id=None,
                        block_id=answer.block_id,
                        status="accepted",
                        at=answer.created_at,
                    )
                )
    pending: list[PendingRef] = []
    for job in reversed(in_flight):
        answered = {r.block_id for r in receipts if r.job_id == job.id and r.block_id}
        ref = await _pending_of(session, job, answered)
        if ref is not None:
            pending.append(ref)
    return ThreadRuntime(
        active_job=JobOut.model_validate(active) if active else None,
        tasks=[JobOut.model_validate(job) for job in tasks[:RUNTIME_TASKS]],
        pending=pending,
        receipts=sorted(receipts, key=lambda r: r.at)[-RUNTIME_RECEIPTS:],
    )


# --- posting ------------------------------------------------------------------------------------


def credential_like(text: str) -> bool:
    """A single token with no spaces: how a password or a one-time code looks in a composer."""
    return bool(text.strip()) and not re.search(r"\s", text.strip()) and len(text.strip()) <= 128


async def _locked(session: AsyncSession, user_id: str, job_id: str) -> Job | None:
    return await session.scalar(
        select(Job)
        .where(Job.user_id == user_id, Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _waiting_secret(job: Job) -> bool:
    pending = dialogue.state_of(job).get("pending")
    return (
        isinstance(pending, dict)
        and pending.get("channel") == "native"
        and bool(pending.get("secret"))
    )


def _waiting_dialogue(job: Job) -> bool:
    pending = dialogue.state_of(job).get("pending")
    return (
        job.state == "waiting_user"
        and isinstance(pending, dict)
        and pending.get("channel") == "dialogue"
    )


async def post_message(
    session: AsyncSession, services: Services, user_id: str, thread_id: str, body: MessageIn
) -> MessageAccepted:
    thread = await owned_thread(session, user_id, thread_id)
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"thread-post:{thread.id}", 0)))
    )
    if body.client_message_id:
        replay = await _replay(session, user_id, thread.id, body)
        if replay is not None:
            return replay
    jobs = list(
        await session.scalars(
            select(Job)
            .where(
                Job.user_id == user_id, Job.thread_id == thread.id, Job.state.in_(sorted(IN_FLIGHT))
            )
            .execution_options(populate_existing=True)
        )
    )
    target: Job | None = None
    if body.target_job_id is not None:
        target = await _locked(session, user_id, body.target_job_id)
        if target is None or target.thread_id != thread.id or target.kind not in TASK_KINDS:
            raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
        if target.state not in IN_FLIGHT:
            raise BotaiError(
                ErrorCode.CONFLICT, "Этот шаг уже завершён. Откройте текущее состояние разговора"
            )
    else:
        waiting = next(
            (
                job
                for job in sorted(jobs, key=lambda j: (j.created_at, j.id), reverse=True)
                if job.kind in TASK_KINDS and _waiting_dialogue(job)
            ),
            None,
        )
        if waiting is not None:
            target = await _locked(session, user_id, waiting.id)
            if target is not None and not _waiting_dialogue(target):
                target = None  # answered while we were waiting for the lock
    text = safe_text(body.text) if target is not None else body.text
    if target is not None:
        if target.connection_id:
            conn = await repo.get_connection(session, user_id, target.connection_id)
            if conn.status == "disabled":
                raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
        if _waiting_secret(target):
            raise BotaiError(ErrorCode.CONFLICT, SECRET_IN_CHAT)
    else:
        if any(job.kind == JobKind.CHAT.value for job in jobs):
            raise BotaiError(ErrorCode.CONFLICT, "Ассистент ещё отвечает на прошлое сообщение")
        if credential_like(text) and any(_waiting_secret(job) for job in jobs):
            raise BotaiError(ErrorCode.CONFLICT, SECRET_IN_CHAT)
    now = services.clock.now()
    if target is not None:
        now = await dialogue.message_time(session, user_id, thread.id, now)
    block = TextBlock(id=new_id(), at=now, markdown=text)
    message = ChatMessage(
        id=new_id(),
        user_id=user_id,
        thread_id=thread.id,
        role="user",
        blocks=[block.model_dump(by_alias=True, mode="json", exclude_none=True)],
        client_message_id=body.client_message_id,
        created_at=now,
    )
    session.add(message)
    if target is not None:
        message.job_id = target.id
        state = dialogue.state_of(target)
        inbox = list(state.get("inbox") or [])
        pending = state.get("pending")
        inbox.append(
            {
                "message_id": message.id,
                "reply_to_block_id": pending.get("block_id")
                if isinstance(pending, dict) and pending.get("channel") == "dialogue"
                else None,
            }
        )
        state["inbox"] = inbox
        dialogue.save_state(target, state)
        dialogue.advance_input(target)
        dialogue.receipt(target, now=now, status="accepted", message_id=message.id)
        await session.execute(select(func.pg_notify(CHANNEL_ANSWERS, target.id)))
        job_id = target.id
    else:
        queued = await enqueue_in_session(
            session,
            JobKind.CHAT,
            user_id=user_id,
            thread_id=thread.id,
            payload={"thread_id": thread.id, "message_id": message.id},
            now=now,
            dedupe=False,
        )
        message.job_id = job_id = queued.id
    await session.commit()
    diagnostic_event(
        "chat.message",
        thread_id=thread.id,
        job_id=job_id,
        message_id=message.id,
        source=target is not None,  # the message went to a task's inbox, not to an agent turn
        input_chars=len(text),
        connection_id=target.connection_id if target else None,
    )
    return MessageAccepted(message=message_out(message), job_id=job_id)


async def _replay(
    session: AsyncSession, user_id: str, thread_id: str, body: MessageIn
) -> MessageAccepted | None:
    """The canonical answer of an earlier identical submission; a different body is a conflict."""
    existing = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.user_id == user_id,
            ChatMessage.thread_id == thread_id,
            ChatMessage.client_message_id == body.client_message_id,
        )
    )
    if existing is None:
        return None
    stored = "\n\n".join(
        str(b.get("markdown", "")) for b in existing.blocks if b.get("type") == "text"
    )
    existing_job = (
        await session.scalar(
            select(Job).where(
                Job.id == existing.job_id, Job.user_id == user_id, Job.thread_id == thread_id
            )
        )
        if existing.job_id
        else None
    )
    submitted = (
        safe_text(body.text)
        if existing_job is not None and existing_job.kind in TASK_KINDS
        else body.text
    )
    if stored != submitted or (
        body.target_job_id is not None and body.target_job_id != existing.job_id
    ):
        raise BotaiError(ErrorCode.CONFLICT, "Этот запрос уже использован для другого сообщения")
    if existing.job_id is None:
        raise BotaiError(ErrorCode.CONFLICT, "Сообщение ещё не связано с заданием")
    return MessageAccepted(message=message_out(existing), job_id=existing.job_id)
