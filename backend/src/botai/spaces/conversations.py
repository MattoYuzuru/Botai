"""Owned source conversations: binding, continuation and bootstrap without a configuration page."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import InputBlock, TextBlock
from botai.core.dialogue import DialogueContext
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import guard_job
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import ChatThread, Connection, Job, JobAnswer, JobEvent, Space, University
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT, append_event
from botai.jobs.queue import enqueue_in_session, lock_sources
from botai.learn.addresses import navigation_url, url_origin
from botai.learn.intent import prepare_turn
from botai.learn.registry import clean_warning
from botai.sources.vocab import KIND_LABELS
from botai.spaces import catalog, repo, universities


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


async def canonical_thread(
    session: AsyncSession, user_id: str, connection_id: str
) -> ChatThread | None:
    return await session.scalar(
        select(ChatThread)
        .where(
            ChatThread.user_id == user_id,
            ChatThread.connect_context["connection_ids"].contains([connection_id]),
        )
        .order_by(ChatThread.created_at, ChatThread.id)
        .limit(1)
    )


def bind_thread(thread: ChatThread, connection_id: str) -> None:
    ids = list(thread.connect_context.get("connection_ids") or [])
    if connection_id not in ids:
        ids.append(connection_id)
    thread.connect_context = {
        **thread.connect_context,
        "connection_id": connection_id,
        "connection_ids": ids,
    }


async def make_connection(
    session: AsyncSession, user_id: str, thread: ChatThread, url: str
) -> Connection:
    context = thread.connect_context
    origin = url_origin(url)
    if thread.space_id is None:
        university_id = context.get("university_id")
        uni = (
            await universities.find_or_create(
                session,
                title=str(context.get("title") or university_id),
                university_id=str(university_id),
            )
            if university_id
            else await universities.find_or_create(
                session,
                title=str(context.get("title") or urlsplit(url).hostname or "Источник"),
                homepage_url=origin,
            )
        )
        # Distinct chat drafts for the same known university must resolve one owned space.
        # This precedes Connection/Job/Thread locks; explicit space_id bypasses this lookup.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"university-space:{user_id}:{uni.id}", 0)
                )
            )
        )
        space = await session.scalar(
            select(Space)
            .where(Space.user_id == user_id, Space.university_id == uni.id)
            .order_by(Space.created_at, Space.id)
            .limit(1)
            .with_for_update()
        )
        if space is None:
            space = Space(
                user_id=user_id, university_id=uni.id, title=uni.title, accent_hue=uni.accent_hue
            )
            session.add(space)
            await session.flush([space])
        thread.space_id = space.id
    space = await repo.get_space(session, user_id, thread.space_id)
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
    conn = await session.scalar(query)
    if conn is not None:
        await lock_sources(session, [conn.id])
        conn = await session.scalar(
            query.where(Connection.id == conn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if conn is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Подключение больше недоступно")
    if conn is not None and conn.status == "disabled":
        raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
    if conn is None:
        conn = Connection(
            id=new_id(),
            user_id=user_id,
            space_id=space.id,
            origin=origin,
            kind=str(context.get("kind") or "other"),
            title=str(context.get("title") or urlsplit(url).hostname or "Источник")[:120],
            status="draft",
            allow_hosts=[urlsplit(url).hostname or ""],
        )
        session.add(conn)
        await lock_sources(session, [conn.id])
        await session.flush([conn])
    bind_thread(thread, conn.id)
    return conn


async def bind_bootstrap(
    ctx: JobContext, services: Services, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Resolve an optional initial context in the same CONNECT job before Explorer starts."""
    async with services.db() as session:
        job = await session.scalar(
            select(Job).where(Job.id == ctx.job_id, Job.user_id == ctx.user_id)
        )
        if job is None or job.state not in IN_FLIGHT or job.thread_id is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Задание подключения не найдено")
        thread = await owned_thread(session, ctx.user_id, job.thread_id)
        context = normalize_context(thread.connect_context)
        if job.connection_id is not None:
            await repo.get_connection(session, ctx.user_id, job.connection_id)
            ctx.connection_id = job.connection_id
            return job.connection_id, {**payload, "learning_intent": intent_of(context)}
        urls = list(context.get("entry_urls") or [])
        if not urls and context.get("university_id"):
            uni = await session.get(University, str(context["university_id"]))
            known = [safe_url(value) for value in (uni.origins if uni else [])]
            if len(known) == 1:
                urls = known
    while not urls:
        title = context.get("title")
        question = (
            f"Для «{title}» пока нет проверенного адреса. Пришлите ссылку на сайт или учебный раздел."
            if title
            else "Какой вуз или сайт подключить? Можно назвать вуз или сразу прислать ссылку."
        )
        block = InputBlock(
            id=new_id(),
            at=services.clock.now(),
            question=question,
            input_kind="text",
            required=True,
            max_length=2000,
        )
        if isinstance(ctx, DialogueContext):
            text = await ctx.ask_dialogue(block)
        else:
            answer = await ctx.ask(block)
            text = answer.values if isinstance(answer.values, str) else ""
        text = safe_text(text)
        direct = navigation_url(text)
        turn = prepare_turn(text)
        # An address is already durable in entry_urls. Treating it as prose makes restore
        # redact it to "[ссылка]" and needlessly classify that placeholder before real dialogue.
        if not direct:
            context["user_note"] = text[:2000]
        if not direct and not turn.links:
            suggestion = catalog.identify(text)
            if suggestion:
                context["university_id"] = suggestion.id
                context["title"] = suggestion.title
            elif len(text.strip()) <= 120:
                context["title"] = text.strip()
            async with services.db() as session:
                current = await owned_thread(session, ctx.user_id, job.thread_id)
                current.connect_context = {**current.connect_context, **context}
                await session.commit()
            if isinstance(ctx, DialogueContext):
                await ctx.checkpoint_learning(intent_of(context))
        urls = [direct] if direct else list(turn.links)
        if not urls:
            await ctx.status("Жду обычную ссылку на источник")
    context["entry_urls"] = list(dict.fromkeys(urls))
    async with services.db() as session:
        thread = await owned_thread(session, ctx.user_id, thread.id)
        if thread.space_id is not None:
            space = await session.scalar(
                select(Space)
                .where(Space.id == thread.space_id, Space.user_id == ctx.user_id)
                .with_for_update()
            )
            if space is None:
                raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")
        # Keep the global Space -> Connection -> Job -> Thread writer order, including ORM flushes.
        with session.no_autoflush:
            thread.connect_context = {**thread.connect_context, **context}
            conn = await make_connection(session, ctx.user_id, thread, urls[0])
        with session.no_autoflush:
            job = await session.scalar(
                select(Job)
                .where(Job.id == ctx.job_id, Job.user_id == ctx.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if job is None or job.state not in IN_FLIGHT or job.thread_id != thread.id:
            raise BotaiError(ErrorCode.NOT_FOUND, "Задание подключения уже завершено")
        await guard_job(ctx, session, check_input=True)
        other = await session.scalar(
            select(Job)
            .where(
                Job.user_id == ctx.user_id,
                Job.connection_id == conn.id,
                Job.kind == "connect",
                Job.state.in_(IN_FLIGHT),
                Job.id != job.id,
            )
            .limit(1)
        )
        if other is not None:
            raise BotaiError(
                ErrorCode.CONFLICT, "Это подключение уже настраивается в другом разговоре"
            )
        job.connection_id = conn.id
        job.payload = {**job.payload, "url": urls[0], "learning_intent": intent_of(context)}
        university = await session.scalar(
            select(University)
            .join(Space, Space.university_id == University.id)
            .where(Space.id == conn.space_id, Space.user_id == ctx.user_id)
        )
        warning = clean_warning(university.third_party_warning) if university else None
        if warning and not job.payload.get("university_warning_block_id"):
            now = services.clock.now()
            block = TextBlock(
                id=new_id(),
                at=now,
                markdown=f"**Важно.** {warning} Вы можете продолжить подключение.",
            )
            job.payload = {**job.payload, "university_warning_block_id": block.id}
            data = block.model_dump(by_alias=True, mode="json", exclude_none=True)
            await dialogue.project_block(session, job, data, now)
            await append_event(session, job.id, "block", data)
        await session.commit()
        payload = dict(job.payload)
    ctx.connection_id = conn.id
    if isinstance(ctx, DialogueContext):
        await ctx.checkpoint_learning(intent_of(context))
    return conn.id, payload


async def attach_conversation(
    session: AsyncSession, services: Services, user_id: str, connection_id: str, job_id: str | None
) -> ChatThread:
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
    thread = await canonical_thread(session, user_id, conn.id)
    if thread is None and job is not None and job.thread_id:
        thread = await owned_thread(session, user_id, job.thread_id)
    now = services.clock.now()
    if thread is None:
        thread = ChatThread(
            id=new_id(),
            user_id=user_id,
            space_id=conn.space_id,
            title=f"Подключение: {conn.title}"[:200],
            purpose="connect",
            connect_context={},
            created_at=now,
            updated_at=now,
        )
        session.add(thread)
        await session.flush()
    thread.purpose = "connect"
    bind_thread(thread, conn.id)
    if job is not None:
        if job.thread_id is not None and job.thread_id != thread.id:
            raise BotaiError(ErrorCode.CONFLICT, "Задание уже привязано к другому чату")
        job.thread_id = thread.id
        from pydantic import TypeAdapter, ValidationError

        from botai.contracts.agent_ui import ASKING_TYPES, AgentUiBlock

        adapter = TypeAdapter(AgentUiBlock)
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
                block = adapter.validate_python(data)
            except ValidationError:
                continue
            blocks[block.id] = block.model_dump(by_alias=True, mode="json", exclude_none=True)
            await dialogue.project_block(session, job, blocks[block.id], event.created_at)
            if job.state == "waiting_user" and block.type in ASKING_TYPES:
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
                dialogue.set_pending(job, block, channel=channel, now=event.created_at)
        answers = await session.scalars(select(JobAnswer).where(JobAnswer.job_id == job.id))
        for answer in answers:
            dialogue.complete_pending(
                job, answer.block_id, now=answer.created_at, status="accepted"
            )
        await dialogue.project_terminal(session, job, now)
    await session.commit()
    return thread


async def continue_source(
    ctx: JobContext, services: Services, parent_connection_id: str, url: str, intent: dict[str, Any]
) -> dict[str, str]:
    url = safe_url(url)
    context = normalize_context(intent)
    context["entry_urls"] = [url]
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
                Connection.origin == url_origin(url),
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
        if url_origin(url) == parent.origin:
            raise BotaiError(
                ErrorCode.VALIDATION, "Продолжение на том же сайте не требует нового подключения"
            )
        if job.thread_id is None:
            # Parent row serializes the canonical binding with POST /conversation.
            thread = await canonical_thread(session, ctx.user_id, parent.id)
            if thread is None:
                now = services.clock.now()
                thread = ChatThread(
                    id=new_id(),
                    user_id=ctx.user_id,
                    space_id=parent.space_id,
                    title=f"Подключение: {parent.title}"[:200],
                    purpose="connect",
                    connect_context={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(thread)
                await session.flush([thread])
        else:
            thread = await owned_thread(session, ctx.user_id, job.thread_id)
        if thread.space_id != parent.space_id:
            raise BotaiError(ErrorCode.FORBIDDEN, "Источник относится к другому пространству")
        with session.no_autoflush:
            bind_thread(thread, parent.id)
            thread.connect_context = {
                **thread.connect_context,
                **context,
                "kind": "other",
                "title": urlsplit(url).hostname,
            }
            child = await make_connection(session, ctx.user_id, thread, url)
            job = await session.scalar(
                select(Job)
                .where(Job.user_id == ctx.user_id, Job.id == ctx.job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if (
            job is None
            or job.connection_id != parent.id
            or job.state not in IN_FLIGHT
            or job.thread_id not in (None, thread.id)
        ):
            raise BotaiError(ErrorCode.NOT_FOUND, "Активное подключение уже завершено")
        await guard_job(ctx, session, check_input=True)
        job.thread_id = thread.id
        with session.no_autoflush:
            child_job = await enqueue_in_session(
                session,
                JobKind.CONNECT,
                user_id=ctx.user_id,
                connection_id=child.id,
                thread_id=thread.id,
                payload={
                    "url": url,
                    "learning_intent": intent_of(context),
                    "parent_job_id": job.id,
                },
                now=services.clock.now(),
            )
        if child_job.thread_id != thread.id:
            raise BotaiError(ErrorCode.CONFLICT, "Источник уже подключается в другом разговоре")
        job.payload = {**job.payload, "continuation_job_id": child_job.id}
        await session.commit()
        return {"connectionId": child.id, "jobId": child_job.id}
