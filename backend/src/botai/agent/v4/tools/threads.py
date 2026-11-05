"""`threads_search`, `threads_read`: the v4 agent finds what the student discussed in other chats.

The tools read the live `chat_threads`/`chat_messages` rows of the run's user, never a copy: a
thread the student deleted is gone from the search the same instant (the messages cascade), an
edited title or a refreshed summary is seen at once, and nothing is embedded or paid for. The
search is Postgres full text over the text of the messages and over each thread's title and
rolling summary (`russian` configuration, prefix match of every word, any word may match), so
«руководитель курсовой» finds «научный руководитель курсовой работы». Why not RAG: see
`docs/v4/24-chat-memory.md` §3.

What comes back is the conversation as the thread holds it: the student's words (already scrubbed
of secrets when they were posted) and the agent's answers (already masked when stored). It is data
about the past, not instructions; the runtime wraps it like any tool result.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import ColumnElement, Text, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.env import thread_link
from botai.agent.memory import parse_summary, turn_from_blocks
from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.db.models import ChatMessage, ChatThread

DEFAULT_THREADS = 5
MAX_THREADS = 10
MATCHES_PER_THREAD = 3
MAX_CANDIDATES = 200
EXCERPT_CHARS = 320
SUMMARY_CHARS = 1_200
DEFAULT_READ = 30
MAX_READ = 80
READ_TURN_CHARS = 2_000
MAX_TERMS = 12
_WORD = re.compile(r"[^\W_]{2,}")


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=500)
    limit: int = Field(default=DEFAULT_THREADS, ge=1, le=MAX_THREADS)
    include_current: bool = False


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=26)
    limit: int = Field(default=DEFAULT_READ, ge=1, le=MAX_READ)
    before_message_id: str | None = Field(default=None, max_length=26)


def _invalid(exc: ValidationError) -> ToolResult:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    return ToolResult.failure(
        "invalid_arguments", f"Некорректные аргументы: {location}: {first.get('msg', 'ошибка')}"
    )


def search_terms(query: str) -> list[str]:
    """The words of the query (letters and digits only, so they are safe inside `to_tsquery`)."""
    return list(dict.fromkeys(w.casefold() for w in _WORD.findall(query)))[:MAX_TERMS]


def _tsquery(terms: list[str]) -> ColumnElement[Any]:
    """Any of the words, each as a prefix: «курсов» matches «курсовая» and «курсовой»."""
    return func.to_tsquery("russian", " | ".join(f"{term}:*" for term in terms))


def _message_text() -> ColumnElement[Any]:
    """The words a message shows: the markdown of its text blocks and the question of a card."""
    texts = func.jsonb_path_query_array(
        ChatMessage.blocks, cast(literal("$[*].markdown"), JSONPATH)
    )
    questions = func.jsonb_path_query_array(
        ChatMessage.blocks, cast(literal("$[*].question"), JSONPATH)
    )
    return func.concat_ws(" ", cast(texts, Text), cast(questions, Text))


def _excerpt(text: str, terms: list[str]) -> str:
    """A window of the message around its first matching word (or its start)."""
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    lowered = flat.casefold()
    hits = [i for term in terms if (i := lowered.find(term[: max(4, len(term) - 2)])) >= 0]
    start = max(0, min(hits) - EXCERPT_CHARS // 3) if hits else 0
    piece = flat[start : start + EXCERPT_CHARS]
    return ("…" if start else "") + piece + ("…" if start + EXCERPT_CHARS < len(flat) else "")


def _summary(stored: str | None) -> str | None:
    text = parse_summary(stored)[1].strip()
    return text[:SUMMARY_CHARS] or None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ThreadToolkit:
    """The past-conversation tools over one session factory (scoped to the run's user)."""

    search_spec = ToolSpec(
        name="threads_search",
        description=(
            "Найти, что студент уже обсуждал с тобой в других чатах: полнотекстовый поиск по "
            "сообщениям, названиям и сводкам его чатов (любое из слов, по началу слова). Для "
            "каждого чата — название, сводка, дата и до трёх подходящих фрагментов с ролью и "
            "датой. Без query — последние чаты со сводками. Текущий чат исключён "
            "(include_current=true — включить). Полный текст чата — threads_read. Только чтение."
        ),
        parameters=object_schema(
            {
                "query": {"type": "string", "maxLength": 500, "default": ""},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_THREADS,
                    "default": DEFAULT_THREADS,
                },
                "include_current": {"type": "boolean", "default": False},
            }
        ),
        timeout_s=20.0,
    )
    read_spec = ToolSpec(
        name="threads_read",
        description=(
            "Прочитать переписку одного чата студента: сводку и последние реплики по порядку "
            "(student/assistant). before_message_id — листать к более ранним. thread_id — из "
            "threads_search. Только чтение."
        ),
        parameters=object_schema(
            {
                "thread_id": {"type": "string", "minLength": 1, "maxLength": 26},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ,
                    "default": DEFAULT_READ,
                },
                "before_message_id": {"type": ["string", "null"], "maxLength": 26},
            },
            required=["thread_id"],
        ),
        timeout_s=20.0,
        max_result_chars=60_000,
    )

    def __init__(self, *, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm
        self._handlers: list[ToolHandler] = [
            FunctionTool(self.search_spec, self._search),
            FunctionTool(self.read_spec, self._read),
        ]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    # --- threads_search ----------------------------------------------------------------------

    async def _search(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Поиск по чатам без пользователя невозможен.")
        try:
            args = _SearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        current = context.metadata.get("thread_id")
        skip = current if isinstance(current, str) and not args.include_current else None
        terms = search_terms(args.query)
        async with self._sm() as session:
            if not terms:
                threads = await self._recent(session, user_id, skip, args.limit)
                found = [self._thread_item(thread, []) for thread in threads]
                summary = f"Последние чаты студента: {len(found)}."
            else:
                found = await self._matching(session, user_id, skip, terms, args.limit)
                summary = (
                    f"Найдено чатов: {len(found)}."
                    if found
                    else "В других чатах студента ничего не найдено по этим словам."
                )
        return ToolResult.success(summary=summary, data={"terms": terms, "threads": found})

    async def _recent(
        self, session: AsyncSession, user_id: str, skip: str | None, limit: int
    ) -> list[ChatThread]:
        query = select(ChatThread).where(ChatThread.user_id == user_id)
        if skip is not None:
            query = query.where(ChatThread.id != skip)
        rows = await session.scalars(
            query.order_by(ChatThread.last_message_at.desc(), ChatThread.id.desc()).limit(limit)
        )
        return list(rows)

    async def _matching(
        self,
        session: AsyncSession,
        user_id: str,
        skip: str | None,
        terms: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        tsq = _tsquery(terms)
        body = func.to_tsvector("russian", _message_text())
        rank = func.ts_rank(body, tsq)
        messages = (
            select(ChatMessage, rank.label("rank"))
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(
                ChatMessage.user_id == user_id,
                ChatThread.user_id == user_id,
                body.op("@@")(tsq),
            )
            .order_by(rank.desc(), ChatMessage.created_at.desc())
            .limit(MAX_CANDIDATES)
        )
        head = func.to_tsvector(
            "russian", func.concat_ws(" ", ChatThread.title, ChatThread.summary)
        )
        head_rank = func.ts_rank(head, tsq)
        heads = (
            select(ChatThread, head_rank.label("rank"))
            .where(ChatThread.user_id == user_id, head.op("@@")(tsq))
            .order_by(head_rank.desc(), ChatThread.last_message_at.desc())
            .limit(MAX_CANDIDATES)
        )
        if skip is not None:
            messages = messages.where(ChatMessage.thread_id != skip)
            heads = heads.where(ChatThread.id != skip)

        score: dict[str, float] = {}
        matches: dict[str, list[dict[str, Any]]] = {}
        for message, value in await session.execute(messages):
            turn = turn_from_blocks(message.id, message.role, message.blocks)
            if turn is None:
                continue
            score[message.thread_id] = score.get(message.thread_id, 0.0) + float(value)
            bucket = matches.setdefault(message.thread_id, [])
            if len(bucket) < MATCHES_PER_THREAD:
                bucket.append(
                    {
                        "messageId": message.id,
                        "role": "student" if turn.role == "user" else "assistant",
                        "at": _iso(message.created_at),
                        "excerpt": _excerpt(turn.text, terms),
                    }
                )
        threads: dict[str, ChatThread] = {}
        for thread, value in await session.execute(heads):
            threads[thread.id] = thread
            # A match in the title or the summary speaks for the whole conversation.
            score[thread.id] = score.get(thread.id, 0.0) + 2.0 * float(value)
        missing = [tid for tid in score if tid not in threads]
        if missing:
            rows = await session.scalars(
                select(ChatThread).where(ChatThread.user_id == user_id, ChatThread.id.in_(missing))
            )
            threads.update({row.id: row for row in rows})
        ordered = sorted(
            (tid for tid in score if tid in threads),
            key=lambda tid: (score[tid], threads[tid].last_message_at),
            reverse=True,
        )[:limit]
        return [self._thread_item(threads[tid], matches.get(tid, [])) for tid in ordered]

    @staticmethod
    def _thread_item(thread: ChatThread, matches: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "threadId": thread.id,
            "title": thread.title,
            "link": thread_link(thread.id),
            "lastMessageAt": _iso(thread.last_message_at),
            "summary": _summary(thread.summary),
            "matches": matches,
        }

    # --- threads_read ------------------------------------------------------------------------

    async def _read(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Чтение чатов без пользователя невозможно.")
        try:
            args = _ReadArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        async with self._sm() as session:
            thread = await session.scalar(
                select(ChatThread).where(
                    ChatThread.id == args.thread_id, ChatThread.user_id == user_id
                )
            )
            if thread is None:
                return ToolResult.failure(
                    "not_found",
                    "Такого чата нет (или он удалён).",
                    hint="Возьми thread_id из threads_search.",
                )
            query = select(ChatMessage).where(
                ChatMessage.thread_id == thread.id, ChatMessage.user_id == user_id
            )
            if args.before_message_id:
                anchor = await session.scalar(
                    select(ChatMessage.created_at).where(
                        ChatMessage.id == args.before_message_id,
                        ChatMessage.thread_id == thread.id,
                        ChatMessage.user_id == user_id,
                    )
                )
                if anchor is None:
                    return ToolResult.failure("not_found", "Сообщение не из этого чата.")
                query = query.where(
                    or_(
                        ChatMessage.created_at < anchor,
                        (ChatMessage.created_at == anchor)
                        & (ChatMessage.id < args.before_message_id),
                    )
                )
            # Thinking lines and other non-text blocks are not conversation: read a margin.
            rows = list(
                await session.scalars(
                    query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(
                        args.limit * 3
                    )
                )
            )
            item = self._thread_item(thread, [])
        turns: list[dict[str, Any]] = []
        for row in rows:
            turn = turn_from_blocks(row.id, row.role, row.blocks)
            if turn is None:
                continue
            text = turn.text
            turns.append(
                {
                    "messageId": row.id,
                    "role": "student" if turn.role == "user" else "assistant",
                    "at": _iso(row.created_at),
                    "text": text[:READ_TURN_CHARS] + ("…" if len(text) > READ_TURN_CHARS else ""),
                    **({"quote": turn.quote[:300]} if turn.quote else {}),
                }
            )
            if len(turns) >= args.limit:
                break
        turns.reverse()
        earlier = len(rows) == args.limit * 3 or len(turns) >= args.limit
        return ToolResult.success(
            summary=f"Чат «{thread.title}»: реплик {len(turns)}.",
            data={**item, "turns": turns},
            next=(
                f"threads_read(thread_id={thread.id}, before_message_id={turns[0]['messageId']})"
                if earlier and turns
                else None
            ),
        )


def build_thread_tools(toolkit: ThreadToolkit) -> list[ToolHandler]:
    """The past-conversation set; the wiring adds it next to the memory tools."""
    return toolkit.tools()


__all__ = ["ThreadToolkit", "build_thread_tools", "search_terms"]
