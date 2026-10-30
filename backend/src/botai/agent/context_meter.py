"""The context scale: what the agent's context for a thread consists of (agent-harness.md §6).

Estimates, not billing: the same `chars / 3` figure the budgets use. `system` and `profile` and
`history` are computed from the thread as it is now; `data` and `documents` (tool results) exist
only during a turn, so they come from what the last finished CHAT job recorded in `jobs.result`,
together with the list of sources of that answer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.memory import (
    RECENT_TOKENS,
    history_messages,
    load_turns,
    parse_summary,
    split_recent,
    summary_message,
)
from botai.agent.prompts import build_system_text
from botai.agent.schemas import ContextOut, ContextSegment, ContextSource
from botai.agent.tokens import estimate_messages, estimate_tokens, estimate_tools
from botai.agent.tools import TOOL_SPECS
from botai.agent.turn_context import turn_context
from botai.core.jobs import JobKind, JobState
from botai.core.services import Services
from botai.db.models import ChatThread, Job

CONTEXT_LIMIT = 1_000_000  # DeepSeek context window
LABELS = {
    "system": "Правила и навыки",
    "profile": "Профиль",
    "data": "Данные вуза",
    "documents": "Документы",
    "history": "Переписка",
    "free": "Свободно",
}


def system_tokens() -> int:
    """Stable prefix: system.md + agent skills + tool schemas."""
    specs = sorted(TOOL_SPECS.values(), key=lambda s: s.name)
    return estimate_tokens(build_system_text("agent")) + estimate_tools(specs)


async def last_chat_result(session: AsyncSession, user_id: str, thread_id: str) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Job.result)
            .where(
                Job.user_id == user_id,
                Job.thread_id == thread_id,
                Job.kind == JobKind.CHAT.value,
                Job.state == JobState.DONE.value,
                Job.result.is_not(None),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or {}


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


async def build_context(
    session: AsyncSession, services: Services, user_id: str, thread: ChatThread
) -> ContextOut:
    last = await last_chat_result(session, user_id, thread.id)
    recorded: dict[str, int] = last.get("context") or {}

    profile = estimate_messages(
        [await turn_context(session, user_id, thread, services.clock.now())]
    )

    turns, stored = await load_turns(services, user_id, thread.id)
    _, recent = split_recent(turns, RECENT_TOKENS)
    summary = summary_message(parse_summary(stored)[1])
    history = estimate_messages([*([summary] if summary else []), *history_messages(recent)])

    tokens = {
        "system": system_tokens(),
        "profile": profile,
        "data": int(recorded.get("data", 0)),
        "documents": int(recorded.get("documents", 0)),
        "history": history,
    }
    used = sum(tokens.values())
    tokens["free"] = max(CONTEXT_LIMIT - used, 0)
    return ContextOut(
        limit=CONTEXT_LIMIT,
        used=used,
        segments=[ContextSegment(kind=k, label=LABELS[k], tokens=t) for k, t in tokens.items()],  # type: ignore[arg-type]
        sources=[
            ContextSource(
                title=str(s.get("title", "")),
                kind=str(s.get("kind", "")),
                fetched_at=_parse_time(s.get("fetchedAt")),
            )
            for s in last.get("sources") or []
        ],
    )
