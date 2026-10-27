"""PgJobContext and the answers API: blocks, waiting, secrets (FR-007, NFR-003, NFR-013)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from botai.contracts.agent_ui import (
    ChoiceBlock,
    ChoiceOption,
    ConfirmBlock,
    InputBlock,
    TextBlock,
)
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobContext, JobKind, JobState
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import Job, SecretRow
from botai.jobs.context import PgJobContext
from botai.jobs.notify import Notifier
from tests.auth.support import NOW
from tests.jobs.env import Env

pytestmark = pytest.mark.db

Payload = dict[str, Any]
SECRET = "S3cr3t-FIXTURE-d41d8"
CODE = "482915"


def choice(
    *, mode: str = "single", required: bool = True, allow_other: bool = False
) -> ChoiceBlock:
    return ChoiceBlock(
        id=new_id(),
        at=NOW,
        question="Что подключить?",
        mode=mode,  # type: ignore[arg-type]
        required=required,
        options=[
            ChoiceOption(value="schedule", label="Расписание"),
            ChoiceOption(value="lms", label="LMS"),
        ],
        allow_other=allow_other,
    )


def password_input(**kw: Any) -> InputBlock:
    return InputBlock(
        id=new_id(),
        at=NOW,
        question="Пароль",
        input_kind="password",
        required=True,
        max_length=kw.pop("max_length", 200),
        secret=True,
        autocomplete="current-password",
        **kw,
    )


def text_input(**kw: Any) -> InputBlock:
    return InputBlock(id=new_id(), at=NOW, question="Логин", input_kind="text", required=True, **kw)


async def _login(env: Env) -> tuple[httpx.AsyncClient, str, str, str]:
    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    return (
        client,
        acc.id,
        conn,
        await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn),
    )


def manual_ctx(env: Env, job_id: str, user_id: str, conn: str | None, **kw: Any) -> PgJobContext:
    """A context driven by the test itself (no worker): polling only, short."""
    return PgJobContext(
        job_id=job_id,
        user_id=user_id,
        connection_id=conn,
        db=env.sm,
        vault=env.services.vault,
        clock=env.clock,
        notifier=Notifier(None),
        poll_seconds=0.05,
        **kw,
    )


async def _mark_running(env: Env, job_id: str) -> None:
    async with env.sm() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        job.state = "running"
        await s.commit()


async def _answer(
    client: httpx.AsyncClient, job_id: str, block_id: str, values: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": block_id, "values": values}
    )


async def _wait_waiting(env: Env, job_id: str) -> None:
    await env.wait_for(job_id, {"waiting_user"})


# --- emit / status --------------------------------------------------------------------------------


async def test_emit_writes_camel_case_blocks_and_may_replace_by_id(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        block = TextBlock(id="01J00000000000000000TEXT01", at=NOW, markdown="первая")
        await ctx.emit(block)
        await ctx.emit(block.model_copy(update={"markdown": "вторая"}))

    _, _, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}))
    await env.wait_for(job_id, {"done"})
    blocks = [e.data for e in await env.events(job_id) if e.type == "block"]
    assert [b["markdown"] for b in blocks] == ["первая", "вторая"]
    assert {b["id"] for b in blocks} == {
        "01J00000000000000000TEXT01"
    }  # same id: the client replaces
    assert blocks[0]["type"] == "text"
    assert blocks[0]["at"].startswith("2026-10-27T12:00:00")


async def test_status_moves_the_job_between_running_and_waiting(env: Env) -> None:
    _, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    await ctx.status("Жду шаг на экране", state=JobState.WAITING_USER)
    job = await env.job(job_id)
    assert (job.state, job.stage) == ("waiting_user", "Жду шаг на экране")
    await ctx.status("Продолжаю")
    assert (await env.job(job_id)).state == "running"
    last = (await env.events(job_id))[-1]
    assert last.type == "status"
    assert last.data["state"] == "running"
    assert last.data["stage"] == "Продолжаю"
    assert isinstance(last.data["elapsedMs"], int)

    with pytest.raises(ValueError, match="running or waiting_user"):
        await ctx.status("x", state=JobState.DONE)
    await ctx.status("я" * 500)
    assert len((await env.job(job_id)).stage or "") == 120


async def test_a_context_refuses_to_work_for_an_ended_job(env: Env) -> None:
    _, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    async with env.sm() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        job.state = "cancelled"
        await s.commit()
    with pytest.raises(JobCancelled):
        await ctx.status("поздно")
    with pytest.raises(JobCancelled):
        await ctx.emit(TextBlock(id=new_id(), at=NOW, markdown="поздно"))


# --- ask ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("notify", [True, False], ids=["notify", "polling-only"])
async def test_ask_waits_for_the_answer_and_resumes(env: Env, notify: bool) -> None:
    block = choice()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        answer = await ctx.ask(block)
        return {"picked": answer.values, "block": answer.block_id}

    client, _, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}, notify=notify))
    await _wait_waiting(env, job_id)

    events = await env.events(job_id)
    assert [e.type for e in events][-2:] == ["block", "status"]
    assert events[-2].data["id"] == block.id
    assert events[-1].data["state"] == "waiting_user"

    assert (await _answer(client, job_id, block.id, "lms")).status_code == 204
    job = await env.wait_for(job_id, {"done"})
    assert job.result == {"picked": "lms", "block": block.id}
    states = [e.data["state"] for e in await env.events(job_id) if e.type == "status"]
    assert states == ["queued", "running", "waiting_user", "running", "done"]
    [stored] = await env.answers(job_id)
    assert (stored.block_id, stored.values, stored.is_secret) == (block.id, "lms", False)


async def test_waiting_does_not_spend_the_jobs_time_budget(env: Env) -> None:
    block = choice()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        answer = await ctx.ask(block)
        return {"picked": answer.values}

    client, _, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}, budgets={"connect": 0.4}))
    await _wait_waiting(env, job_id)
    await asyncio.sleep(0.9)  # twice the budget, spent waiting for the student
    await _answer(client, job_id, block.id, "schedule")
    assert (await env.wait_for(job_id, {"done"})).result == {"picked": "schedule"}


async def test_nobody_answering_cancels_the_job_after_the_ttl(env: Env) -> None:
    block = choice()
    seen: list[str] = []

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        try:
            await ctx.ask(block)
        except JobCancelled as exc:
            seen.append("cancelled")
            raise exc

    _, _, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}, ask_ttl=timedelta(seconds=0.3)))
    job = await env.wait_for(job_id, {"cancelled"})
    assert seen == ["cancelled"]
    assert job.stage is not None
    assert "ответ" in job.stage
    assert job.finished_at is not None
    assert (await env.events(job_id))[-1].data["state"] == "cancelled"


async def test_cancelling_while_the_job_waits_wakes_the_handler(env: Env) -> None:
    block = choice()
    seen: list[str] = []

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        try:
            await ctx.ask(block)
        except (JobCancelled, asyncio.CancelledError):  # NOTIFY cancels the task, polling raises
            seen.append("cancelled")
            raise

    client, _, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}))
    await _wait_waiting(env, job_id)
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    await env.wait_for(job_id, {"cancelled"})
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.02)
    assert seen == ["cancelled"]


async def test_ask_and_ask_secret_check_their_blocks(env: Env) -> None:
    _, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    with pytest.raises(ValueError, match="ask_secret"):
        await ctx.ask(password_input())
    with pytest.raises(ValueError, match="secret=True"):
        await ctx.ask_secret(text_input())


# --- secrets --------------------------------------------------------------------------------------


async def _database_text(env: Env) -> str:
    """Everything the job tables and the secrets table hold, as text (a leaked backup)."""
    async with env.sm() as s:
        parts = []
        for table in ("jobs", "job_events", "job_answers", "secrets"):
            parts += (await s.execute(text(f"select {table}::text from {table}"))).scalars().all()  # noqa: S608
    return "\n".join(parts)


async def test_secret_answers_go_through_the_vault_and_nowhere_else(env: Env) -> None:
    password = password_input()
    code = InputBlock(
        id=new_id(),
        at=NOW,
        question="Код",
        input_kind="code",
        required=True,
        secret=True,
        autocomplete="one-time-code",
    )

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        pw = await ctx.ask_secret(password)
        otp = await ctx.ask_secret(code)
        return {
            "password_length": len(pw.get_secret_value()),
            "code_length": len(otp.get_secret_value()),
        }

    client, uid, _, job_id = await _login(env)
    env.start(env.worker({JobKind.CONNECT: handler}))

    await _wait_waiting(env, job_id)
    assert (await _answer(client, job_id, password.id, SECRET)).status_code == 204
    for _ in range(200):  # the second question appears once the first answer is consumed
        blocks = [e.data["id"] for e in await env.events(job_id) if e.type == "block"]
        if code.id in blocks:
            break
        await asyncio.sleep(0.02)
    await _wait_waiting(env, job_id)
    assert (await _answer(client, job_id, code.id, CODE)).status_code == 204

    job = await env.wait_for(job_id, {"done"})
    assert job.result == {"password_length": len(SECRET), "code_length": len(CODE)}
    dump = await _database_text(env)
    assert SECRET not in dump
    assert CODE not in dump
    answers = {a.block_id: a for a in await env.answers(job_id)}
    assert {(a.values, a.is_secret) for a in answers.values()} == {(None, True)}
    # read once: nothing is left in the Vault either
    assert not await env.services.vault.has(uid, password.id, SecretKind.TRANSIENT)
    assert not await env.services.vault.has(uid, code.id, SecretKind.TRANSIENT)
    for e in await env.events(job_id):
        assert SECRET not in str(e.data)


async def test_a_secret_answer_sits_encrypted_for_ten_minutes_until_taken(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    asked = asyncio.create_task(ctx.ask_secret(block))
    await _wait_waiting(env, job_id)

    assert (await _answer(client, job_id, block.id, SECRET)).status_code == 204
    taken = await asyncio.wait_for(asked, 5)
    assert taken.get_secret_value() == SECRET
    assert not await env.services.vault.has(uid, block.id, SecretKind.TRANSIENT)

    # a second block, answered while nobody is waiting for it: the Vault holds it, sealed
    again = password_input()
    await ctx.emit(again)
    await ctx.status("жду", state=JobState.WAITING_USER)
    assert (await _answer(client, job_id, again.id, SECRET)).status_code == 204
    async with env.sm() as s:
        row = (await s.scalars(select(SecretRow).where(SecretRow.connection_id == again.id))).one()
    assert row.kind == "transient"
    assert row.user_id == uid
    assert row.expires_at == env.clock.now() + timedelta(minutes=10)
    assert SECRET.encode() not in row.ciphertext + row.wrapped_dek + row.nonce
    assert SECRET not in await _database_text(env)

    env.advance(minutes=10, seconds=1)  # nobody came for it: it is gone
    assert not await env.services.vault.has(uid, again.id, SecretKind.TRANSIENT)


async def test_cancelling_removes_a_secret_nobody_took(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    await ctx.emit(block)
    await ctx.status("жду", state=JobState.WAITING_USER)
    await _answer(client, job_id, block.id, SECRET)
    assert await env.services.vault.has(uid, block.id, SecretKind.TRANSIENT)

    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    assert not await env.services.vault.has(uid, block.id, SecretKind.TRANSIENT)
    assert SECRET not in await _database_text(env)
    assert (await env.job(job_id)).state == "cancelled"


async def test_a_second_answer_is_409_and_cannot_replace_a_secret(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    await ctx.emit(block)
    await ctx.status("жду", state=JobState.WAITING_USER)

    assert (await _answer(client, job_id, block.id, SECRET)).status_code == 204
    second = await _answer(client, job_id, block.id, "attacker-value")
    assert second.status_code == 409
    assert second.json()["code"] == "block_answered"
    assert "attacker" not in second.text
    kept = await env.services.vault.get(uid, block.id, SecretKind.TRANSIENT)
    assert kept is not None
    assert kept.get_secret_value() == SECRET


async def test_a_second_plain_answer_is_409(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = choice()
    await ctx.emit(block)
    assert (await _answer(client, job_id, block.id, "lms")).status_code == 204
    again = await _answer(client, job_id, block.id, "schedule")
    assert again.status_code == 409
    [stored] = await env.answers(job_id)
    assert stored.values == "lms"


async def test_racing_answers_to_one_block_accept_exactly_one(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    await ctx.emit(block)
    others = [env.new_client() for _ in range(5)]
    for c in others:  # same user, several tabs
        c.cookies.update(client.cookies)
    results = await asyncio.gather(
        *(_answer(c, job_id, block.id, f"{SECRET}-{n}") for n, c in enumerate([client, *others]))
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [204, 409, 409, 409, 409, 409]
    winner = next(n for n, r in enumerate(results) if r.status_code == 204)
    kept = await env.services.vault.get(uid, block.id, SecretKind.TRANSIENT)
    assert kept is not None
    assert kept.get_secret_value() == f"{SECRET}-{winner}"


# --- validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "values", "ok"),
    [
        (lambda: choice(), "lms", True),
        (lambda: choice(), ["lms"], True),
        (lambda: choice(), "nope", False),
        (lambda: choice(), ["lms", "schedule"], False),
        (lambda: choice(), [], False),
        (lambda: choice(), True, False),
        (lambda: choice(allow_other=True), "своё", True),
        (lambda: choice(allow_other=True), "", False),
        (lambda: choice(mode="multi"), ["lms", "schedule"], True),
        (lambda: choice(mode="multi"), ["lms", "x"], False),
        (lambda: choice(mode="multi", required=False), [], True),
        (lambda: text_input(), "ivanov", True),
        (lambda: text_input(), "", False),
        (lambda: text_input(), ["a"], False),
        (lambda: text_input(max_length=5), "123456", False),
        (lambda: password_input(), SECRET, True),
        (lambda: password_input(), "", False),
        (lambda: password_input(), [SECRET], False),
        (lambda: password_input(max_length=5), SECRET, False),
        (
            lambda: ConfirmBlock(
                id=new_id(), at=NOW, question="Точно?", confirm_label="Да", cancel_label="Нет"
            ),
            True,
            True,
        ),
        (
            lambda: ConfirmBlock(
                id=new_id(), at=NOW, question="Точно?", confirm_label="Да", cancel_label="Нет"
            ),
            "yes",
            False,
        ),
    ],
)
async def test_answers_must_fit_the_question(env: Env, make: Any, values: Any, ok: bool) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = make()
    await ctx.emit(block)
    r = await _answer(client, job_id, block.id, values)
    if ok:
        assert r.status_code == 204, r.text
    else:
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation"
        assert SECRET not in r.text  # the rejected value is never echoed back
        assert not await env.answers(job_id)  # a bad answer does not burn the question
        if isinstance(block, InputBlock) and block.secret:
            assert not await env.services.vault.has(uid, block.id, SecretKind.TRANSIENT)


async def test_answers_to_unknown_or_unanswerable_blocks(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    note = TextBlock(id=new_id(), at=NOW, markdown="Просто текст")
    await ctx.emit(note)
    assert (await _answer(client, job_id, new_id(), "x")).status_code == 404
    assert (await _answer(client, job_id, note.id, "x")).status_code == 422
    malformed = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": "short", "values": "x"}
    )
    assert malformed.status_code == 422


async def test_a_finished_job_takes_no_more_answers(env: Env) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = choice()
    await ctx.emit(block)
    async with env.sm() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        job.state = "done"
        await s.commit()
    r = await _answer(client, job_id, block.id, "lms")
    assert r.status_code == 409
    assert r.json()["code"] == "conflict"


async def test_other_users_cannot_answer_cancel_or_read_a_job(env: Env) -> None:
    owner_client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    await ctx.emit(block)
    stranger = env.new_client()
    await env.account(stranger, email="stranger@example.com")

    for method, path, kwargs in (
        ("GET", f"/api/v1/jobs/{job_id}", {}),
        (
            "POST",
            f"/api/v1/jobs/{job_id}/answers",
            {"json": {"blockId": block.id, "values": SECRET}},
        ),
        ("POST", f"/api/v1/jobs/{job_id}/cancel", {}),
    ):
        r = await stranger.request(method, path, **kwargs)
        assert r.status_code == 404, (method, path)
        assert r.json()["code"] == "not_found"
    assert not await env.answers(job_id)
    assert not await env.services.vault.has(uid, block.id, SecretKind.TRANSIENT)
    assert (await env.job(job_id)).state == "running"
    assert (await owner_client.get(f"/api/v1/jobs/{job_id}")).status_code == 200


async def test_get_job_reports_state_result_and_error_without_the_payload(env: Env) -> None:
    client, _, conn, job_id = await _login(env)
    r = await client.get(f"/api/v1/jobs/{job_id}")
    body = r.json()
    assert r.status_code == 200
    assert (body["id"], body["kind"], body["state"], body["connectionId"]) == (
        job_id,
        "connect",
        "queued",
        conn,
    )
    assert "payload" not in body
    assert body["result"] is None
    assert body["error"] is None
    assert (await client.get(f"/api/v1/jobs/{new_id()}")).status_code == 404


async def test_cancel_is_idempotent_for_finished_jobs(env: Env) -> None:
    client, _, _, job_id = await _login(env)
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    first = await env.job(job_id)
    assert first.state == "cancelled"
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    assert (await env.job(job_id)).finished_at == first.finished_at
    events = [e.data["state"] for e in await env.events(job_id) if e.type == "status"]
    assert events == ["queued", "cancelled"]


async def test_answering_a_secret_leaves_nothing_in_the_logs(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    client, uid, conn, job_id = await _login(env)
    await _mark_running(env, job_id)
    ctx = manual_ctx(env, job_id, uid, conn)
    block = password_input()
    await ctx.emit(block)
    with caplog.at_level(logging.DEBUG):
        assert (await _answer(client, job_id, block.id, SECRET)).status_code == 204
        assert (await _answer(client, job_id, block.id, SECRET)).status_code == 409
        too_long = password_input(max_length=5)
        await ctx.emit(too_long)
        assert (await _answer(client, job_id, too_long.id, SECRET)).status_code == 422
    assert SECRET not in caplog.text
