"""Chat: the CHAT job handler and everything it assembles around the harness.

One job answers one student message: load the thread (tenant-scoped), fold old turns into the
summary, build the variable part (date, space profile, summary, recent turns, question), run the
harness with the read-only tools, turn `[S1]` citations into real sources, store and emit the
reply. Failures become a human answer, never a stack trace or a bare error code.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent import harness
from botai.agent.memory import (
    RECENT_TOKENS,
    history_messages,
    load_turns,
    parse_summary,
    refresh_summary,
    split_recent,
    summary_message,
)
from botai.agent.reader import DomainReader
from botai.agent.sources import Finalized, finalize
from botai.agent.tokens import estimate_messages
from botai.agent.tools import DATA_TOOLS, DOCUMENT_TOOLS, ChatEnv, build_chat_tools
from botai.contracts.agent_ui import TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobContext
from botai.core.llm import LlmMessage
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Space

log = logging.getLogger("botai.agent.chat")

DEFAULT_TITLE = "Новый чат"
WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
MAX_MARKDOWN = 7_500  # TextBlock.markdown is limited to 8000 characters
UNKNOWN_BUDGET = (
    "Не знаю: мне не хватило шагов, чтобы найти ответ. Уточните вопрос, например дату, курс "
    "или название документа, и я поищу снова."
)
INJECTION_NOTE = (
    "\n\nВ найденном тексте был фрагмент, похожий на инструкцию для ассистента. "
    "Я его не выполнял и убрал из рассмотрения."
)


def context_message(now: datetime, space: Space | None, space_titles: list[str]) -> LlmMessage:
    """Date, time zone and space profile. Day-level only, so the prefix cache survives the day."""
    tz = ZoneInfo(space.time_zone) if space else ZoneInfo("Europe/Moscow")
    local = now.astimezone(tz)
    lines = [
        "Контекст приложения (не слова студента):",
        f"Сегодня: {WEEKDAYS_RU[local.weekday()]}, {local.day} {MONTHS_RU[local.month - 1]} "
        f"{local.year} года. Часовой пояс: {tz.key}.",
    ]
    if space:
        profile = space.profile or {}
        extra = ", ".join(
            f"{label}: {profile[key]}"
            for key, label in (("programme", "программа"), ("year", "курс"))
            if profile.get(key)
        )
        lines.append(f"Пространство: «{space.title}»" + (f" ({extra})" if extra else "") + ".")
    elif space_titles:
        lines.append(
            "Пространства студента: "
            + ", ".join(f"«{t}»" for t in space_titles)
            + ". Если вопрос не уточняет вуз, отвечайте по всем."
        )
    else:
        lines.append("У студента пока нет подключённых вузов.")
    return LlmMessage(role="user", content="\n".join(lines))


def compose_answer(
    result: harness.HarnessResult, env: ChatEnv, now: datetime
) -> tuple[str, Finalized | None]:
    """The text for the student and the citations behind it, whatever the run ended with."""
    if result.stopped in ("quota", "error"):
        return result.error_detail or "Не удалось получить ответ. Попробуйте позже.", None
    if not result.content.strip():
        return UNKNOWN_BUDGET, None
    done = finalize(result.content.strip(), env.sources, now=now)
    text = done.markdown
    if result.stopped == "budget":
        text += "\n\nОтвет может быть неполным: я остановился по лимиту шагов."
    return text, done


async def load_thread_scope(
    session: AsyncSession, user_id: str, thread: ChatThread
) -> tuple[Space | None, list[str]]:
    """The thread's space (if any) and the titles of all the student's spaces."""
    space = None
    if thread.space_id:
        space = (
            await session.execute(
                select(Space).where(Space.id == thread.space_id, Space.user_id == user_id)
            )
        ).scalar_one_or_none()
    titles = list(
        (
            await session.execute(
                select(Space.title).where(Space.user_id == user_id).order_by(Space.created_at)
            )
        ).scalars()
    )
    return space, titles


async def _load(
    services: Services, user_id: str, thread_id: str
) -> tuple[ChatThread, Space | None, list[str]]:
    async with services.db() as session:
        thread = (
            await session.execute(
                select(ChatThread).where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
            )
        ).scalar_one_or_none()
        if thread is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
        space, titles = await load_thread_scope(session, user_id, thread)
        return thread, space, titles


def meter_segments(
    result: harness.HarnessResult,
    ctx_msg: LlmMessage,
    summary: LlmMessage | None,
    recent: list[LlmMessage],
    question: LlmMessage,
) -> dict[str, int]:
    """Estimated tokens by kind for the context meter (agent-harness.md §6)."""
    by_tool = {"data": 0, "documents": 0}
    for t in result.tool_calls:
        if t.name in DATA_TOOLS:
            by_tool["data"] += t.result_tokens
        elif t.name in DOCUMENT_TOOLS:
            by_tool["documents"] += t.result_tokens
    return {
        "system": result.prefix_tokens,
        "profile": estimate_messages([ctx_msg]),
        "data": by_tool["data"],
        "documents": by_tool["documents"],
        "history": estimate_messages([*([summary] if summary else []), *recent, question]),
    }


async def handle_chat(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    reader: DomainReader | None = None,
) -> dict[str, Any] | None:
    user_id = ctx.user_id
    thread_id = str(payload["thread_id"])
    thread, space, space_titles = await _load(services, user_id, thread_id)
    await ctx.status("Читаю вопрос")

    turns, stored_summary = await load_turns(services, user_id, thread_id)
    question_id = str(payload.get("message_id") or "")
    idx = next((i for i, t in enumerate(turns) if t.id == question_id), len(turns) - 1)
    if idx < 0 or turns[idx].role != "user":
        raise BotaiError(ErrorCode.NOT_FOUND, "Вопрос не найден")
    question, history = turns[idx], turns[:idx]
    older, recent = split_recent(history, RECENT_TOKENS)
    summary_text = parse_summary(stored_summary)[1]
    if older:
        summary_text = await refresh_summary(
            services,
            user_id=user_id,
            thread_id=thread_id,
            older=older,
            stored=stored_summary,
            ctx=ctx,
            job_id=ctx.job_id,
        )

    now = services.clock.now()
    ctx_msg = context_message(now, space, space_titles)
    summary_msg = summary_message(summary_text)
    recent_msgs = history_messages(recent)
    question_msg = LlmMessage(role="user", content=question.text)
    messages = [ctx_msg, *([summary_msg] if summary_msg else []), *recent_msgs, question_msg]

    env = ChatEnv(
        services=services,
        user_id=user_id,
        thread_id=thread_id,
        job_id=ctx.job_id,
        space_id=thread.space_id,
    )
    if reader is not None:
        env.reader = reader
    result = await harness.run(
        "chat",
        skill="chat",
        messages=messages,
        tools=build_chat_tools(env),
        ctx=ctx,
        services=services,
        user_id=user_id,
        job_id=ctx.job_id,
    )

    text, done = compose_answer(result, env, now)
    if result.findings:
        text += INJECTION_NOTE
    text = text[:MAX_MARKDOWN]
    block = TextBlock(
        id=new_id(),
        at=now,
        markdown=text,
        sources=done.sources if done and done.sources else None,
    )
    await ctx.emit(block)
    message_id = await _store_reply(services, ctx, thread, block, question.text)

    return {
        "messageId": message_id,
        "stopped": result.stopped,
        "steps": result.steps,
        "tokens": result.total_tokens,
        "cacheShare": round(result.cache_share, 3),
        "prefixVersion": result.prefix_version,
        "context": meter_segments(result, ctx_msg, summary_msg, recent_msgs, question_msg),
        "sources": [
            {
                "title": r.title,
                "kind": r.kind,
                "fetchedAt": r.fetched_at.isoformat() if r.fetched_at else None,
            }
            for r in (done.cited if done else [])
        ],
    }


async def _store_reply(
    services: Services, ctx: JobContext, thread: ChatThread, block: TextBlock, question: str
) -> str:
    """Persist the reply once per job (a retried job does not answer twice)."""
    async with services.db() as session:
        existing = (
            await session.execute(
                select(ChatMessage.id).where(
                    ChatMessage.thread_id == thread.id,
                    ChatMessage.user_id == ctx.user_id,
                    ChatMessage.job_id == ctx.job_id,
                    ChatMessage.role == "agent",
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        message_id = new_id()
        session.add(
            ChatMessage(
                id=message_id,
                user_id=ctx.user_id,
                thread_id=thread.id,
                role="agent",
                blocks=[block.model_dump(by_alias=True, mode="json")],
                job_id=ctx.job_id,
                created_at=services.clock.now(),
            )
        )
        row = (
            await session.execute(
                select(ChatThread).where(
                    ChatThread.id == thread.id, ChatThread.user_id == ctx.user_id
                )
            )
        ).scalar_one()
        row.updated_at = services.clock.now()
        if row.title == DEFAULT_TITLE:
            row.title = title_from(question)
        await session.commit()
        return message_id


def title_from(question: str) -> str:
    one_line = " ".join(question.split())
    return (one_line[:57] + "…") if len(one_line) > 60 else one_line or DEFAULT_TITLE
