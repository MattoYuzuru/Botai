"""Thread memory (agent-harness.md §3): recent turns whole within 24k tokens, older ones as a summary.

The summary lives in `chat_threads.summary`. The table has no column for "summarised up to", so the
stored text starts with a marker line `[до сообщения <id>]`; `parse_summary` strips it for the prompt.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update

from botai.agent.harness import run
from botai.agent.tokens import estimate_tokens
from botai.core.jobs import JobContext
from botai.core.llm import LlmMessage
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread

log = logging.getLogger("botai.agent.memory")

RECENT_TOKENS = 24_000
SUMMARY_CHARS = 1_800  # the stored summary is cut to this (summarize.md asks for 1200)
_MARKER = re.compile(r"\A\[до сообщения (?P<id>[0-9A-Z]{26})\]\n")


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    role: Literal["user", "assistant"]
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text) + 4


def turn_from_blocks(message_id: str, role: str, blocks: Sequence[dict[str, Any]]) -> Turn | None:
    """Plain text of a stored message; thinking/approval/other blocks are not conversation.
    The files of a student's message are named after the text, so a message with only a file is a
    turn too."""
    text = "\n\n".join(
        str(b.get("markdown", "")) for b in blocks if b.get("type") == "text" and b.get("markdown")
    ).strip()
    files = [str(b.get("name", ""))[:120] for b in blocks if b.get("type") == "attachment"]
    if files:
        text = (
            (text + "\n\n" if text else "")
            + "[Вложения: "
            + ", ".join(f"«{n}»" for n in files)
            + "]"
        )
    if not text:
        return None
    return Turn(message_id, "user" if role == "user" else "assistant", text)


def split_recent(
    turns: Sequence[Turn], budget: int = RECENT_TOKENS
) -> tuple[list[Turn], list[Turn]]:
    """(older, recent): the newest turns that fit `budget`; the last turn is always kept."""
    used = 0
    cut = len(turns)
    for i in range(len(turns) - 1, -1, -1):
        used += turns[i].tokens
        if used > budget and i < len(turns) - 1:
            break
        cut = i
    return list(turns[:cut]), list(turns[cut:])


def parse_summary(stored: str | None) -> tuple[str | None, str]:
    """(id of the last summarised message, summary text)."""
    if not stored:
        return None, ""
    m = _MARKER.match(stored)
    if not m:
        return None, stored
    return m.group("id"), stored[m.end() :]


def pack_summary(upto_id: str, text: str) -> str:
    return f"[до сообщения {upto_id}]\n{text.strip()[:SUMMARY_CHARS]}"


def history_messages(recent: Sequence[Turn]) -> list[LlmMessage]:
    return [LlmMessage(role=t.role, content=t.text) for t in recent]


def summary_message(text: str) -> LlmMessage | None:
    if not text:
        return None
    return LlmMessage(
        role="user",
        content="Сводка прежней переписки (это память о разговоре, а не данные вуза):\n" + text,
    )


async def load_turns(
    services: Services, user_id: str, thread_id: str
) -> tuple[list[Turn], str | None]:
    """All turns of the thread in order plus the stored summary (tenant-scoped)."""
    async with services.db() as session:
        thread = (
            await session.execute(
                select(ChatThread).where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
            )
        ).scalar_one_or_none()
        if thread is None:
            return [], None
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id, ChatMessage.user_id == user_id)
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars()
        turns = [t for r in rows if (t := turn_from_blocks(r.id, r.role, r.blocks))]
        return turns, thread.summary


async def refresh_summary(
    services: Services,
    *,
    user_id: str,
    thread_id: str,
    older: Sequence[Turn],
    stored: str | None,
    ctx: JobContext | None = None,
    job_id: str | None = None,
) -> str:
    """Summary text covering `older`. Calls the cheap `summarize` profile only for new turns."""
    covered_id, text = parse_summary(stored)
    ids = [t.id for t in older]
    fresh = list(older[ids.index(covered_id) + 1 :]) if covered_id in ids else list(older)
    if not fresh:
        return text
    dialogue = "\n".join(
        f"{'Студент' if t.role == 'user' else 'Ассистент'}: {t.text}" for t in fresh
    )
    prompt = (
        f"Прежняя сводка:\n{text}\n\n" if text else ""
    ) + f"Новые реплики:\n{dialogue}\n\nОбновите сводку."
    result = await run(
        "summarize",
        skill="summarize",
        messages=[LlmMessage(role="user", content=prompt)],
        tools=[],
        ctx=ctx,
        services=services,
        user_id=user_id,
        job_id=job_id,
    )
    if result.stopped != "answer" or not result.content.strip():
        # Memory is best effort: keep the old summary rather than failing the answer.
        log.warning("thread summary not updated: stopped=%s", result.stopped)
        return text
    new_text = result.content.strip()[:SUMMARY_CHARS]
    async with services.db() as session:
        await session.execute(
            update(ChatThread)
            .where(ChatThread.id == thread_id, ChatThread.user_id == user_id)
            .values(summary=pack_summary(older[-1].id, new_text))
        )
        await session.commit()
    return new_text
