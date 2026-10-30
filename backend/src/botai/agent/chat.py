"""The agent turn: the CHAT job handler and everything it assembles around the harness.

One job answers one student message: load the thread (tenant-scoped), fold old turns into the
summary, build the variable part (date, universities and sources with statuses, running tasks,
memory, summary, recent turns, question), run the harness with the agent's tools, turn `[S1]`
citations into real sources, store and emit the reply with its task cards and quick replies.
Failures become a human answer, never a stack trace or a bare error code.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from botai.agent import facts, harness
from botai.agent.attachment_context import attachments_message
from botai.agent.env import ChatEnv, SeenLinks
from botai.agent.links import strip_bare_ids
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
from botai.agent.tools import DATA_TOOLS, DOCUMENT_TOOLS, build_chat_tools
from botai.agent.tools_web import question_urls
from botai.agent.turn_context import turn_context
from botai.contracts.agent_ui import AgentUiBlock, TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobContext
from botai.core.llm import LlmMessage
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread
from botai.jobs.dialogue import message_time

log = logging.getLogger("botai.agent.chat")

DEFAULT_TITLE = "Новый чат"
MAX_MARKDOWN = 7_500  # TextBlock.markdown is limited to 8000 characters
TOO_LARGE = (
    "Сообщение вместе с вложениями слишком большое для одного ответа, и я его не разобрал. "
    "Отправьте файлы по несколько штук или короче: я прочитаю их по частям."
)
UNKNOWN_BUDGET = (
    "Не знаю: мне не хватило шагов, чтобы найти ответ. Уточните вопрос, например дату, курс "
    "или название документа, и я поищу снова."
)
INJECTION_NOTE = (
    "\n\nВ найденном тексте был фрагмент, похожий на инструкцию для ассистента. "
    "Я его не выполнял и убрал из рассмотрения."
)


SHORT_WITH_BUTTONS = (
    "Служебная проверка: ваш ответ студенту — один вопрос под кнопками, без объяснения. "
    "Студент увидит только следующий ваш ответ, поэтому напишите его целиком заново, обращаясь к "
    "студенту: что вы нашли или сделали и что из этого следует, и только последней фразой — "
    "вопрос, к которому относятся кнопки. Не упоминайте эту проверку и сами кнопки."
)


def too_short(text: str) -> bool:
    """A lone question (one sentence, a few words) is not an answer to put buttons under."""
    plain = text.strip()
    sentences = [part for part in re.split(r"(?<=[.!?…])\s+", plain) if part.strip()]
    return len(sentences) < 2 or len(plain) < 60


def compose_answer(
    result: harness.HarnessResult, env: ChatEnv, now: datetime
) -> tuple[str, Finalized | None]:
    """The text for the student and the citations behind it, whatever the run ended with."""
    if result.stopped in ("quota", "error"):
        return result.error_detail or "Не удалось получить ответ. Попробуйте позже.", None
    if result.overflow:
        return TOO_LARGE, None  # not «не хватило шагов»: no step was made
    if not result.content.strip():
        return UNKNOWN_BUDGET, None
    done = finalize(result.content.strip(), env.sources, now=now)
    text = strip_bare_ids(done.markdown)
    if result.stopped == "budget":
        text += "\n\nОтвет может быть неполным: я остановился по лимиту шагов."
    return text, done


async def _load(
    services: Services, user_id: str, thread_id: str, now: datetime
) -> tuple[ChatThread, LlmMessage]:
    async with services.db() as session:
        thread = (
            await session.execute(
                select(ChatThread).where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
            )
        ).scalar_one_or_none()
        if thread is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Чат не найден")
        return thread, await turn_context(session, user_id, thread, now)


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
    now = services.clock.now()
    thread, ctx_msg = await _load(services, user_id, thread_id, now)
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

    async with services.db() as session:
        files = await attachments_message(session, user_id, question.id)
    if files.message is not None:
        ctx_msg.content += "\n\n" + files.message.content
    if files.findings:
        await harness.audit_injection(
            services, user_id, ctx.job_id, "attachment", files.findings, 0
        )
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
        public_question=question.text,
        ctx=ctx,
        question_links=question_urls(question.text),
        seen=SeenLinks.load(thread.seen_links),
        outside_read=files.has_text,  # a file's words are not the student's
    )
    if reader is not None:
        env.reader = reader
    result = await harness.run(
        "agent",
        skill="agent",
        messages=messages,
        tools=build_chat_tools(env),
        ctx=ctx,
        services=services,
        user_id=user_id,
        job_id=ctx.job_id,
        review=lambda text: SHORT_WITH_BUTTONS if env.quick_replies and too_short(text) else None,
    )

    text, done = compose_answer(result, env, now)
    fact_id = await _save_fact(services, env, result, done, thread, question.text, now)
    if result.findings or files.findings:
        text += INJECTION_NOTE
    if env.remembered:  # the student sees what the agent keeps about them, and can delete it
        text += "\n\n" + " ".join(f"Запомнил: «{note}»." for note in env.remembered)
    text = text[:MAX_MARKDOWN]
    block = TextBlock(
        id=new_id(),
        at=now,
        markdown=text,
        sources=done.sources if done and done.sources else None,
        quick_replies=env.quick_replies or None,
    )
    reply: list[AgentUiBlock] = [block, *env.blocks]
    for item in reply:
        await ctx.emit(item)
    message_id = await _store_reply(services, ctx, thread, reply, question.text, env.seen)

    return {
        "messageId": message_id,
        "factId": fact_id,
        "stopped": result.stopped,
        "steps": result.steps,
        "tokens": result.total_tokens,
        "cacheShare": round(result.cache_share, 3),
        "prefixVersion": result.prefix_version,
        "tools": [t.name for t in result.tool_calls],
        "blockedCalls": result.blocked_calls,
        "injections": len(result.findings),
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


async def _save_fact(
    services: Services,
    env: ChatEnv,
    result: harness.HarnessResult,
    done: Finalized | None,
    thread: ChatThread,
    question: str,
    now: datetime,
) -> str | None:
    """Keep a document-based answer as a fact. Never for a page that tried to instruct us."""
    if (
        result.stopped != "answer"
        or done is None
        or result.findings
        or "search_documents" not in env.used
    ):
        return None
    try:
        return await facts.remember(
            services,
            user_id=env.user_id,
            space_id=thread.space_id,
            question=question,
            answer=done.markdown,
            cited=done.cited,
            now=now,
        )
    except Exception:
        log.exception("failed to save the fact")  # the answer is still delivered
        return None


async def _store_reply(
    services: Services,
    ctx: JobContext,
    thread: ChatThread,
    blocks: list[AgentUiBlock],
    question: str,
    seen: SeenLinks,
) -> str:
    """Persist the reply once per job (a retried job does not answer twice). The plan the turn
    published lives in its own message with the same job id; the reply is the one starting with text."""
    async with services.db() as session:
        existing = (
            await session.execute(
                select(ChatMessage.id).where(
                    ChatMessage.thread_id == thread.id,
                    ChatMessage.user_id == ctx.user_id,
                    ChatMessage.job_id == ctx.job_id,
                    ChatMessage.role == "agent",
                    ChatMessage.blocks[0]["type"].astext == "text",
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
                blocks=[
                    b.model_dump(by_alias=True, mode="json", exclude_none=True) for b in blocks
                ],
                job_id=ctx.job_id,
                created_at=await message_time(
                    session, ctx.user_id, thread.id, services.clock.now()
                ),
            )
        )
        row = (
            await session.execute(
                select(ChatThread).where(
                    ChatThread.id == thread.id, ChatThread.user_id == ctx.user_id
                )
            )
        ).scalar_one()
        if row.title == DEFAULT_TITLE:
            row.title = title_from(question)
        if seen.changed:
            row.seen_links = seen.dump()
        await session.commit()
        return message_id


def title_from(question: str) -> str:
    one_line = " ".join(question.split())
    return (one_line[:57] + "…") if len(one_line) > 60 else one_line or DEFAULT_TITLE
