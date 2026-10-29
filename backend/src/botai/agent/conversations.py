"""Durable chat-bound source setup. Plain dialogue never becomes a native credential answer."""

from __future__ import annotations

import hashlib
import json
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
from botai.contracts.agent_ui import AgentUiBlock, TextBlock
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Connection, Job, JobAnswer, JobEvent
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT, TERMINAL
from botai.jobs.notify import CHANNEL_ANSWERS
from botai.jobs.queue import PgJobQueue, enqueue_in_session
from botai.jobs.schemas import JobOut
from botai.spaces import repo, universities
from botai.spaces.conversations import intent_of, normalize_context, owned_thread, safe_text
from botai.spaces.service import connection_out

SOURCE_KINDS = ("connect", "repair", "learn", "sync")
_BLOCK = TypeAdapter(AgentUiBlock)


def message_out(message: ChatMessage) -> MessageOut:
    blocks: list[dict[str, Any]] = []
    for raw in message.blocks:
        try:
            block = _BLOCK.validate_python(raw)
        except ValidationError:
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
    space = (
        await repo.get_space(session, user_id, body.space_id) if body.space_id is not None else None
    )
    if body.purpose != "connect" and body.connect_context is not None:
        raise BotaiError(ErrorCode.VALIDATION, "Контекст подключения относится к чату подключения")
    context = (
        normalize_context(
            body.connect_context.model_dump(mode="json", exclude_none=True, exclude_unset=True)
            if body.connect_context
            else {}
        )
        if body.purpose == "connect"
        else {}
    )
    if (
        body.purpose == "connect"
        and space is not None
        and space.university_id
        and not context.get("university_id")
    ):
        context["university_id"] = space.university_id
    if context.get("university_id"):
        university = await universities.find_or_create(
            session,
            title=str(context.get("title") or context["university_id"]),
            university_id=str(context["university_id"]),
        )
        context["university_id"] = university.id
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "space_id": body.space_id,
                "title": body.title,
                "purpose": body.purpose,
                "context": context,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
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
            previous_context = {
                key: value for key, value in context.items() if key != "discovery_mode"
            }
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "space_id": body.space_id,
                        "title": body.title,
                        "purpose": body.purpose,
                        "context": previous_context,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            accepted_hashes = {fingerprint}
            if context.get("discovery_mode") == "auto":
                accepted_hashes.add(legacy_fingerprint)
            if existing.connect_context.get("_start_hash") not in accepted_hashes:
                raise BotaiError(
                    ErrorCode.CONFLICT, "Этот запрос уже использован для другого разговора"
                )
            return existing
    now = services.clock.now()
    thread = ChatThread(
        id=new_id(),
        user_id=user_id,
        space_id=body.space_id,
        title=(
            (safe_text(body.title) if body.title and body.purpose == "connect" else body.title)
            or ("Подключение источника" if body.purpose == "connect" else "Новый чат")
        ).strip()
        or "Новый чат",
        purpose=body.purpose,
        connect_context={**context, "_start_hash": fingerprint},
        client_request_id=body.client_request_id,
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    await session.flush()
    if body.purpose == "connect":
        job = await enqueue_in_session(
            session,
            JobKind.CONNECT,
            user_id=user_id,
            thread_id=thread.id,
            payload={"learning_intent": intent_of(context)},
            now=now,
            dedupe=False,
        )
        intro = "Подключить источник" + (f": {context['title']}" if context.get("title") else ".")
        if context.get("entry_urls"):
            intro += "\n" + "\n".join(context["entry_urls"])
        if context.get("user_note"):
            intro += "\n" + context["user_note"]
        block = TextBlock(id=new_id(), at=now, markdown=intro[:8000])
        session.add(
            ChatMessage(
                id=new_id(),
                user_id=user_id,
                thread_id=thread.id,
                role="user",
                blocks=[block.model_dump(by_alias=True, mode="json", exclude_none=True)],
                job_id=job.id,
                created_at=now,
            )
        )
    await session.commit()
    diagnostic_event("chat.created", thread_id=thread.id, source=body.purpose == "connect")
    return thread


async def current_job(session: AsyncSession, user_id: str, thread: ChatThread) -> Job | None:
    jobs = list(
        await session.scalars(
            select(Job)
            .where(Job.user_id == user_id, Job.thread_id == thread.id)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .execution_options(populate_existing=True)
        )
    )
    sources = [job for job in jobs if job.kind in SOURCE_KINDS and job.state in IN_FLIGHT]
    active_ids = {job.id for job in sources}
    sources = [job for job in sources if job.payload.get("continuation_job_id") not in active_ids]
    if sources:
        return sources[0]
    active_chat = next((job for job in jobs if job.kind == "chat" and job.state in IN_FLIGHT), None)
    if active_chat is not None:
        return active_chat
    if thread.purpose == "connect":
        return next((job for job in jobs if job.kind in SOURCE_KINDS), None)
    return None


async def runtime(
    session: AsyncSession, services: Services, user_id: str, thread_id: str
) -> ThreadRuntime:
    thread = await owned_thread(session, user_id, thread_id)
    job = await current_job(session, user_id, thread)
    connection_id = (
        job.connection_id
        if job and job.connection_id
        else thread.connect_context.get("connection_id")
    )
    connection = (
        await session.scalar(
            select(Connection).where(
                Connection.user_id == user_id, Connection.id == str(connection_id)
            )
        )
        if connection_id
        else None
    )
    pending: list[PendingRef] = []
    receipts: list[DialogueReceipt] = []
    jobs = list(
        await session.scalars(
            select(Job)
            .where(Job.user_id == user_id, Job.thread_id == thread.id)
            .order_by(Job.created_at, Job.id)
        )
    )
    receipt_keys: set[tuple[str, str]] = set()
    for item in jobs:
        for value in (dialogue.state_of(item).get("receipts") or {}).values():
            receipt = DialogueReceipt.model_validate(value)
            receipts.append(receipt)
            receipt_keys.add((item.id, str(receipt.block_id or receipt.message_id)))
    if jobs:
        answers = await session.scalars(
            select(JobAnswer).where(JobAnswer.job_id.in_([item.id for item in jobs]))
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
    if job is not None and job.state not in TERMINAL:
        value = dialogue.state_of(job).get("pending")
        if isinstance(value, dict):
            pending.append(PendingRef.model_validate(value))
        elif job.state == "waiting_user":
            answered = {r.block_id for r in receipts if r.job_id == job.id and r.block_id}
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
                if block.id in answered or block.type not in {
                    "input",
                    "choice",
                    "confirm",
                    "approval",
                    "login",
                    "wait",
                }:
                    continue
                channel = (
                    "manual"
                    if block.type == "login"
                    else "approval"
                    if block.type == "approval"
                    else "dialogue"
                    if block.type == "input"
                    and not block.secret
                    and not dialogue.is_auth_input(block)
                    else "native"
                )
                pending.append(
                    PendingRef(
                        job_id=job.id,
                        block_id=block.id,
                        channel=channel,
                        secret=block.type == "input" and block.secret,
                    )
                )
                break
    return ThreadRuntime(
        purpose="connect" if thread.purpose == "connect" else "chat",
        active_job=JobOut.model_validate(job) if job else None,
        connection=await connection_out(services.vault, connection) if connection else None,
        pending=pending,
        receipts=sorted(receipts, key=lambda r: r.at)[-200:],
    )


async def post_message(
    session: AsyncSession, services: Services, user_id: str, thread_id: str, body: MessageIn
) -> MessageAccepted:
    thread = await owned_thread(session, user_id, thread_id)
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"thread-post:{thread.id}", 0)))
    )
    job = await current_job(session, user_id, thread)
    target = None
    if body.target_job_id is not None:
        target = await session.scalar(
            select(Job)
            .where(Job.user_id == user_id, Job.thread_id == thread.id, Job.id == body.target_job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    elif job is not None and job.kind in SOURCE_KINDS and job.state in IN_FLIGHT:
        target = await session.scalar(
            select(Job)
            .where(Job.user_id == user_id, Job.id == job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    source = target is not None and target.kind in SOURCE_KINDS
    text = safe_text(body.text) if source else body.text
    if body.client_message_id:
        existing = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.thread_id == thread.id,
                ChatMessage.client_message_id == body.client_message_id,
            )
        )
        if existing is not None:
            stored = "\n\n".join(
                str(b.get("markdown", "")) for b in existing.blocks if b.get("type") == "text"
            )
            existing_job = (
                await session.scalar(
                    select(Job).where(
                        Job.id == existing.job_id,
                        Job.user_id == user_id,
                        Job.thread_id == thread.id,
                    )
                )
                if existing.job_id
                else None
            )
            submitted_text = (
                safe_text(body.text)
                if existing_job is not None and existing_job.kind in SOURCE_KINDS
                else body.text
            )
            if stored != submitted_text or (
                body.target_job_id is not None and body.target_job_id != existing.job_id
            ):
                raise BotaiError(
                    ErrorCode.CONFLICT, "Этот запрос уже использован для другого сообщения"
                )
            if existing.job_id is None:
                raise BotaiError(ErrorCode.CONFLICT, "Сообщение ещё не связано с заданием")
            return MessageAccepted(message=message_out(existing), job_id=existing.job_id)
    if target is not None:
        # The target lock may have waited for a committed cross-origin continuation.
        # Existing idempotent submissions returned above; a new turn must follow the current job.
        selected = await current_job(session, user_id, thread)
        if selected is None or selected.id != target.id:
            raise BotaiError(
                ErrorCode.CONFLICT, "Этот шаг уже заменён. Откройте текущее состояние разговора"
            )
    if target is not None and (not source or target.state not in IN_FLIGHT):
        raise BotaiError(
            ErrorCode.CONFLICT, "Этот шаг уже завершён. Откройте текущее состояние разговора"
        )
    if target is None and job is not None and job.state in IN_FLIGHT:
        raise BotaiError(ErrorCode.CONFLICT, "Ассистент ещё отвечает на прошлое сообщение")
    now = services.clock.now()
    if source:
        assert target is not None
        if target.connection_id:
            conn = await repo.get_connection(session, user_id, target.connection_id)
            if conn.status == "disabled":
                raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
        state = dialogue.state_of(target)
        waiting = state.get("pending")
        if (
            isinstance(waiting, dict)
            and waiting.get("channel") == "native"
            and waiting.get("secret")
        ):
            raise BotaiError(
                ErrorCode.CONFLICT, "Пароль и код вводятся только в защищённое поле, не в чат"
            )
    if source:
        assert target is not None
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
    thread.updated_at = now
    if source:
        assert target is not None
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
        if isinstance(services.queue, PgJobQueue):
            queued = await enqueue_in_session(
                session,
                JobKind.CHAT,
                user_id=user_id,
                thread_id=thread.id,
                payload={"thread_id": thread.id, "message_id": message.id},
                now=now,
                dedupe=False,
            )
            message.job_id = queued.id
            await session.commit()
            diagnostic_event(
                "chat.message",
                thread_id=thread.id,
                job_id=queued.id,
                message_id=message.id,
                source=False,
                input_chars=len(text),
            )
            return MessageAccepted(message=message_out(message), job_id=queued.id)
        # Non-transactional queue implementations retain the legacy protocol and compensation.
        await session.commit()
        try:
            job_id = await services.queue.enqueue(
                JobKind.CHAT,
                user_id=user_id,
                thread_id=thread.id,
                payload={"thread_id": thread.id, "message_id": message.id},
                dedupe=False,
            )
        except Exception:
            await session.delete(message)
            await session.commit()
            raise
        message.job_id = job_id
    await session.commit()
    diagnostic_event(
        "chat.message",
        thread_id=thread.id,
        job_id=job_id,
        message_id=message.id,
        source=source,
        input_chars=len(text),
        connection_id=target.connection_id if target else None,
    )
    return MessageAccepted(message=message_out(message), job_id=job_id)
