"""Operator command: retry a dead chat turn by sending the thread a normal student message.

    python -m botai.ops.retry_thread --thread ULID [--text TEXT] [--resume] [--dry-run]

A production turn can die on a provider failure (`llm_unavailable`) or on a soft limit; the chat
stays in the thread, and the owner wants the agent to continue from it. The command does exactly
what the student does in the composer: it posts `MessageIn` through
`botai.agent.conversations.post_message` for the thread owner, so the usual routing applies (a
message to a task that waits for a plain answer continues that task; otherwise it starts an agent
turn, and an in-flight turn is refused with `agent_busy`). The default text asks the agent to
continue without repeating the whole request.

A turn that stopped without an answer leaves a checkpoint (`agent_run_checkpoints`, D-040): when it
is resumable and fresh, the message continues from it and the agent does not re-read its traces.
That is the default; `--resume` insists on it and fails if there is nothing to resume.

`--dry-run` reads the thread, its owner, the last message, the last job and the resumable
checkpoint and prints them; it writes nothing and posts nothing. The command prints only
identifiers, states and counters, never message text, prompts, keys or other secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.conversations import post_message
from botai.agent.schemas import MessageIn
from botai.agent.v4.checkpoints import StoredCheckpoint, checkpoint_store
from botai.core.errors import BotaiError, ErrorCode
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Job, User
from botai.wiring import build_services

DEFAULT_TEXT = "Продолжай, пожалуйста."
BUSY_TEXT = "Ассистент ещё отвечает на прошлое сообщение. Дождитесь ответа и повторите."
# ULID: 26 chars of Crockford base32 (no I, L, O, U).
_THREAD_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class RetryThreadError(Exception):
    """A mistake of the operator, printed without a traceback."""


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    thread_id: str
    owner_id: str
    owner_name: str


@dataclass(frozen=True, slots=True)
class DryRunInfo:
    thread: ThreadInfo
    last_role: str | None
    last_at: datetime | None
    job_id: str | None
    job_kind: str | None
    job_state: str | None
    checkpoint: CheckpointInfo | None = None


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    run_id: str
    status: str
    segment: int
    message_count: int
    size_bytes: int
    updated_at: datetime
    expires_at: datetime


def _info(checkpoint: StoredCheckpoint) -> CheckpointInfo:
    return CheckpointInfo(
        run_id=checkpoint.run_id,
        status=checkpoint.status,
        segment=checkpoint.segment,
        message_count=checkpoint.message_count,
        size_bytes=checkpoint.bytes,
        updated_at=checkpoint.updated_at,
        expires_at=checkpoint.expires_at,
    )


async def resumable_checkpoint(services: Services, thread: ThreadInfo) -> StoredCheckpoint | None:
    """The checkpoint the thread's next message would continue from (resumable and fresh)."""
    window = timedelta(hours=services.settings.agent_resume_window_hours)
    return await checkpoint_store(services).resumable_in_thread(
        thread.owner_id, thread.thread_id, window=window
    )


async def find_thread(session: AsyncSession, thread_id: str) -> ThreadInfo:
    """The thread and its owner; any account may be reached by the operator by thread id alone."""
    thread = await session.scalar(select(ChatThread).where(ChatThread.id == thread_id))
    if thread is None:
        raise RetryThreadError("Чат не найден")
    owner = await session.scalar(select(User).where(User.id == thread.user_id))
    if owner is None:  # a foreign-key violation would be a broken database, not a bad command
        raise RetryThreadError("Владелец чата не найден")
    return ThreadInfo(thread_id=thread.id, owner_id=owner.id, owner_name=owner.display_name)


async def inspect_thread(
    session: AsyncSession, thread_id: str, services: Services | None = None
) -> DryRunInfo:
    """Everything `--dry-run` prints: the thread, owner, last message, last job and checkpoint."""
    thread = await find_thread(session, thread_id)
    checkpoint = await resumable_checkpoint(services, thread) if services is not None else None
    message = await session.scalar(
        select(ChatMessage)
        .where(ChatMessage.thread_id == thread_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    job = await session.scalar(
        select(Job)
        .where(Job.thread_id == thread_id)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )
    return DryRunInfo(
        thread=thread,
        last_role=message.role if message is not None else None,
        last_at=message.created_at if message is not None else None,
        job_id=job.id if job is not None else None,
        job_kind=job.kind if job is not None else None,
        job_state=job.state if job is not None else None,
        checkpoint=_info(checkpoint) if checkpoint is not None else None,
    )


def _thread_id(value: str) -> str:
    normalized = value.strip().upper()
    if not _THREAD_ID.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            f"идентификатор чата — ULID из 26 символов, получено {value!r}"
        )
    return normalized


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="botai.ops.retry_thread",
        description="BotAI: повторить ход агента в чате обычным сообщением студента",
    )
    p.add_argument("--thread", required=True, type=_thread_id, help="ULID чата")
    p.add_argument(
        "--text",
        default=DEFAULT_TEXT,
        help=f"текст сообщения (по умолчанию «{DEFAULT_TEXT}»)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="продолжить от чекпоинта прерванного хода; ошибка, если свежего чекпоинта нет "
        "(без флага чекпоинт используется сам, когда он есть)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="только прочитать чат и задания; ничего не писать"
    )
    return p


def _message(dry: DryRunInfo, out: Callable[[str], None]) -> None:
    thread = dry.thread
    out(f"Чат: {thread.thread_id}")
    out(f"Владелец: {thread.owner_name} ({thread.owner_id})")
    if dry.last_role is None or dry.last_at is None:
        out("Последнее сообщение: нет")
    else:
        out(f"Последнее сообщение: {dry.last_role}, {dry.last_at.isoformat(timespec='seconds')}")
    if dry.job_id is None:
        out("Последнее задание: нет")
    else:
        out(f"Последнее задание: {dry.job_id} ({dry.job_kind}), состояние {dry.job_state}")
    checkpoint = dry.checkpoint
    if checkpoint is None:
        out("Чекпоинт для продолжения: нет")
    else:
        out(
            f"Чекпоинт для продолжения: {checkpoint.run_id}, статус {checkpoint.status}, "
            f"часть {checkpoint.segment + 1}, сообщений {checkpoint.message_count}, "
            f"{checkpoint.size_bytes} байт, "
            f"обновлён {checkpoint.updated_at.isoformat(timespec='seconds')}, "
            f"истекает {checkpoint.expires_at.isoformat(timespec='seconds')}"
        )


def _report_botai_error(exc: BotaiError, err: Callable[[str], None]) -> int:
    """Expected refusals in operator language; `agent_busy` gets its own exit code."""
    if exc.code == ErrorCode.AGENT_BUSY:
        err(f"Ошибка: {BUSY_TEXT}")
        return 2
    err(f"Ошибка: {exc.detail}")
    return 1


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


async def run(
    argv: Sequence[str],
    *,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = _stderr,
) -> int:
    args = _parser().parse_args(argv)
    services: Services = build_services()
    try:
        async with services.db() as session:
            if args.dry_run:
                _message(await inspect_thread(session, args.thread, services), out)
                out("--dry-run: ничего не отправлено")
                return 0
            info = await find_thread(session, args.thread)
            try:
                body = MessageIn(text=args.text)
            except ValidationError:
                raise RetryThreadError(
                    "текст сообщения не прошёл проверку: нужно от 1 до 8000 символов"
                ) from None
            checkpoint = await resumable_checkpoint(services, info)
            if args.resume and checkpoint is None:
                raise RetryThreadError(
                    "нечего продолжать: у последнего хода нет свежего чекпоинта "
                    "(он устарел, ход закончился ответом или ещё идёт)"
                )
            if checkpoint is not None:
                body = body.model_copy(update={"resume_run_id": checkpoint.run_id})
            accepted = await post_message(session, services, info.owner_id, info.thread_id, body)
    except RetryThreadError as exc:
        err(f"Ошибка: {exc}")
        return 1
    except BotaiError as exc:
        return _report_botai_error(exc, err)
    out(f"Чат: {info.thread_id}")
    out(f"Владелец: {info.owner_name} ({info.owner_id})")
    out(f"Сообщение: {accepted.message.id}")
    out(f"Задание: {accepted.job_id}")
    if checkpoint is not None:
        out(f"Продолжение от чекпоинта: {checkpoint.run_id}")
    out("Ход поставлен в очередь: ответ появится в чате.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
