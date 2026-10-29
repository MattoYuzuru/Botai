"""Real durable jobs resume saved proposals and fresh reads through Chromium and the proxy."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import contextlib
import json
from typing import Any

import pytest
from sqlalchemy import func, select

from botai.browser.egress import get_egress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.core.vault import CipherVault, SealedSecret, SecretKind
from botai.db import models as m
from botai.jobs import dialogue
from botai.jobs.research import ResearchStore
from botai.learn import explorer as explorer_module
from botai.learn.explorer import Explorer
from botai.learn.progress import ResearchProgress
from botai.worker import handler_table
from tests.browser.test_login import FAST
from tests.e2e.support import Campus, Stack, Student
from tests.fakeuni import data
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env, live_http
from tests.learn import support as ls

pytestmark = [pytest.mark.db, pytest.mark.browser]
ENTRY = data.ORIGIN + "/app"


async def configured_learner(stack: Stack, email: str):
    env = stack.env
    learner = await Campus(env).enrol(email, url=ENTRY)
    async with env.sm() as session, session.begin():
        job = await session.get(m.Job, learner.connect_job)
        assert job is not None
        job.payload = {**job.payload, "url": ENTRY, "kinds": ["scheduleEvents"]}
    return learner


async def private_progress(env: Env, job_id: str) -> ResearchProgress:
    """Inspect the encrypted test artifact through the Vault cipher capability, never keys."""
    async with env.sm() as session:
        row = await session.get(m.JobResearchCheckpoint, job_id)
        assert row is not None
        assert isinstance(env.services.vault, CipherVault)
        payload = env.services.vault.open_value(
            row.user_id,
            ResearchStore._scope(row),
            SecretKind.RESEARCH,
            SealedSecret(row.kek_id, row.wrapped_dek, row.nonce, row.ciphertext, row.aad_version),
        )
        return ResearchProgress.model_validate_json(payload.get_secret_value())


async def stop_task(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_saved_proposal_survives_shutdown_then_fresh_runtime_canaries_without_author_llm(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    learner = await configured_learner(stack, "research-restart@example.com")
    first_llm = ScriptedLlm(queue=[ls.timetable_answer()])
    env.services.llm = first_llm
    ready, hold = asyncio.Event(), asyncio.Event()
    first_canary = Explorer._canary
    interrupted = False

    async def pause_before_first_canary(self, spec, sctx, *, require):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            progress = await private_progress(env, learner.connect_job)
            assert "scheduleEvents" in progress.proposals
            assert progress.candidates == {}
            ready.set()
            await hold.wait()
        return await first_canary(self, spec, sctx, require=require)

    monkeypatch.setattr(Explorer, "_canary", pause_before_first_canary)
    first_worker = env.worker(handler_table(), name="research-before-shutdown")
    claim = await first_worker.claim()
    assert claim is not None
    assert claim.id == learner.connect_job
    running: asyncio.Task[Any] | None = None
    attending: asyncio.Task[Any] | None = None
    new_gateway: PlaywrightGateway | None = None
    try:
        async with live_http(env.app) as http:
            student = Student(http, learner.headers())
            attending = asyncio.create_task(student.attend(learner.connect_job, within=180))
            running = asyncio.create_task(first_worker.run_job(claim))
            await asyncio.wait_for(ready.wait(), 90)
            running.cancel()  # worker shutdown, not the user's cancel endpoint
            with pytest.raises(asyncio.CancelledError):
                await running
            job = await env.job(learner.connect_job)
            assert job.state == "queued"
            assert job.payload["_retry_failures"] == 0
            assert stack.gateway.active_for(learner.user_id) is None
            assert len(first_llm.requests) == 1

            original_rows = data.timetable

            def changed_after_restart():
                rows = original_rows()
                rows[0]["subject"] = {"name": "Курс после восстановления"}
                return rows

            monkeypatch.setattr(data, "timetable", changed_after_restart)
            new_gateway = PlaywrightGateway(
                get_egress(env.settings),
                sandbox=False,
                ignore_https_errors=True,
            )
            env.services.browser = new_gateway
            env.services.sessions = LoginFlow(
                gateway=new_gateway,
                vault=env.services.vault,
                store=PgLoginStore(env.sm),
                egress=get_egress(env.settings),
                clock=env.clock,
                timing=FAST,
            )
            after_restart = ScriptedLlm()  # any attempt to author again fails loudly
            env.services.llm = after_restart
            second_worker = env.worker(handler_table(), name="research-after-shutdown")
            resumed = await second_worker.claim()
            assert resumed is not None
            assert resumed.id == claim.id
            assert resumed.attempts == claim.attempts + 1
            running = asyncio.create_task(second_worker.run_job(resumed))
            await asyncio.wait_for(running, 90)
            attendance = await asyncio.wait_for(attending, 10)
            assert attendance.result["status"] == "ok"
            assert after_restart.requests == []
            assert new_gateway.active_for(learner.user_id) is None
            async with env.sm() as session:
                titles = list(
                    await session.scalars(
                        select(m.ScheduleEvent.title).where(
                            m.ScheduleEvent.connection_id == learner.connection_id,
                        )
                    )
                )
                assert len(titles) == 4
                assert "Курс после восстановления" in titles
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.AdapterSpecRow)
                        .where(
                            m.AdapterSpecRow.origin == data.ORIGIN,
                        )
                    )
                    == 1
                )

            synced = await learner.client.post(f"/api/v1/connections/{learner.connection_id}/sync")
            assert synced.status_code == 202
            sync_claim = await second_worker.claim()
            assert sync_claim is not None
            assert sync_claim.id == synced.json()["jobId"]
            await second_worker.run_job(sync_claim)
            sync_job = await env.job(sync_claim.id)
            assert sync_job.state == "done"
            assert sync_job.result is not None
            assert sync_job.result["status"] == "ok"
            assert after_restart.requests == []
    finally:
        await stop_task(running)
        await stop_task(attending)
        if new_gateway is not None:
            await new_gateway.aclose()


class SlicePlanner(ScriptedLlm):
    def __init__(self) -> None:
        super().__init__(queue=[ls.timetable_answer()])

    async def complete_observation(self, request: LlmRequest) -> LlmResponse:
        self.observation_requests.append(request)
        state = json.loads(request.messages[1].content)["observation"]
        action, ref, evidence = "finish", None, []
        if len(self.observation_requests) == 1:
            target = next(link for link in state["links"] if link["url"] == "/portal/deadlines")
            action, ref = "navigate", target["ref"]
        else:
            timetable = next(
                row for row in state["responses"] if row["url"].startswith("/api/v2/timetable")
            )
            evidence = [timetable["ref"]]
        return LlmResponse(
            content=json.dumps(
                {
                    "stateRef": state["stateRef"],
                    "action": action,
                    "ref": ref,
                    "discoveries": [],
                    "evidenceRefs": evidence,
                }
            )
        )


async def test_one_step_slice_continues_same_job_and_does_not_spend_failure_retry(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    learner = await configured_learner(stack, "research-slice@example.com")
    env.settings.botai_research_slice_steps = 1
    planner = SlicePlanner()
    env.services.llm = planner
    worker = env.worker(handler_table(), name="research-slices")
    first = await worker.claim()
    assert first is not None
    running: asyncio.Task[Any] | None = None
    attending: asyncio.Task[Any] | None = None
    try:
        async with live_http(env.app) as http:
            attending = asyncio.create_task(
                Student(http, learner.headers()).attend(learner.connect_job)
            )
            running = asyncio.create_task(worker.run_job(first))
            await asyncio.wait_for(running, 90)
            yielded = await env.job(first.id)
            assert yielded.state == "queued"
            assert yielded.payload["_retry_failures"] == 0
            assert stack.gateway.active_for(learner.user_id) is None
            progress = await private_progress(env, first.id)
            assert progress.memory.resume_needed
            assert progress.memory.steps == 1
            assert progress.memory.last_url == data.ORIGIN + "/portal/deadlines"
            assert planner.requests == []
            second = await worker.claim()
            assert second is not None
            assert second.id == first.id
            assert second.attempts == first.attempts + 1
            running = asyncio.create_task(worker.run_job(second))
            await asyncio.wait_for(running, 90)
            attendance = await asyncio.wait_for(attending, 10)
            assert attendance.result["status"] == "ok"
            assert len(planner.requests) == 1
            assert len(planner.observation_requests) == 2
            final_job = await env.job(first.id)
            assert final_job.payload["_retry_failures"] == 0
            assert (await private_progress(env, first.id)).memory.steps == 2
    finally:
        await stop_task(running)
        await stop_task(attending)


async def test_new_scope_message_during_canary_blocks_late_activation(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    learner = await configured_learner(stack, "research-input-race@example.com")
    attached = await learner.client.post(
        f"/api/v1/connections/{learner.connection_id}/conversation",
        json={},
    )
    assert attached.status_code == 200
    thread_id = attached.json()["id"]
    env.services.llm = ScriptedLlm(queue=[ls.timetable_answer()])
    ready, release = asyncio.Event(), asyncio.Event()
    original = explorer_module.run_canary

    async def delayed_result(spec, sctx, *, require):
        result = await original(spec, sctx, require=require)
        assert result.ok
        ready.set()
        await release.wait()
        return result

    monkeypatch.setattr(explorer_module, "run_canary", delayed_result)
    worker = env.worker(handler_table(), name="research-input-race")
    claim = await worker.claim()
    assert claim is not None
    running: asyncio.Task[Any] | None = None
    attending: asyncio.Task[Any] | None = None
    try:
        async with live_http(env.app) as http:
            attending = asyncio.create_task(
                Student(http, learner.headers()).attend(learner.connect_job)
            )
            running = asyncio.create_task(worker.run_job(claim))
            await asyncio.wait_for(ready.wait(), 90)
            before = await env.job(claim.id)
            posted = await learner.client.post(
                f"/api/v1/chat/threads/{thread_id}/messages",
                json={
                    "text": "Подключите только дедлайны, расписание не нужно.",
                    "targetJobId": claim.id,
                },
            )
            assert posted.status_code == 202
            assert posted.json()["jobId"] == claim.id
            release.set()
            await asyncio.wait_for(running, 20)
            stopped = await env.job(claim.id)
            assert stopped.state == "queued"
            assert dialogue.input_revision(stopped) > dialogue.input_revision(before)
            assert stopped.payload["_retry_failures"] == 0
            async with env.sm() as session:
                conn = await session.get(m.Connection, learner.connection_id)
                assert conn is not None
                assert conn.spec_id is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.ScheduleEvent)
                        .where(
                            m.ScheduleEvent.connection_id == learner.connection_id,
                        )
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.AdapterSpecRow)
                        .where(
                            m.AdapterSpecRow.origin == data.ORIGIN,
                        )
                    )
                    == 0
                )
            progress = await private_progress(env, claim.id)
            assert progress.candidates == {}
            assert "scheduleEvents" in progress.proposals
    finally:
        release.set()
        await stop_task(running)
        await stop_task(attending)


async def test_committed_activation_survives_shutdown_before_first_sync_without_a_new_revision(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stack.env
    monkeypatch.setattr(fakeuni_login, "OTP_REQUIRED", False)
    learner = await configured_learner(stack, "research-activation-window@example.com")
    initial_llm = ScriptedLlm(queue=[ls.timetable_answer()])
    env.services.llm = initial_llm
    activated, hold = asyncio.Event(), asyncio.Event()
    original_sync = explorer_module.sync_connection
    paused = False

    async def pause_first_sync(ctx, services, connection_id, **kwargs):
        nonlocal paused
        if not paused:
            paused = True
            async with env.sm() as session:
                connection = await session.get(m.Connection, learner.connection_id)
                job = await session.get(m.Job, learner.connect_job)
                assert connection is not None
                assert job is not None
                assert connection.status == "active"
                assert connection.spec_id is not None
                assert "_research_activation" in job.payload
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.AdapterSpecRow)
                        .where(
                            m.AdapterSpecRow.origin == data.ORIGIN,
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.ScheduleEvent)
                        .where(
                            m.ScheduleEvent.connection_id == learner.connection_id,
                        )
                    )
                    == 0
                )
            activated.set()
            await hold.wait()
        return await original_sync(ctx, services, connection_id, **kwargs)

    monkeypatch.setattr(explorer_module, "sync_connection", pause_first_sync)
    worker = env.worker(handler_table(), name="research-before-activation-sync")
    first = await worker.claim()
    assert first is not None
    running: asyncio.Task[Any] | None = None
    attending: asyncio.Task[Any] | None = None
    try:
        async with live_http(env.app) as http:
            student = Student(http, learner.headers())
            attending = asyncio.create_task(student.attend(learner.connect_job, within=180))
            running = asyncio.create_task(worker.run_job(first))
            await asyncio.wait_for(activated.wait(), 90)
            questions_before_restart = list(student.history)
            assert sum(question.type == "confirm" for question in questions_before_restart) == 1
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            interrupted = await env.job(first.id)
            assert interrupted.state == "queued"
            assert "_research_activation" in interrupted.payload
            assert interrupted.payload["_retry_failures"] == 0

            original_rows = data.timetable

            def current_rows():
                rows = original_rows()
                rows[0]["subject"] = {"name": "Курс из сохранённой ревизии"}
                return rows

            monkeypatch.setattr(data, "timetable", current_rows)
            resumed_llm = ScriptedLlm()
            env.services.llm = resumed_llm
            after_restart = env.worker(handler_table(), name="research-after-activation-sync")
            second = await after_restart.claim()
            assert second is not None
            assert second.id == first.id
            running = asyncio.create_task(after_restart.run_job(second))
            await asyncio.wait_for(running, 90)
            result = await asyncio.wait_for(attending, 10)
            assert result.result["status"] == "ok"
            assert result.result["resumed_activation"] is True
            assert student.history == questions_before_restart
            assert resumed_llm.requests == []
            assert resumed_llm.observation_requests == []
            async with env.sm() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(m.AdapterSpecRow)
                        .where(
                            m.AdapterSpecRow.origin == data.ORIGIN,
                        )
                    )
                    == 1
                )
                titles = list(
                    await session.scalars(
                        select(m.ScheduleEvent.title).where(
                            m.ScheduleEvent.connection_id == learner.connection_id,
                        )
                    )
                )
                assert len(titles) == 4
                assert "Курс из сохранённой ревизии" in titles
                connection = await session.get(m.Connection, learner.connection_id)
                assert connection is not None
                assert connection.status == "active"
    finally:
        await stop_task(running)
        await stop_task(attending)
