"""Source tasks inside threads: safe user input, the thread of a connection and cross-site continuation.

A thread has no purpose. A task (connect, repair, sync, ingest) lives in the thread where it was
asked: `jobs.thread_id` is that thread and the task's blocks are projected into it
(`jobs.dialogue.project_block`). A connection's "own" thread is the thread of its newest task.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import ASKING_TYPES, AgentUiBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import guard_job
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import ChatThread, Connection, Job, JobAnswer, JobEvent, Space
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import enqueue_in_session, lock_sources
from botai.learn.addresses import navigation_url, url_origin
from botai.learn.intent import prepare_turn
from botai.sources.vocab import KIND_LABELS

_BLOCK = TypeAdapter(AgentUiBlock)


def safe_url(value: object) -> str:
    url = navigation_url(value)
    if url is None:
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Нужна обычная HTTPS-ссылка без паролей, токенов и служебных кодов",
        )
    return url


def safe_text(text: str) -> str:
    """Do not durably copy a rejected URL or a labelled secret from the composer."""
    turn = prepare_turn(text, max_length=8000)
    if turn.rejected_links:
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Ссылка содержит недопустимые или секретные параметры. Пришлите обычную ссылку",
        )
    value = turn.text
    for index, url in enumerate(turn.links):
        value = value.replace(f"[link:{index}]", url)
    if len(value) > 8000:
        raise BotaiError(ErrorCode.VALIDATION, "Сообщение слишком длинное")
    return value


def normalize_context(data: dict[str, Any]) -> dict[str, Any]:
    context = dict(data)
    if "discovery_mode" in context:
        if not isinstance(context["discovery_mode"], str) or context["discovery_mode"] not in {
            "auto",
            "requested",
        }:
            raise BotaiError(ErrorCode.VALIDATION, "Неверный режим исследования источника")
    elif "requested_kinds" not in context:
        context["discovery_mode"] = "auto"
    urls = context.get("entry_urls") or []
    if not isinstance(urls, list) or len(urls) > 6:
        raise BotaiError(ErrorCode.VALIDATION, "Можно указать до шести адресов источника")
    context["entry_urls"] = list(dict.fromkeys(safe_url(value) for value in urls))
    context["requested_kinds"] = context.get("requested_kinds", ["scheduleEvents"])
    context["absent_kinds"] = context.get("absent_kinds", [])
    for key in ("requested_kinds", "absent_kinds"):
        values = context[key]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or value not in KIND_LABELS for value in values)
            or len(set(values)) != len(values)
        ):
            raise BotaiError(ErrorCode.VALIDATION, "Неверный список видов учебных данных")
    if set(context["requested_kinds"]) & set(context["absent_kinds"]):
        raise BotaiError(
            ErrorCode.VALIDATION, "Нельзя одновременно искать и исключать один вид данных"
        )
    note = context.get("user_note") or ""
    if not isinstance(note, str) or len(note) > 2000:
        raise BotaiError(
            ErrorCode.VALIDATION, "Уточнение источника может содержать до 2000 символов"
        )
    context["user_note"] = safe_text(note)
    if isinstance(context.get("title"), str):
        context["title"] = safe_text(context["title"])[:120]
    return context


def intent_of(context: dict[str, Any]) -> dict[str, Any]:
    return {
        key: context[key]
        for key in ("entry_urls", "requested_kinds", "absent_kinds", "user_note", "discovery_mode")
        if key in context
    }


async def owned_thread(session: AsyncSession, user_id: str, thread_id: str) -> ChatThread:
    thread = await session.scalar(
        select(ChatThread).where(ChatThread.user_id == user_id, ChatThread.id == thread_id)
    )
    if thread is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
    return thread


async def connection_thread(
    session: AsyncSession, user_id: str, connection_id: str
) -> ChatThread | None:
    """The thread of the newest task of this connection that was asked in a thread."""
    return await session.scalar(
        select(ChatThread)
        .join(Job, Job.thread_id == ChatThread.id)
        .where(
            Job.user_id == user_id,
            Job.connection_id == connection_id,
            ChatThread.user_id == user_id,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )


async def thread_connection_ids(session: AsyncSession, user_id: str, thread_id: str) -> list[str]:
    """Connections that have tasks in this thread, newest first."""
    rows = await session.execute(
        select(Job.connection_id)
        .where(
            Job.user_id == user_id,
            Job.thread_id == thread_id,
            Job.connection_id.is_not(None),
        )
        .group_by(Job.connection_id)
        .order_by(Job.connection_id)
    )
    return [str(row[0]) for row in rows]


def _new_thread(user_id: str, space_id: str | None, title: str) -> ChatThread:
    return ChatThread(
        id=new_id(), user_id=user_id, space_id=space_id, title=title[:200].strip() or "Новый чат"
    )


async def attach_conversation(
    session: AsyncSession, services: Services, user_id: str, connection_id: str, job_id: str | None
) -> ChatThread:
    """The thread that shows a connection's task; created and filled from the job's history
    when the task was started outside any thread (a source plate, the scheduler's retry)."""
    conn = await session.scalar(
        select(Connection)
        .where(Connection.id == connection_id, Connection.user_id == user_id)
        .with_for_update()
    )
    if conn is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
    if conn.status == "disabled":
        raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
    query = select(Job).where(Job.user_id == user_id, Job.connection_id == conn.id)
    if job_id is not None:
        query = query.where(Job.id == job_id)
    job = await session.scalar(
        query.order_by(Job.created_at.desc(), Job.id.desc()).limit(1).with_for_update()
    )
    if job_id is not None and job is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание подключения не найдено")
    thread: ChatThread | None = None
    if job is not None and job.thread_id:
        thread = await owned_thread(session, user_id, job.thread_id)
    if thread is None:
        thread = await connection_thread(session, user_id, conn.id)
    now = services.clock.now()
    if thread is None:
        thread = _new_thread(user_id, conn.space_id, conn.title)
        session.add(thread)
        await session.flush()
    if job is not None and job.thread_id is None:
        job.thread_id = thread.id
        await _project_history(session, job)
        await dialogue.project_terminal(session, job, now)
    await session.commit()
    return thread


async def _project_history(session: AsyncSession, job: Job) -> None:
    """Replay the safe public blocks of a task that predates its thread."""
    events = await session.scalars(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.seq)
    )
    blocks: dict[str, dict[str, Any]] = {}
    for event in events:
        data = dict(event.data)
        if event.type == "block_update" and data.get("id") in blocks:
            data = {**blocks[str(data["id"])], **(data.get("patch") or {})}
        elif event.type != "block":
            continue
        try:
            block = _BLOCK.validate_python(data)
        except ValidationError:
            continue
        blocks[block.id] = block.model_dump(by_alias=True, mode="json", exclude_none=True)
        await dialogue.project_block(session, job, blocks[block.id], event.created_at)
        if job.state == "waiting_user" and block.type in ASKING_TYPES:
            dialogue.set_pending(
                job, block, channel=dialogue.channel_of(block), now=event.created_at
            )
    answers = await session.scalars(select(JobAnswer).where(JobAnswer.job_id == job.id))
    for answer in answers:
        dialogue.complete_pending(job, answer.block_id, now=answer.created_at, status="accepted")


async def continue_source(
    ctx: JobContext, services: Services, parent_connection_id: str, url: str, intent: dict[str, Any]
) -> dict[str, str]:
    """A link on another site becomes a separate source of the same university, in this thread."""
    url = safe_url(url)
    context = normalize_context(intent)
    context["entry_urls"] = [url]
    origin = url_origin(url)
    async with services.db() as session:
        parent = await session.scalar(
            select(Connection).where(
                Connection.user_id == ctx.user_id, Connection.id == parent_connection_id
            )
        )
        if parent is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Активное подключение не найдено")
        space = await session.scalar(
            select(Space)
            .where(Space.id == parent.space_id, Space.user_id == ctx.user_id)
            .with_for_update()
        )
        if space is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")
        # Resolve the existing destination without row locks. A producer holding its
        # source group may need Connection KEY SHARE to insert a job, so neither the
        # parent's nor the child's Connection/Job rows may be locked before these groups.
        child_id = await session.scalar(
            select(Connection.id)
            .where(
                Connection.user_id == ctx.user_id,
                Connection.space_id == space.id,
                Connection.origin == origin,
            )
            .order_by(Connection.created_at, Connection.id)
            .limit(1)
        )
        await lock_sources(session, [parent_connection_id, *([child_id] if child_id else [])])
        parent = await session.scalar(
            select(Connection)
            .where(Connection.user_id == ctx.user_id, Connection.id == parent_connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = await session.scalar(
            select(Job).where(Job.user_id == ctx.user_id, Job.id == ctx.job_id)
        )
        if (
            parent is None
            or parent.space_id != space.id
            or job is None
            or job.connection_id != parent.id
            or job.state not in IN_FLIGHT
        ):
            raise BotaiError(ErrorCode.NOT_FOUND, "Активное подключение не найдено")
        if parent.status == "disabled":
            raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
        if origin == parent.origin:
            raise BotaiError(
                ErrorCode.VALIDATION, "Продолжение на том же сайте не требует нового подключения"
            )
        child = await _child_connection(session, ctx.user_id, space, url, origin)
        job = await session.scalar(
            select(Job)
            .where(Job.user_id == ctx.user_id, Job.id == ctx.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or job.connection_id != parent.id or job.state not in IN_FLIGHT:
            raise BotaiError(ErrorCode.NOT_FOUND, "Активное подключение уже завершено")
        await guard_job(ctx, session, check_input=True)
        thread_id = job.thread_id
        if thread_id is None:
            thread = await connection_thread(session, ctx.user_id, parent.id)
            if thread is None:
                thread = _new_thread(ctx.user_id, parent.space_id, parent.title)
                session.add(thread)
                await session.flush([thread])
            thread_id = job.thread_id = thread.id
        with session.no_autoflush:
            child_job = await enqueue_in_session(
                session,
                JobKind.CONNECT,
                user_id=ctx.user_id,
                connection_id=child.id,
                thread_id=thread_id,
                payload={
                    "url": url,
                    "learning_intent": intent_of(context),
                    "parent_job_id": job.id,
                },
                now=services.clock.now(),
            )
        if child_job.thread_id != thread_id:
            raise BotaiError(ErrorCode.CONFLICT, "Источник уже подключается в другом разговоре")
        job.payload = {**job.payload, "continuation_job_id": child_job.id}
        await session.commit()
        return {"connectionId": child.id, "jobId": child_job.id}


async def _child_connection(
    session: AsyncSession, user_id: str, space: Space, url: str, origin: str
) -> Connection:
    query = (
        select(Connection)
        .where(
            Connection.user_id == user_id,
            Connection.space_id == space.id,
            Connection.origin == origin,
        )
        .order_by(Connection.created_at, Connection.id)
        .limit(1)
    )
    conn = await session.scalar(query.with_for_update().execution_options(populate_existing=True))
    if conn is not None and conn.status == "disabled":
        raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
    if conn is None:
        host = urlsplit(url).hostname or ""
        conn = Connection(
            id=new_id(),
            user_id=user_id,
            space_id=space.id,
            origin=origin,
            kind="other",
            title=(host or "Источник")[:120],
            status="draft",
            allow_hosts=[host],
        )
        session.add(conn)
        await lock_sources(session, [conn.id])
        await session.flush([conn])
    return conn
