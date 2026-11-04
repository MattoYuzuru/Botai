"""Persisted checkpoints of v4 chat segments (D-040): `agent_run_checkpoints`.

The runtime hands a `CheckpointSnapshot` (masked, consistent conversation without the system
message) to a sink after complete steps and at the end of every run. This module stores it as plain
JSONB, one row per run, keeps the chain's counters next to it and finds the checkpoint a later
segment or a later student message continues from. A row expires after `agent_checkpoint_ttl_hours`
(72 by default), goes away with its thread and never leaves the owner (every query is tenant-scoped).

Size: a snapshot above `agent_checkpoint_max_bytes` is compacted deterministically — old tool
results are shortened (the last messages stay whole, and a tool-call/answer pair is never split) —
and if it is still too big it is not saved at all; the caller then stops the chain and says so.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.runtime import CheckpointSnapshot
from botai.core.clock import Clock
from botai.core.jobs import JobKind
from botai.core.llm import LlmMessage
from botai.core.services import Services
from botai.db.models import AgentRunCheckpoint, Job

log = logging.getLogger("botai.agent.v4.checkpoints")

FORMAT_VERSION = 1
KEEP_RECENT_MESSAGES = 20
COMPACT_STEPS_CHARS = (1_500, 300)  # first pass, then a harder second pass over the old results
SOURCE_OPEN = "<source_data"
SOURCE_CLOSE = "</source_data>"
COMPACT_MARKER = "[сокращено: показано {kept} из {total} симв.; полный результат был в трассе]"


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    """A row of `agent_run_checkpoints` with its conversation parsed back into messages."""

    run_id: str
    user_id: str
    thread_id: str
    job_id: str | None
    chain_id: str
    segment: int
    parent_run_id: str | None
    status: str
    error_code: str | None
    resumable: bool
    messages: tuple[LlmMessage, ...]
    message_count: int
    bytes: int
    cost_usd_chain: float
    tokens_chain: int
    continuations: int
    chain_started_at: datetime
    updated_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Compacted:
    messages: tuple[LlmMessage, ...]
    size: int
    compacted: bool
    fits: bool


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    saved: bool
    bytes: int = 0
    message_count: int = 0
    compacted: bool = False


_UNSTORABLE = re.compile(r"\x00|[\ud800-\udfff]")


def _clean_text(value: str) -> str:
    """JSONB stores neither NUL nor a lone surrogate: drop the first, replace the second."""
    return _UNSTORABLE.sub(lambda hit: "" if hit.group() == "\x00" else "\ufffd", value)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {_clean_text(str(key)): _clean(item) for key, item in value.items()}
    return value


def encode_messages(messages: Sequence[LlmMessage]) -> list[dict[str, Any]]:
    """The messages as JSON the database accepts (see `_clean_text`)."""
    return [_clean(message.model_dump(mode="json", exclude_none=True)) for message in messages]


def decode_messages(rows: Sequence[dict[str, Any]]) -> tuple[LlmMessage, ...]:
    return tuple(LlmMessage.model_validate(row) for row in rows)


def encoded_size(rows: Sequence[dict[str, Any]]) -> int:
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())


def protected_from(messages: Sequence[LlmMessage], keep: int) -> int:
    """Index of the first message of the intact tail: the last `keep`, never splitting a call group.

    An assistant message with `tool_calls` and the tool messages that answer it live or go
    together; if the boundary falls inside such a group it moves back to the assistant message.
    """
    start = max(0, len(messages) - keep)
    while start > 0 and messages[start].role == "tool":
        start -= 1
    return start


def _shorten(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = COMPACT_MARKER.format(kept=limit, total=len(content))
    if content.startswith(SOURCE_OPEN):
        opening, _, rest = content.partition("\n")
        body = rest.removesuffix(SOURCE_CLOSE).rstrip()
        return f"{opening}\n{body[:limit].rstrip()}\n{marker}\n{SOURCE_CLOSE}"
    return f"{content[:limit].rstrip()}\n{marker}"


def compact_messages(
    messages: Sequence[LlmMessage], max_bytes: int, *, keep: int = KEEP_RECENT_MESSAGES
) -> Compacted:
    """Shrink old tool results until the encoded conversation fits; deterministic and pair-safe.

    Only the content of `tool` messages before the intact tail is shortened (the assistant
    messages, their `tool_calls` and `reasoning_content` are what the provider needs back as they
    were), so every assistant `tool_calls` still has exactly its tool messages afterwards.
    """
    size = encoded_size(encode_messages(messages))
    if size <= max_bytes:
        return Compacted(tuple(messages), size, compacted=False, fits=True)
    boundary = protected_from(messages, keep)
    current = list(messages)
    for limit in COMPACT_STEPS_CHARS:
        for index in range(boundary):
            message = current[index]
            if message.role == "tool":
                shorter = _shorten(message.content, limit)
                if shorter != message.content:
                    current[index] = message.model_copy(update={"content": shorter})
        size = encoded_size(encode_messages(current))
        if size <= max_bytes:
            return Compacted(tuple(current), size, compacted=True, fits=True)
    return Compacted(tuple(current), size, compacted=True, fits=False)


def _stored(row: AgentRunCheckpoint) -> StoredCheckpoint:
    return StoredCheckpoint(
        run_id=row.run_id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        job_id=row.job_id,
        chain_id=row.chain_id,
        segment=row.segment,
        parent_run_id=row.parent_run_id,
        status=row.status,
        error_code=row.error_code,
        resumable=row.resumable,
        messages=decode_messages(row.messages),
        message_count=row.message_count,
        bytes=row.bytes,
        cost_usd_chain=float(row.cost_usd_chain),
        tokens_chain=int(row.tokens_chain),
        continuations=row.continuations,
        chain_started_at=row.chain_started_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


@dataclass(frozen=True, slots=True)
class ChainPosition:
    """Where a segment sits in its chain; every field is fixed before the segment starts."""

    user_id: str
    thread_id: str
    job_id: str
    chain_id: str
    segment: int
    parent_run_id: str | None
    continuations: int
    chain_started_at: datetime
    cost_before: float = 0.0
    tokens_before: int = 0


class CheckpointStore:
    """Tenant-scoped reads and writes of `agent_run_checkpoints`."""

    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        max_bytes: int,
        ttl: timedelta,
    ) -> None:
        self._db = db
        self._clock = clock
        self._max_bytes = max_bytes
        self._ttl = ttl

    async def save(self, position: ChainPosition, snapshot: CheckpointSnapshot) -> SaveOutcome:
        """Upsert the run's row; a snapshot that cannot fit is not saved (`saved=False`)."""
        compacted = compact_messages(snapshot.messages, self._max_bytes)
        if not compacted.fits:
            log.warning(
                "checkpoint of run %s is %d bytes after compaction (cap %d): not saved",
                snapshot.run_id,
                compacted.size,
                self._max_bytes,
            )
            await self._disown_chain(position)
            return SaveOutcome(saved=False, bytes=compacted.size, compacted=True)
        now = self._clock.now()
        values: dict[str, Any] = {
            "run_id": snapshot.run_id,
            "user_id": position.user_id,
            "thread_id": position.thread_id,
            "job_id": position.job_id,
            "chain_id": position.chain_id,
            "segment": position.segment,
            "parent_run_id": position.parent_run_id,
            "status": snapshot.status,
            "error_code": snapshot.error_code,
            "resumable": snapshot.resumable,
            "messages": encode_messages(compacted.messages),
            "message_count": len(compacted.messages),
            "bytes": compacted.size,
            "format_version": FORMAT_VERSION,
            "cost_usd_chain": position.cost_before + snapshot.cost_usd,
            "tokens_chain": position.tokens_before + snapshot.total_tokens,
            "continuations": position.continuations,
            "chain_started_at": position.chain_started_at,
            "updated_at": now,
            "expires_at": now + self._ttl,
        }
        update_set = {
            key: value
            for key, value in values.items()
            if key not in ("run_id", "user_id", "thread_id", "chain_id", "segment")
        }
        async with self._db() as session, session.begin():
            await session.execute(
                pg_insert(AgentRunCheckpoint)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[AgentRunCheckpoint.run_id],
                    set_=update_set,
                    where=AgentRunCheckpoint.user_id == position.user_id,
                )
            )
        return SaveOutcome(
            saved=True,
            bytes=compacted.size,
            message_count=len(compacted.messages),
            compacted=compacted.compacted,
        )

    async def _disown_chain(self, position: ChainPosition) -> None:
        """No snapshot fits: older ones of this chain would resume at a stale point, so they stop
        being resumable (the student's next message then starts from the thread history)."""
        await self.retire_job(position.user_id, position.job_id)

    async def latest_for_job(self, user_id: str, job_id: str) -> StoredCheckpoint | None:
        """The newest checkpoint of the chain a job owns (its last segment, partial or final)."""
        async with self._db() as session:
            row = await session.scalar(
                select(AgentRunCheckpoint)
                .where(
                    AgentRunCheckpoint.user_id == user_id,
                    AgentRunCheckpoint.job_id == job_id,
                    AgentRunCheckpoint.expires_at > self._clock.now(),
                )
                .order_by(AgentRunCheckpoint.segment.desc(), AgentRunCheckpoint.updated_at.desc())
                .limit(1)
            )
        return _stored(row) if row is not None else None

    async def segments_of_job(self, user_id: str, job_id: str) -> list[tuple[int, str, str | None]]:
        """(segment, status, error_code) of the chain, oldest first: the streak of transient errors."""
        async with self._db() as session:
            rows = await session.execute(
                select(
                    AgentRunCheckpoint.segment,
                    AgentRunCheckpoint.status,
                    AgentRunCheckpoint.error_code,
                )
                .where(
                    AgentRunCheckpoint.user_id == user_id,
                    AgentRunCheckpoint.job_id == job_id,
                )
                .order_by(AgentRunCheckpoint.segment)
            )
            return [(row[0], row[1], row[2]) for row in rows.all()]

    async def get(self, user_id: str, run_id: str) -> StoredCheckpoint | None:
        async with self._db() as session:
            row = await session.scalar(
                select(AgentRunCheckpoint).where(
                    AgentRunCheckpoint.user_id == user_id, AgentRunCheckpoint.run_id == run_id
                )
            )
        return _stored(row) if row is not None else None

    async def _last_chat_job(
        self,
        session: AsyncSession,
        user_id: str,
        thread_id: str,
        exclude_job_id: str | None,
    ) -> str | None:
        return await session.scalar(
            select(Job.id)
            .where(
                Job.user_id == user_id,
                Job.thread_id == thread_id,
                Job.kind == JobKind.CHAT.value,
                *([Job.id != exclude_job_id] if exclude_job_id else []),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )

    async def resumable_in_thread(
        self,
        user_id: str,
        thread_id: str,
        *,
        window: timedelta,
        exclude_job_id: str | None = None,
    ) -> StoredCheckpoint | None:
        """The checkpoint the thread's last agent turn stopped at, if it is resumable and fresh.

        "Last turn" is the newest CHAT job of the thread other than `exclude_job_id`; an older
        stopped turn that a later turn already followed is not resumed (that later turn either
        resumed it or moved on).
        """
        now = self._clock.now()
        async with self._db() as session:
            last_job = await self._last_chat_job(session, user_id, thread_id, exclude_job_id)
            if last_job is None:
                return None
            row = await session.scalar(
                select(AgentRunCheckpoint)
                .where(
                    AgentRunCheckpoint.user_id == user_id,
                    AgentRunCheckpoint.thread_id == thread_id,
                    AgentRunCheckpoint.job_id == last_job,
                    AgentRunCheckpoint.expires_at > now,
                    AgentRunCheckpoint.updated_at >= now - window,
                )
                .order_by(AgentRunCheckpoint.segment.desc(), AgentRunCheckpoint.updated_at.desc())
                .limit(1)
            )
        if row is None or not row.resumable:
            return None
        return _stored(row)

    async def resumable_by_run(
        self,
        user_id: str,
        thread_id: str,
        run_id: str,
        *,
        window: timedelta,
        exclude_job_id: str | None = None,
    ) -> StoredCheckpoint | None:
        """A checkpoint the student named (the «Продолжить» button): theirs, this thread, fresh.

        It must also be what `resumable_in_thread` would pick: the last segment of the thread's
        newest CHAT job. A button of an old turn that was resumed and answered since stays stale.
        """
        checkpoint = await self.get(user_id, run_id)
        now = self._clock.now()
        if (
            checkpoint is None
            or checkpoint.thread_id != thread_id
            or not checkpoint.resumable
            or checkpoint.expires_at <= now
            or checkpoint.updated_at < now - window
        ):
            return None
        current = await self.resumable_in_thread(
            user_id, thread_id, window=window, exclude_job_id=exclude_job_id
        )
        if current is None or current.run_id != checkpoint.run_id:
            return None
        return checkpoint

    async def retire_run(self, user_id: str, run_id: str) -> None:
        """A later turn took this checkpoint over: it must not be resumed a second time."""
        async with self._db() as session, session.begin():
            await session.execute(
                update(AgentRunCheckpoint)
                .where(AgentRunCheckpoint.user_id == user_id, AgentRunCheckpoint.run_id == run_id)
                .values(resumable=False)
            )

    async def retire_job(self, user_id: str, job_id: str) -> None:
        """The job's turn ended with an answer: none of its checkpoints is resumable any more."""
        async with self._db() as session, session.begin():
            await session.execute(
                update(AgentRunCheckpoint)
                .where(AgentRunCheckpoint.user_id == user_id, AgentRunCheckpoint.job_id == job_id)
                .values(resumable=False)
            )


def checkpoint_store(services: Services) -> CheckpointStore:
    """The store configured by the owner's settings (cap, TTL) over the service's database."""
    settings = services.settings
    return CheckpointStore(
        services.db,
        services.clock,
        max_bytes=settings.agent_checkpoint_max_bytes,
        ttl=timedelta(hours=settings.agent_checkpoint_ttl_hours),
    )


async def purge_expired_checkpoints(db: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Delete the checkpoints past their TTL (the maintenance pass of the worker)."""
    async with db() as session:
        result = await session.execute(
            delete(AgentRunCheckpoint).where(AgentRunCheckpoint.expires_at <= now)
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


class SegmentCheckpointer:
    """The `CheckpointSink` of one segment: stores snapshots at the segment's chain position."""

    def __init__(self, store: CheckpointStore, position: ChainPosition) -> None:
        self._store = store
        self.position = position
        self.last: SaveOutcome | None = None
        self.final: SaveOutcome | None = None
        self._parent_retired = False

    async def save(self, snapshot: CheckpointSnapshot) -> None:
        outcome = await self._store.save(self.position, snapshot)
        self.last = outcome
        if outcome.saved and not self._parent_retired and self.position.segment == 0:
            # Segment 0 with a parent is a turn that took a stopped turn's checkpoint over: from
            # here on this job's own checkpoint is the state, the parent must not be resumed again.
            self._parent_retired = True
            if self.position.parent_run_id is not None:
                await self._store.retire_run(self.position.user_id, self.position.parent_run_id)
        if snapshot.final:
            self.final = outcome

    @property
    def final_saved(self) -> bool:
        return self.final is not None and self.final.saved


__all__ = [
    "COMPACT_STEPS_CHARS",
    "FORMAT_VERSION",
    "KEEP_RECENT_MESSAGES",
    "ChainPosition",
    "CheckpointStore",
    "Compacted",
    "SaveOutcome",
    "SegmentCheckpointer",
    "StoredCheckpoint",
    "checkpoint_store",
    "compact_messages",
    "decode_messages",
    "encode_messages",
    "encoded_size",
    "protected_from",
    "purge_expired_checkpoints",
]
