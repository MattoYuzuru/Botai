"""Operator command: time the chat agent on a fixed set of questions for one account.

    python -m botai.ops.chat_probe --user ULID --question "..." [--question "..."] [--timeout 900]
    python -m botai.ops.chat_probe --user ULID --questions-file questions.txt --json

Each question goes to a new chat of the account («Замер скорости: …») exactly as the student would
send it, one at a time. The command waits for the turn to finish and prints how long it took, how
many model steps it needed, what it cost and which tools were called. It is the before/after
measure of the fast-answer path (D-044): the same questions, the same account, two builds.

The chats stay in the account so the owner can read the answers. The command prints the question,
counters and the first characters of the answer; it never prints prompts, keys or session data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from botai.agent.conversations import create_thread, post_message
from botai.agent.schemas import MessageIn, ThreadCreate
from botai.core.services import Services
from botai.db.models import AgentRun, AgentRunEvent, ChatMessage, Job, LlmUsageRow, User
from botai.jobs.answers import cancel_job
from botai.wiring import build_services

TITLE_PREFIX = "Замер скорости: "
POLL_SECONDS = 0.5
DONE_STATES = frozenset({"done", "failed", "cancelled"})
ANSWER_PREVIEW = 400


@dataclass(slots=True)
class ProbeResult:
    question: str
    thread_id: str
    job_id: str
    state: str = "timeout"
    seconds: float = 0.0
    queue_seconds: float = 0.0
    steps: int = 0
    llm_calls: int = 0
    llm_seconds: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    tools: list[str] = field(default_factory=lambda: [])
    tool_seconds: float = 0.0
    answer: str = ""


def _answer_text(blocks: list[dict[str, Any]]) -> str:
    parts = [str(b.get("markdown", "")) for b in blocks if b.get("type") == "text"]
    return " ".join(" ".join(parts).split())


async def _collect(services: Services, result: ProbeResult) -> None:
    async with services.db() as session:
        job = await session.get(Job, result.job_id)
        if job is None:
            return
        result.state = job.state
        if job.started_at is not None:
            result.queue_seconds = round((job.started_at - job.created_at).total_seconds(), 2)
        runs = list(await session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)))
        result.steps = sum(run.steps for run in runs)
        result.tokens = sum(run.total_tokens for run in runs)
        usage = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(LlmUsageRow.latency_ms), 0),
                    func.coalesce(func.sum(LlmUsageRow.cost_usd), 0),
                ).where(LlmUsageRow.job_id == job.id)
            )
        ).one()
        result.llm_calls = int(usage[0])
        result.llm_seconds = round(float(usage[1]) / 1000, 1)
        result.cost_usd = round(float(usage[2]), 4)
        if runs:
            events = await session.execute(
                select(AgentRunEvent.payload, AgentRunEvent.duration_ms)
                .where(
                    AgentRunEvent.run_id.in_([run.run_id for run in runs]),
                    AgentRunEvent.kind == "tool_result",
                )
                .order_by(AgentRunEvent.run_id, AgentRunEvent.seq)
            )
            for payload, duration_ms in events:
                result.tools.append(str((payload or {}).get("tool", "?")))
                result.tool_seconds += (duration_ms or 0) / 1000
            result.tool_seconds = round(result.tool_seconds, 1)
        reply = await session.scalar(
            select(ChatMessage)
            .where(ChatMessage.job_id == job.id, ChatMessage.role != "user")
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
        if reply is not None:
            result.answer = _answer_text(list(reply.blocks))[:ANSWER_PREVIEW]


async def probe(
    services: Services,
    user_id: str,
    question: str,
    *,
    wait_seconds: float,
    space_id: str | None = None,
) -> ProbeResult:
    async with services.db() as session:
        thread = await create_thread(
            session,
            services,
            user_id,
            ThreadCreate(title=(TITLE_PREFIX + question)[:200], space_id=space_id),
        )
        thread_id = thread.id
    started = time.monotonic()
    async with services.db() as session:
        accepted = await post_message(
            session, services, user_id, thread_id, MessageIn(text=question)
        )
    result = ProbeResult(question=question, thread_id=thread_id, job_id=accepted.job_id)
    waiting = False
    finished = False
    try:
        while time.monotonic() - started < wait_seconds:
            async with services.db() as session:
                state = await session.scalar(select(Job.state).where(Job.id == result.job_id))
            if state in DONE_STATES:
                break
            if state == "waiting_user":  # the agent asked the student: nobody here to answer
                waiting = True
                break
            await asyncio.sleep(POLL_SECONDS)
        result.seconds = round(time.monotonic() - started, 1)
        await _collect(services, result)
        finished = result.state in DONE_STATES
        if not finished:
            result.state = "asked_user" if waiting else "timeout"
    finally:
        if not finished:
            # Leave nothing running or waiting in the account once the measure is taken, also
            # when the probe itself is interrupted (Ctrl-C, a lost connection).
            await _cancel(services, user_id, result.job_id)
    return result


async def _cancel(services: Services, user_id: str, job_id: str) -> None:
    async with services.db() as session:
        await cancel_job(session, services.vault, services.clock, user_id, job_id)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="botai.ops.chat_probe",
        description="BotAI: замерить скорость ответов чата на наборе вопросов",
    )
    p.add_argument("--user", required=True, help="ULID аккаунта")
    p.add_argument("--question", action="append", default=[], help="вопрос (можно несколько)")
    p.add_argument("--questions-file", help="файл с вопросами, по одному в строке")
    p.add_argument("--space", default=None, help="ULID вуза для новых чатов (необязательно)")
    p.add_argument("--timeout", type=float, default=900.0, help="секунд на один вопрос")
    p.add_argument("--json", action="store_true", help="печатать результат одной JSON-строкой")
    return p


def _line(result: ProbeResult) -> str:
    tools = ", ".join(result.tools) or "нет"
    return (
        f"{result.seconds:>7.1f} с | шагов {result.steps:>3} | LLM {result.llm_seconds:>6.1f} с"
        f" | инструменты {result.tool_seconds:>6.1f} с | ${result.cost_usd:.4f}"
        f" | {result.state} | {result.question}\n"
        f"          инструменты: {tools}\n          ответ: {result.answer}"
    )


async def run(argv: Sequence[str], *, out: Callable[[str], None] = print) -> int:
    args = _parser().parse_args(argv)
    questions = [q.strip() for q in args.question if q.strip()]
    if args.questions_file:
        text = await asyncio.to_thread(Path(args.questions_file).read_text, encoding="utf-8")
        questions += [line.strip() for line in text.splitlines() if line.strip()]
    if not questions:
        print("Ошибка: нужен хотя бы один вопрос", file=sys.stderr)
        return 1
    services = build_services()
    async with services.db() as session:
        if await session.get(User, args.user) is None:
            print("Ошибка: аккаунт не найден", file=sys.stderr)
            return 1
    results: list[ProbeResult] = []
    for question in questions:
        result = await probe(
            services, args.user, question, wait_seconds=args.timeout, space_id=args.space
        )
        results.append(result)
        if not args.json:
            out(_line(result))
    if args.json:
        out(json.dumps([asdict(r) for r in results], ensure_ascii=False))
    return 0 if all(r.state == "done" for r in results) else 2


def main() -> None:
    raise SystemExit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
