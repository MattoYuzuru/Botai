"""Answers to the blocks of a job (`POST /jobs/{id}/answers`) and job cancellation.

A secret input never lands in `job_answers`: the value is sealed into the Vault as `transient`
(10 minutes, keyed by the block id) and the table only records *that* the block was answered. The
answer row is committed after the secret is in the Vault, so the worker that wakes on the row
always finds the secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import (
    ASKING_TYPES,
    AgentUiBlock,
    ApprovalBlock,
    ChoiceBlock,
    ConfirmBlock,
    InputBlock,
    LoginBlock,
    TextBlock,
)
from botai.core.clock import Clock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.vault import SecretKind, Vault
from botai.db.models import ChatMessage, Job, JobAnswer, JobEvent
from botai.jobs import dialogue
from botai.jobs.cleanup import lock_job_and_connection, restore_source_after_stop
from botai.jobs.events import TERMINAL, append_event, status_data
from botai.jobs.notify import CHANNEL_ANSWERS, CHANNEL_CANCEL
from botai.jobs.schemas import AnswerIn

TRANSIENT_TTL = timedelta(minutes=10)
OTHER_MAX = 500
_BLOCK = TypeAdapter(AgentUiBlock)

_BAD_ANSWER = "Ответ не подходит к вопросу"


async def get_job(session: AsyncSession, user_id: str, job_id: str) -> Job:
    """The user's job; someone else's is reported as missing (404)."""
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    return job


def _bad() -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, _BAD_ANSWER)


def _check_values(block: AgentUiBlock, values: list[str] | str | bool | None) -> None:
    """Shape and size only. Never echoes the value back: it may be a secret."""
    if isinstance(block, ChoiceBlock):
        allowed = {o.value for o in block.options}
        if isinstance(values, str):
            picked = [values]
        elif isinstance(values, list):
            picked = values
        else:
            raise _bad()
        # An optional question may be skipped ("Не знаю"): [] or "" is a valid answer then.
        picked = [v for v in picked if v != ""]
        if block.mode == "single" and len(picked) > 1:
            raise _bad()
        if block.required and not picked:
            raise _bad()
        for v in picked:
            if v not in allowed and not (block.allow_other and 0 < len(v) <= OTHER_MAX):
                raise _bad()
    elif isinstance(block, InputBlock):
        if not isinstance(values, str):
            raise _bad()
        if (block.required or block.secret) and not values:
            raise _bad()
        if len(values) > block.max_length:
            raise _bad()
    elif isinstance(block, ConfirmBlock | ApprovalBlock):
        if not isinstance(values, bool):
            raise _bad()
    elif isinstance(block, LoginBlock):
        if values is not None and not isinstance(values, bool):
            raise _bad()
    else:  # text, thinking, summary, wait, handoff: nothing to answer
        raise _bad()


async def _asked_block(
    session: AsyncSession, job_id: str, block_id: str
) -> tuple[AgentUiBlock, int]:
    event = await session.scalar(
        select(JobEvent)
        .where(
            JobEvent.job_id == job_id,
            JobEvent.type == "block",
            JobEvent.data["id"].astext == block_id,
        )
        .order_by(JobEvent.seq.desc())
        .limit(1)
    )
    if event is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Блок не найден")
    try:
        block = _BLOCK.validate_python(event.data)
    except ValidationError:  # an event we wrote ourselves; cannot normally happen
        raise BotaiError(ErrorCode.NOT_FOUND, "Блок не найден") from None
    if block.type not in ASKING_TYPES:
        raise _bad()
    return block, event.seq


async def record_answer(
    session: AsyncSession, vault: Vault, user_id: str, job_id: str, body: AnswerIn
) -> None:
    # Serialize answer acceptance with emit/cancel, which lock the same job row.
    # A handoff committed while this request was waiting must retire the native
    # question before any answer slot or transient secret can be written.
    job = await session.scalar(
        select(Job).where(Job.id == job_id, Job.user_id == user_id).with_for_update()
    )
    if job is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    if job.state in TERMINAL:
        raise BotaiError(ErrorCode.CONFLICT, "Задание уже завершено")
    previous = (dialogue.state_of(job).get("receipts") or {}).get(f"block:{body.block_id}")
    if isinstance(previous, dict) and previous.get("status") in {
        "applied",
        "superseded",
        "rejected",
    }:
        raise BotaiError(ErrorCode.BLOCK_ANSWERED, "Этот вопрос уже закрыт")
    block, asked_at = await _asked_block(session, job_id, body.block_id)
    if (
        isinstance(block, InputBlock)
        and job.connection_id is not None
        and block.input_kind != "url"
        and (
            block.autocomplete in {"username", "current-password", "one-time-code"}
            or block.input_kind in {"password", "code"}
        )
    ):
        handoff = await session.scalar(
            select(JobEvent.id)
            .where(
                JobEvent.job_id == job_id,
                JobEvent.type == "block",
                JobEvent.seq > asked_at,
                JobEvent.data["type"].astext == "login",
                JobEvent.data["connectionId"].astext == job.connection_id,
            )
            .limit(1)
        )
        if handoff is not None:
            raise BotaiError(
                ErrorCode.BLOCK_ANSWERED, "Этот шаг уже закрыт. Продолжите вход в браузере"
            )
    _check_values(block, body.values)
    secret = isinstance(block, InputBlock) and block.secret
    values = body.values
    pending = dialogue.state_of(job).get("pending")
    if (
        isinstance(pending, dict)
        and pending.get("channel") == "dialogue"
        and isinstance(values, str)
    ):
        from botai.spaces.conversations import safe_text

        values = safe_text(values)

    # Claim the answer slot first (the unique key makes a second answer lose), but commit it only
    # after the secret is in the Vault.
    claimed = await session.scalar(
        pg_insert(JobAnswer)
        .values(
            id=new_id(),
            job_id=job_id,
            block_id=body.block_id,
            values=None if secret else values,
            is_secret=secret,
        )
        .on_conflict_do_nothing(index_elements=[JobAnswer.job_id, JobAnswer.block_id])
        .returning(JobAnswer.id)
    )
    if claimed is None:
        await session.rollback()
        raise BotaiError(ErrorCode.BLOCK_ANSWERED, "На этот вопрос уже ответили")
    try:
        if secret:
            assert isinstance(body.values, str)
            await vault.put(user_id, body.block_id, SecretKind.TRANSIENT, body.values)
        accepted_at = datetime.now(UTC)
        if (
            isinstance(pending, dict)
            and pending.get("channel") == "dialogue"
            and isinstance(values, str)
            and job.thread_id is not None
        ):
            at = await dialogue.message_time(session, user_id, job.thread_id, accepted_at)
            message_id = new_id()
            text = TextBlock(id=new_id(), at=at, markdown=values)
            session.add(
                ChatMessage(
                    id=message_id,
                    user_id=user_id,
                    thread_id=job.thread_id,
                    job_id=job.id,
                    role="user",
                    blocks=[text.model_dump(by_alias=True, mode="json", exclude_none=True)],
                    created_at=at,
                )
            )
            state = dialogue.state_of(job)
            state["inbox"] = [
                *(state.get("inbox") or []),
                {"message_id": message_id, "reply_to_block_id": body.block_id},
            ]
            dialogue.save_state(job, state)
            dialogue.advance_input(job)
            dialogue.receipt(job, now=accepted_at, status="accepted", message_id=message_id)
        elif job.thread_id is not None:
            # A durable acknowledgement is not the native answer and never joins the LLM inbox.
            label = "Ответ передан"
            if isinstance(block, InputBlock) and block.input_kind == "password":
                label = "Пароль передан"
            elif isinstance(block, InputBlock) and block.input_kind == "code":
                label = "Код передан"
            at = await dialogue.message_time(session, user_id, job.thread_id, accepted_at)
            acknowledgement = TextBlock(id=new_id(), at=at, markdown=label)
            session.add(
                ChatMessage(
                    id=new_id(),
                    user_id=user_id,
                    thread_id=job.thread_id,
                    job_id=job.id,
                    role="user",
                    blocks=[
                        acknowledgement.model_dump(by_alias=True, mode="json", exclude_none=True)
                    ],
                    created_at=at,
                )
            )
        dialogue.receipt(job, now=accepted_at, status="accepted", block_id=body.block_id)
        await session.execute(select(func.pg_notify(CHANNEL_ANSWERS, job_id)))
        await session.commit()
    except BaseException:
        await session.rollback()
        if secret:
            await vault.delete(user_id, body.block_id, SecretKind.TRANSIENT)
        raise


async def stop_job(
    session: AsyncSession, clock: Clock, user_id: str, job_id: str
) -> tuple[bool, list[str]]:
    """Stops a job of the user in the caller's transaction (nothing is committed here).

    Returns whether it was stopped (a job that already ended is left alone) and the ids of the
    transient secret answers that the caller must delete from the Vault after its commit."""
    job, connection = await lock_job_and_connection(session, user_id, job_id)
    if job is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    if job.state in TERMINAL:
        return False, []
    now = clock.now()
    job.state = "cancelled"
    job.stage = "Остановлено"
    job.finished_at = now
    job.locked_by = None
    job.locked_at = None
    await restore_source_after_stop(session, job, connection)
    await dialogue.project_terminal(session, job, now)
    await append_event(session, job.id, "status", status_data(job, now))
    await session.execute(select(func.pg_notify(CHANNEL_CANCEL, job.id)))
    secrets = (
        await session.scalars(
            select(JobAnswer.block_id).where(
                JobAnswer.job_id == job.id, JobAnswer.is_secret.is_(True)
            )
        )
    ).all()
    return True, list(secrets)


async def cancel_job(
    session: AsyncSession, vault: Vault, clock: Clock, user_id: str, job_id: str
) -> None:
    """Stops a job of the user. A job that already ended is left alone (idempotent)."""
    stopped, secrets = await stop_job(session, clock, user_id, job_id)
    if not stopped:
        await session.rollback()
        return
    await session.commit()
    for block_id in secrets:  # an unconsumed secret answer must not outlive the job
        await vault.delete(user_id, block_id, SecretKind.TRANSIENT)
