"""Threads: creation, routing of a student's message, and the live runtime of a thread.

A thread has one agent. A message goes to a task of the thread only when that task is waiting for
a plain, non-secret answer in the dialogue; every other message starts an agent turn (`CHAT`),
whatever background tasks are running. Nothing here knows what the thread was "for".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.safety import scrub_secrets
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
from botai.attachments import service as attachments
from botai.contracts.agent_ui import AgentUiBlock, AttachmentBlock, Quote, TextBlock
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
from botai.spaces import repo, service
from botai.spaces.conversations import owned_thread, reject_secret_links, safe_text

_BLOCK = TypeAdapter(AgentUiBlock)
RUNTIME_TASKS = 20
RUNTIME_RECEIPTS = 200
_ASKING = {"input", "choice", "confirm", "approval", "login", "wait"}
SECRET_IN_CHAT_TEXT = "Пароль и код вводятся только в защищённое поле, не в чат"  # noqa: S105


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
                    ErrorCode.REQUEST_REUSED, "Этот запрос уже использован для другого разговора"
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


# A word for a code, then (at most three filler words) digits: «код 123 456», «код из смс 424242»,
# «код подтверждения 1234». «Код группы 2101» has a word that is no filler, so it is a fact.
_CODE_WITH_DIGITS = re.compile(
    r"(?i)\b(?:код\w*|code|otp|смс|sms|пин|pin)\b"
    r"(?:\s+(?:из|от|для|в|на|с|смс|sms|почты|письма|подтвержден\w+|доступ\w*|вход\w*|"
    r"безопасност\w+|авторизаци\w+|верификаци\w+|приложени\w+|телефон\w*|сообщени\w+|"
    r"одноразов\w+|лк|кабинет\w*)){0,3}"
    r"\s*[:=—-]?\s*(?P<digits>\d[\d \-]*\d|\d)"
)
_PASSWORD_WORD = re.compile(r"(?i)\b(?:парол\w*|password|passwd|pwd|пасс\w*|pass)\b")
_PASSWORD_FILLER = re.compile(r"(?i)^(?:(?:от|для|к|from|for)\s+\S+(?:\s+|$)|(?:is|это|такой)\s+)")
_GROUP_CODE = re.compile(r"^[A-ZА-ЯЁ]{2,6}[- ]?\d{1,3}$")  # ИВТ-21, БПМ231
_WORD_AND_YEAR = re.compile(r"^[А-Яа-яЁё]+(?:19|20)\d\d$")  # Москва2024
_EDGE_PUNCTUATION = ".,;:!?\"'()«»[]"


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def _password_like(word: str) -> bool:
    """One word that has the look of a password: letters and digits together, a symbol or a capital
    inside it. A group («ИВТ-21»), a word with a year («Москва2024») and a plain word are not."""
    token = word.strip(_EDGE_PUNCTUATION)
    if len(token) < 4 or len(token) > 64 or _GROUP_CODE.match(token) or _WORD_AND_YEAR.match(token):
        return False
    if any(ch.isalpha() for ch in token) and _digits(token) >= 1 and len(token) >= 5:
        return True
    if len(token) >= 16 and token.count("-") + token.count("_") >= 2:
        return True  # a passphrase, a generated key
    return bool(re.search(r"\w[^\w\s\-'’.,]\w", token) or re.search(r"[a-zа-яё][A-ZА-ЯЁ]", token))


def _follows_password_word(value: str) -> bool:
    """«пароль: x», «мой пасс Qwerty123», «пароль от ЛК qwerty12»: a word follows the word for a
    password. «Где мой пароль?» has nothing after it, and a long question is a question."""
    tokens = value.split()
    for found in _PASSWORD_WORD.finditer(value):
        rest = value[found.end() :]
        separated = bool(re.match(r"\s*[:=—-]", rest))
        rest = _PASSWORD_FILLER.sub("", re.sub(r"^[\s:=—-]+", "", rest))
        following = rest.split(None, 1)[0] if rest.strip() else ""
        if not re.search(r"\w", following):
            continue
        if separated or _password_like(following) or len(tokens) <= 3:
            return True
    return False


def looks_like_secret(text: str) -> bool:
    """Whether a message typed while a protected input is open is probably the password or the
    code meant for it. The words of a conversation («да», «ок», «Привет!», «?», «2», «ИВТ-21»,
    «Код группы 2101») are not."""
    value = text.strip()
    if not value:
        return False
    if scrub_secrets(value) != value:
        return True  # a token, a cookie line, «token=…», a credential-named field
    if any(_digits(m["digits"]) >= 4 for m in _CODE_WITH_DIGITS.finditer(value)):
        return True  # «код 123 456», «код из смс 424242»
    if _follows_password_word(value):
        return True
    if re.fullmatch(r"[\d \-]+", value) and _digits(value) >= 6:
        return True  # a code typed in groups; a group number («2101») is not one
    words = value.split()
    return len(words) <= 4 and any(_password_like(w) for w in words)


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


def _quote_of(body: MessageIn) -> Quote | None:
    """The quote a client sent with a message to the agent, as it is stored: plain one-line text under
    the secret rule of links. It only decorates, so a bad one is dropped instead of refusing the message."""
    if body.quote is None or not body.text.strip():
        return None
    text = " ".join(body.quote.text.split())
    try:
        text = reject_secret_links(text)
    except BotaiError:
        return None
    text = dialogue.clip_quote(text)
    return Quote(text=text, block_id=body.quote.block_id) if text else None


async def _verified(
    session: AsyncSession, user_id: str, thread_id: str, quote: Quote | None
) -> Quote | None:
    """The quote with a block id only if that block is an agent message of this thread; a block the
    thread does not have is not kept as a reference (the words of the quote still are)."""
    if quote is None or quote.block_id is None:
        return quote
    known = await session.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.user_id == user_id,
            ChatMessage.thread_id == thread_id,
            ChatMessage.role == "agent",
            ChatMessage.blocks.contains([{"id": quote.block_id}]),
        )
        .limit(1)
    )
    return quote if known is not None else Quote(text=quote.text)


async def post_message(
    session: AsyncSession, services: Services, user_id: str, thread_id: str, body: MessageIn
) -> MessageAccepted:
    thread = await owned_thread(session, user_id, thread_id)
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"thread-post:{thread.id}", 0)))
    )
    thread = await owned_thread(session, user_id, thread_id)  # deleted while we waited: not found
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
                ErrorCode.STALE_TARGET,
                "Этот шаг уже завершён. Откройте текущее состояние разговора",
            )
        if await _continued(session, target):
            raise BotaiError(
                ErrorCode.STALE_TARGET, "Этот шаг уже заменён. Откройте текущее состояние разговора"
            )
    else:
        # Files are for the agent: a task's plain question takes only words.
        waiting = next(
            (
                job
                for job in sorted(jobs, key=lambda j: (j.created_at, j.id), reverse=True)
                if job.kind in TASK_KINDS and _waiting_dialogue(job) and not body.attachment_ids
            ),
            None,
        )
        if waiting is not None:
            target = await _locked(session, user_id, waiting.id)
            if target is not None and not _waiting_dialogue(target):
                target = None  # answered while we were waiting for the lock
    if target is not None and body.attachment_ids:
        raise BotaiError(ErrorCode.VALIDATION, "Файлы отправляются обычным сообщением в чат")
    text = safe_text(body.text) if target is not None else reject_secret_links(body.text)
    # A reply to a task is quoted by the server from the question the task is asking; a quote of the
    # client (quick replies of the agent) is kept only for a message to the agent, and only with a block
    # that is in this thread.
    if target is not None:
        quote = await dialogue.question_quote(session, target) if text.strip() else None
    else:
        quote = await _verified(session, user_id, thread.id, _quote_of(body))
    if target is not None:
        if target.connection_id:
            conn = await repo.get_connection(session, user_id, target.connection_id)
            if conn.status == "disabled":
                raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
        if _waiting_secret(target):
            raise BotaiError(ErrorCode.SECRET_IN_CHAT, SECRET_IN_CHAT_TEXT)
    else:
        if any(job.kind == JobKind.CHAT.value for job in jobs):
            raise BotaiError(ErrorCode.AGENT_BUSY, "Ассистент ещё отвечает на прошлое сообщение")
        if any(_waiting_secret(job) for job in jobs) and looks_like_secret(text):
            raise BotaiError(ErrorCode.SECRET_IN_CHAT, SECRET_IN_CHAT_TEXT)
    # Appends to a thread are serialized: even two events in one clock tick keep their order.
    now = services.clock.now()
    at = await dialogue.message_time(session, user_id, thread.id, now)
    blocks: list[dict[str, Any]] = []
    if text.strip():
        block = TextBlock(id=new_id(), at=at, markdown=text, quote=quote)
        blocks.append(block.model_dump(by_alias=True, mode="json", exclude_none=True))
    message = ChatMessage(
        id=new_id(),
        user_id=user_id,
        thread_id=thread.id,
        role="user",
        blocks=blocks,
        client_message_id=body.client_message_id,
        created_at=at,
    )
    session.add(message)
    if body.attachment_ids:
        await _attach_files(session, services, user_id, thread, message, body.attachment_ids, at)
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
        dialogue.receipt(target, now=at, status="accepted", message_id=message.id)
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


async def _attach_files(
    session: AsyncSession,
    services: Services,
    user_id: str,
    thread: ChatThread,
    message: ChatMessage,
    ids: list[str],
    at: datetime,
) -> None:
    """The files go with the message: bound to it, shown as `attachment` blocks, and a document
    starts indexing into the thread's university (or the student's own materials) right away."""
    await session.flush()  # the message exists before files are bound to it
    refs = await attachments.bind_to_message(session, user_id, thread.id, message.id, ids)
    message.blocks = [
        *message.blocks,
        *(
            AttachmentBlock(
                id=new_id(),
                at=at,
                attachment_id=ref.id,
                name=ref.name,
                mime=ref.mime,
                size=ref.size,
                kind=ref.kind,
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
            for ref in refs
        ),
    ]
    documents = [ref for ref in refs if ref.kind == "document"]
    if not documents:
        return
    space_id = thread.space_id or (await service.ensure_space(session, user_id, None)).id
    for ref in documents:
        att = await attachments.get(session, user_id, ref.id)
        await attachments.start_ingest(session, att, space_id, thread.id, now=services.clock.now())


async def _continued(session: AsyncSession, job: Job) -> bool:
    """The task handed its work to a newer one (another site of the same chat)."""
    child_id = job.payload.get("continuation_job_id")
    if not isinstance(child_id, str):
        return False
    state = await session.scalar(
        select(Job.state).where(Job.id == child_id, Job.user_id == job.user_id)
    )
    return state in IN_FLIGHT


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
    sent_files = [
        str(b.get("attachmentId")) for b in existing.blocks if b.get("type") == "attachment"
    ]
    stored_quote = next(
        (
            b["quote"]["text"]
            for b in existing.blocks
            if b.get("type") == "text" and isinstance(b.get("quote"), dict)
        ),
        None,
    )
    to_task = existing_job is not None and existing_job.kind in TASK_KINDS
    submitted_quote = None if to_task else _quote_of(body)  # a task's quote is the server's
    if (
        stored != submitted
        or (submitted_quote is not None and submitted_quote.text != stored_quote)
        or sorted(sent_files) != sorted(dict.fromkeys(body.attachment_ids))
        or (body.target_job_id is not None and body.target_job_id != existing.job_id)
    ):
        raise BotaiError(
            ErrorCode.REQUEST_REUSED, "Этот запрос уже использован для другого сообщения"
        )
    if existing.job_id is None:
        raise BotaiError(ErrorCode.REQUEST_REUSED, "Сообщение ещё не связано с заданием")
    return MessageAccepted(message=message_out(existing), job_id=existing.job_id)
