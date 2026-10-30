"""Tenant-scoped non-secret learning turns and the durable public conversation projection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import (
    QUOTE_MAX,
    AgentUiBlock,
    ApprovalBlock,
    ChoiceBlock,
    ConfirmBlock,
    InputBlock,
    Quote,
)
from botai.core.diagnostics import diagnostic_event
from botai.core.dialogue import DialogueTurn
from botai.core.ids import new_id
from botai.db.models import ChatMessage, ChatThread, Job, JobEvent
from botai.jobs.events import TERMINAL, iso


def clip_quote(text: str) -> str:
    """One line of at most `QUOTE_MAX` characters; a cut shows as an ellipsis."""
    text = " ".join(text.split())
    return text if len(text) <= QUOTE_MAX else text[: QUOTE_MAX - 1].rstrip() + "…"


def question_of(block: AgentUiBlock) -> str | None:
    """What an asking block asks, as plain text for a quote."""
    if isinstance(block, ApprovalBlock):
        return block.proposal.intent
    if isinstance(block, ChoiceBlock | InputBlock | ConfirmBlock):
        return block.question
    return None


def quote_of_block(block: AgentUiBlock) -> Quote | None:
    text = question_of(block)
    return Quote(text=clip_quote(text), block_id=block.id) if text and text.strip() else None


async def question_quote(session: AsyncSession, job: Job) -> Quote | None:
    """The quote of the open plain-text question of a task (`pending.channel == "dialogue"`):
    the student's reply in the composer then answers it."""
    pending = state_of(job).get("pending")
    if not isinstance(pending, dict) or pending.get("channel") != "dialogue":
        return None
    block_id = pending.get("block_id")
    if not isinstance(block_id, str):
        return None
    data = await session.scalar(
        select(JobEvent.data)
        .where(
            JobEvent.job_id == job.id,
            JobEvent.type == "block",
            JobEvent.data["id"].astext == block_id,
        )
        .order_by(JobEvent.seq.desc())
        .limit(1)
    )
    question = data.get("question") if isinstance(data, dict) else None
    if not isinstance(question, str) or not question.strip():
        return None
    return Quote(text=clip_quote(question), block_id=block_id)


def state_of(job: Job) -> dict[str, Any]:
    value = job.payload.get("dialogue")
    return dict(value) if isinstance(value, dict) else {}


def save_state(job: Job, state: dict[str, Any]) -> None:
    job.payload = {**job.payload, "dialogue": state}


def input_revision(job: Job) -> int:
    return payload_input_revision(job.payload)


def payload_input_revision(payload: Mapping[str, Any]) -> int:
    state = payload.get("dialogue")
    value = state.get("input_revision", 0) if isinstance(state, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def advance_input(job: Job) -> None:
    state = state_of(job)
    state["input_revision"] = input_revision(job) + 1
    save_state(job, state)


def is_auth_input(block: AgentUiBlock) -> bool:
    """A field of a login: marked so by its author, or recognisable by what it is."""
    return isinstance(block, InputBlock) and (
        block.login
        or (
            block.input_kind != "url"
            and (
                block.input_kind in {"password", "code"}
                or block.autocomplete in {"username", "current-password", "one-time-code"}
            )
        )
    )


def channel_of(block: AgentUiBlock) -> str:
    """How the student answers an asking block: see ThreadRuntime.pending[].channel."""
    if block.type == "login":
        return "manual"
    if block.type == "approval":
        return "approval"
    if isinstance(block, InputBlock) and not block.secret and not is_auth_input(block):
        return "dialogue"
    return "native"


def receipt(
    job: Job,
    *,
    now: datetime,
    status: str,
    message_id: str | None = None,
    block_id: str | None = None,
) -> None:
    if (message_id is None) == (block_id is None):
        raise ValueError("a receipt identifies exactly one message or block")
    state = state_of(job)
    receipts = dict(state.get("receipts") or {})
    key = f"message:{message_id}" if message_id else f"block:{block_id}"
    receipts[key] = {
        "job_id": job.id,
        "message_id": message_id,
        "block_id": block_id,
        "status": status,
        "at": iso(now),
    }
    state["receipts"] = receipts
    save_state(job, state)

    diagnostic_event(
        "dialogue.receipt",
        job_id=job.id,
        thread_id=job.thread_id,
        connection_id=job.connection_id,
        message_id=message_id,
        block_id=block_id,
        receipt_status=status,
    )


def set_pending(job: Job, block: AgentUiBlock, *, channel: str, now: datetime) -> None:
    previous = state_of(job).get("pending")
    if isinstance(previous, dict) and previous.get("block_id") != block.id:
        receipt(job, now=now, status="superseded", block_id=str(previous["block_id"]))
    state = state_of(job)
    state["pending"] = {
        "job_id": job.id,
        "block_id": block.id,
        "channel": channel,
        "secret": isinstance(block, InputBlock) and block.secret,
    }
    state["pending_kind"] = block.type
    save_state(job, state)


def complete_pending(job: Job, block_id: str, *, now: datetime, status: str = "applied") -> None:
    receipt(job, now=now, status=status, block_id=block_id)
    state = state_of(job)
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("block_id") == block_id:
        state.pop("pending", None)
        state.pop("pending_kind", None)
    save_state(job, state)


async def supersede_old_question(session: AsyncSession, job: Job, now: datetime) -> None:
    """New research input retires a previous data choice, never authentication or approval."""
    engine = job.payload.get("_research_engine")
    if type(engine) is not int or engine != 1:
        return
    state = state_of(job)
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("channel") != "native" or pending.get("secret"):
        return
    block_id = pending.get("block_id")
    if not isinstance(block_id, str):
        return
    kind = state.get("pending_kind")
    if not isinstance(kind, str):
        # Legacy pending descriptors have no kind; absent/trimmed evidence is not
        # permission to retire a protected control.
        kind = await session.scalar(
            select(JobEvent.data["type"].astext)
            .where(
                JobEvent.job_id == job.id,
                JobEvent.type == "block",
                JobEvent.data["id"].astext == block_id,
            )
            .order_by(JobEvent.seq.desc())
            .limit(1)
        )
    if kind in {"choice", "confirm"}:
        complete_pending(job, block_id, now=now, status="superseded")


async def read_turns(session: AsyncSession, job: Job) -> list[DialogueTurn]:
    if job.thread_id is None:
        return []
    state = state_of(job)
    applied = set(state.get("applied_ids") or [])
    entries = [
        e
        for e in state.get("inbox", [])
        if isinstance(e, dict) and e.get("message_id") not in applied
    ]
    ids = [str(e["message_id"]) for e in entries]
    if not ids:
        return []
    rows = await session.scalars(
        select(ChatMessage).where(
            ChatMessage.user_id == job.user_id,
            ChatMessage.thread_id == job.thread_id,
            ChatMessage.job_id == job.id,
            ChatMessage.role == "user",
            ChatMessage.id.in_(ids),
        )
    )
    by_id = {row.id: row for row in rows}
    turns: list[DialogueTurn] = []
    for entry in entries:
        row = by_id.get(str(entry["message_id"]))
        if row is None:
            continue
        text = "\n\n".join(
            str(b.get("markdown", "")) for b in row.blocks if b.get("type") == "text"
        )
        turns.append(DialogueTurn(row.id, text, row.created_at, entry.get("reply_to_block_id")))
    return turns


async def message_time(
    session: AsyncSession, user_id: str, thread_id: str | None, now: datetime
) -> datetime:
    """Serialize thread appends; even two events in one clock tick keep their public order."""
    await session.scalar(
        select(ChatThread)
        .where(
            ChatThread.id == thread_id,
            ChatThread.user_id == user_id,
        )
        .with_for_update()
    )
    last = await session.scalar(
        select(func.max(ChatMessage.created_at)).where(
            ChatMessage.thread_id == thread_id,
            ChatMessage.user_id == user_id,
        )
    )
    return max(now, last + timedelta(microseconds=1)) if last is not None else now


async def project_block(
    session: AsyncSession, job: Job, data: dict[str, Any], now: datetime
) -> None:
    """One stable message per public block of a task, committed with its SSE event; never
    credentials or tickets. The thread's `last_message_at` follows the insert."""
    if job.thread_id is None or job.kind == "chat":
        return
    thread = await session.scalar(
        select(ChatThread).where(ChatThread.id == job.thread_id, ChatThread.user_id == job.user_id)
    )
    if thread is None:
        return
    kind = data.get("type")
    safe = dict(data)
    # A confirmation with a body keeps its form: what is confirmed reads with the question. It holds
    # no credential, and as a block of its own it stays out of the text a model reads as history.
    keeps_form = kind == "confirm" and bool(str(data.get("details") or "").strip())
    if kind in {"login", "input", "choice", "confirm", "approval", "wait"} and not keeps_form:
        label = data.get("question") or data.get("message") or "Ручной вход в браузере"
        safe = {"type": "text", "id": data["id"], "at": data["at"], "markdown": str(label)[:500]}
    existing = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.user_id == job.user_id,
            ChatMessage.thread_id == job.thread_id,
            ChatMessage.job_id == job.id,
            ChatMessage.role == "agent",
            ChatMessage.blocks[0]["id"].astext == str(data["id"]),
        )
    )
    if existing is None:
        session.add(
            ChatMessage(
                id=new_id(),
                user_id=job.user_id,
                thread_id=job.thread_id,
                role="agent",
                blocks=[safe],
                job_id=job.id,
                created_at=await message_time(session, job.user_id, job.thread_id, now),
            )
        )
    else:
        existing.blocks = [safe]  # creation time and message identity never move on a stage update
    await session.flush()


# What the student reads when a task ends: (cancelled, failed, done) by kind.
_TERMINAL_TEXT: dict[str, tuple[str, str, str]] = {
    "connect": (
        "Подключение отменено.",
        "Подключение не завершено. Можно уточнить источник или продолжить настройку.",
        "",  # a connection ends with its own closing message (what is connected, how it updates)
    ),
    "repair": (
        "Починка остановлена.",
        "Починка не завершена. Прежние данные остаются доступными.",
        "Починка завершена.",
    ),
    "sync": (
        "Обновление остановлено.",
        "Обновить данные не удалось. Прежние данные остаются доступными.",
        "Данные источника обновлены.",
    ),
    "ingest": (
        "Загрузка документа остановлена.",
        "Документ добавить не удалось.",
        "",  # a document reports its own summary
    ),
}
# A connect task that adds kinds to a source that works: the source keeps working whatever happens.
_EXTENSION_TEXT = (
    "Добавление остановлено. Подключённые данные работают как раньше.",
    "Добавить данные не удалось. Подключённые данные работают как раньше.",
    "",  # it ends with its own closing message
)
_UNSETTLED = {"unresolved", "rolled_back", "skipped"}


def terminal_text(job: Job) -> str | None:
    """None: the task already told its outcome itself (a connection and a document both do)."""
    extension = job.kind == "connect" and job.payload.get("extend") is True
    cancelled, failed_text, done = (
        _EXTENSION_TEXT if extension else _TERMINAL_TEXT.get(job.kind, _TERMINAL_TEXT["connect"])
    )
    status = (job.result or {}).get("status")
    failed = job.state == "failed" or status == "error"
    if job.state == "cancelled":
        return cancelled
    if extension and (job.result or {}).get("extension") is True and status in {"error", "skipped"}:
        return None  # the task told it in its own words (the source works as before)
    if status == "skipped" and (job.result or {}).get("detail") == "calendar":
        return None  # the thread already holds the question about the calendar
    if failed:
        detail = (job.error or {}).get("detail")
        return failed_text + (" " + detail[:500] if isinstance(detail, str) and detail else "")
    if status == "continued":
        return "Продолжаю подключение по новой ссылке в этом разговоре."
    if status in _UNSETTLED:
        if extension:
            return "Добавление не выполнено. Подключённые данные работают как раньше."
        return "Автоматическая настройка не завершена. Можно уточнить источник или продолжить."
    if status == "healthy":
        return "Источник работает, чинить нечего."
    return done or None


async def project_terminal(session: AsyncSession, job: Job, now: datetime) -> None:
    if job.thread_id is None or job.kind == "chat" or job.state not in TERMINAL:
        return
    state = state_of(job)
    pending = state.get("pending")
    if isinstance(pending, dict):
        complete_pending(job, str(pending["block_id"]), now=now, status="superseded")
    state = state_of(job)
    for entry in state.get("inbox", []):
        message_id = entry.get("message_id")
        if message_id and message_id not in set(state.get("applied_ids") or []):
            receipt(job, now=now, status="rejected", message_id=str(message_id))
    text = terminal_text(job)
    state = state_of(job)
    if text is None:
        save_state(job, state)
        return
    block_id = state.get("terminal_block_id") or new_id()
    state["terminal_block_id"] = block_id
    save_state(job, state)
    await project_block(
        session,
        job,
        {"type": "text", "id": block_id, "at": iso(now), "markdown": text},
        now,
    )
