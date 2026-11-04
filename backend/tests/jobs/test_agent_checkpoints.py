"""Checkpoint store on Postgres (D-040): save/load, TTL, compaction, size cap, tenants, cascade."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from botai.agent.v4.checkpoints import (
    ChainPosition,
    CheckpointStore,
    SegmentCheckpointer,
    compact_messages,
    encode_messages,
    encoded_size,
    protected_from,
    purge_expired_checkpoints,
)
from botai.agent.v4.runtime import INTERRUPTED_TOOL_TEXT, CheckpointSnapshot
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.llm import LlmMessage, LlmToolCall
from botai.db.models import AgentRunCheckpoint
from tests.jobs.env import Env

pytestmark = pytest.mark.db

TTL = timedelta(hours=72)
WINDOW = timedelta(hours=24)


def conversation(
    steps: int, *, tool_chars: int = 200, secret_free: bool = True
) -> tuple[LlmMessage, ...]:
    """user, then `steps` x (assistant with one call + its tool answer), like a real run."""
    messages = [LlmMessage(role="user", content="Сделай расписание")]
    for n in range(steps):
        call_id = f"call{n}"
        messages.append(
            LlmMessage(
                role="assistant",
                content=f"шаг {n}",
                reasoning_content=f"думаю {n}",
                tool_calls=[LlmToolCall(id=call_id, name="http_request", arguments={"n": n})],
            )
        )
        body = f"{n}:" + "x" * tool_chars
        messages.append(
            LlmMessage(
                role="tool",
                tool_call_id=call_id,
                content=f'<source_data tool="http_request">\n{body}\n</source_data>',
            )
        )
    return tuple(messages)


def assert_pairs(messages: tuple[LlmMessage, ...]) -> None:
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        assert message.role != "tool"
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                assert messages[index].role == "tool"
                assert messages[index].tool_call_id == call.id
                index += 1


def snapshot(
    run_id: str,
    messages: tuple[LlmMessage, ...],
    *,
    status: str = "step_limit",
    final: bool = True,
    resumable: bool = True,
    cost: float = 0.25,
    tokens: int = 1000,
) -> CheckpointSnapshot:
    return CheckpointSnapshot(
        run_id=run_id,
        status=status,  # type: ignore[arg-type]
        messages=messages,
        steps=len(messages) // 2,
        total_tokens=tokens,
        cost_usd=cost,
        resumable=resumable,
        final=final,
    )


class Fixture:
    def __init__(self, env: Env, client: httpx.AsyncClient, user_id: str, thread_id: str) -> None:
        self.env = env
        self.client = client
        self.user_id = user_id
        self.thread_id = thread_id

    def store(self, *, max_bytes: int = 6 * 1024 * 1024, ttl: timedelta = TTL) -> CheckpointStore:
        return CheckpointStore(self.env.sm, self.env.clock, max_bytes=max_bytes, ttl=ttl)

    async def job(self) -> str:
        return await self.env.services.queue.enqueue(
            JobKind.CHAT,
            user_id=self.user_id,
            thread_id=self.thread_id,
            payload={"thread_id": self.thread_id, "message_id": new_id()},
            dedupe=False,
        )

    def position(
        self, job_id: str, *, segment: int = 0, parent: str | None = None
    ) -> ChainPosition:
        return ChainPosition(
            user_id=self.user_id,
            thread_id=self.thread_id,
            job_id=job_id,
            chain_id=job_id,
            segment=segment,
            parent_run_id=parent,
            continuations=segment,
            chain_started_at=self.env.clock.now(),
        )


async def fixture(env: Env) -> Fixture:
    client = env.new_client()
    account = await env.account(client)
    thread = await client.post("/api/v1/chat/threads", json={})
    assert thread.status_code == 201, thread.text
    return Fixture(env, client, account.id, thread.json()["id"])


async def test_save_load_and_overwrite_keep_one_row_per_run(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    store = f.store()
    run_id = new_id()
    position = f.position(job)

    first = await store.save(
        position, snapshot(run_id, conversation(1), final=False, resumable=True)
    )
    assert first.saved
    assert first.message_count == 3

    second = await store.save(position, snapshot(run_id, conversation(3), final=True))
    assert second.saved
    assert second.message_count == 7
    loaded = await store.latest_for_job(f.user_id, job)
    assert loaded is not None
    assert loaded.run_id == run_id
    assert loaded.message_count == 7
    assert len(loaded.messages) == 7
    assert loaded.status == "step_limit"
    assert loaded.resumable
    assert loaded.messages == conversation(3)  # lossless round trip, reasoning included
    assert loaded.chain_id == job
    assert loaded.segment == 0
    assert loaded.expires_at - loaded.updated_at == TTL
    async with env.sm() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(AgentRunCheckpoint)
            .where(AgentRunCheckpoint.user_id == f.user_id)
        )
    assert rows == 1


async def test_cost_and_tokens_are_cumulative_over_the_chain(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    store = f.store()
    first_run, second_run = new_id(), new_id()
    await store.save(f.position(job), snapshot(first_run, conversation(2), cost=0.5, tokens=900))
    resumed = replace(
        f.position(job, segment=1, parent=first_run), cost_before=0.5, tokens_before=900
    )
    await store.save(resumed, snapshot(second_run, conversation(4), cost=0.25, tokens=400))

    latest = await store.latest_for_job(f.user_id, job)
    assert latest is not None
    assert latest.run_id == second_run
    assert latest.segment == 1
    assert latest.parent_run_id == first_run
    assert latest.cost_usd_chain == pytest.approx(0.75)
    assert latest.tokens_chain == 1300
    segments = await store.segments_of_job(f.user_id, job)
    assert [segment for segment, _, _ in segments] == [0, 1]


async def test_an_expired_checkpoint_is_invisible_and_purged(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    store = f.store(ttl=timedelta(hours=1))
    await store.save(f.position(job), snapshot(new_id(), conversation(2)))
    assert await store.latest_for_job(f.user_id, job) is not None

    env.advance(hours=2)
    assert await store.latest_for_job(f.user_id, job) is None
    assert await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW) is None
    assert await purge_expired_checkpoints(env.sm, env.clock.now()) == 1
    async with env.sm() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRunCheckpoint)) == 0


async def test_a_thread_resumes_only_the_last_turn_when_it_stopped_and_is_fresh(env: Env) -> None:
    f = await fixture(env)
    store = f.store()
    stopped_job = await f.job()
    run_id = new_id()
    await store.save(f.position(stopped_job), snapshot(run_id, conversation(3)))

    found = await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW)
    assert found is not None
    assert found.run_id == run_id
    assert (await store.resumable_by_run(f.user_id, f.thread_id, run_id, window=WINDOW)) is not None

    # a newer turn that did not leave a checkpoint hides the old one: it already moved on
    newer = await f.job()
    assert await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW) is None
    # the button of the old turn is stale too, for everyone but that newer job itself
    assert await store.resumable_by_run(f.user_id, f.thread_id, run_id, window=WINDOW) is None
    named = await store.resumable_by_run(
        f.user_id, f.thread_id, run_id, window=WINDOW, exclude_job_id=newer
    )
    assert named is not None
    assert named.run_id == run_id
    # ... except for that newer job itself, which is the one asking
    asking = await store.resumable_in_thread(
        f.user_id, f.thread_id, window=WINDOW, exclude_job_id=newer
    )
    assert asking is not None
    assert asking.run_id == run_id

    # an answered turn is not resumable
    await store.save(
        f.position(newer), snapshot(new_id(), conversation(1), status="answer", resumable=False)
    )
    assert await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW) is None

    # freshness: older than the window is not offered, even though the row still exists
    env.advance(hours=25)
    assert (
        await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW, exclude_job_id=newer)
    ) is None
    assert await store.resumable_by_run(f.user_id, f.thread_id, run_id, window=WINDOW) is None
    assert await store.get(f.user_id, run_id) is not None


async def test_checkpoints_belong_to_their_user_and_thread(env: Env) -> None:
    a = await fixture(env)
    b = await fixture(env)
    job = await a.job()
    run_id = new_id()
    await a.store().save(a.position(job), snapshot(run_id, conversation(2)))

    store = a.store()
    assert await store.get(b.user_id, run_id) is None
    assert await store.latest_for_job(b.user_id, job) is None
    assert await store.resumable_by_run(b.user_id, b.thread_id, run_id, window=WINDOW) is None
    assert await store.resumable_by_run(a.user_id, b.thread_id, run_id, window=WINDOW) is None
    assert await store.resumable_in_thread(b.user_id, a.thread_id, window=WINDOW) is None
    assert await store.segments_of_job(b.user_id, job) == []

    # a second user cannot overwrite the first one's row through the same run id
    await b.store().save(b.position(job), snapshot(run_id, conversation(9)))
    kept = await store.get(a.user_id, run_id)
    assert kept is not None
    assert kept.message_count == 5


async def test_deleting_the_thread_removes_its_checkpoints(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    run_id = new_id()
    await f.store().save(f.position(job), snapshot(run_id, conversation(2)))
    assert await f.store().get(f.user_id, run_id) is not None

    response = await f.client.delete(f"/api/v1/chat/threads/{f.thread_id}")
    assert response.status_code in (200, 204), response.text
    assert await f.store().get(f.user_id, run_id) is None


# --- compaction ------------------------------------------------------------------------------------


def test_compaction_is_a_no_op_below_the_cap() -> None:
    messages = conversation(5)
    compacted = compact_messages(messages, 10_000_000)
    assert compacted.messages == messages
    assert not compacted.compacted
    assert compacted.fits


def test_compaction_shortens_old_tool_results_and_keeps_the_pairs_and_the_tail() -> None:
    messages = conversation(40, tool_chars=6000)
    cap = encoded_size(encode_messages(messages)) // 2
    compacted = compact_messages(messages, cap)

    assert compacted.fits
    assert compacted.compacted
    assert compacted.size <= cap
    assert len(compacted.messages) == len(messages)
    assert_pairs(compacted.messages)
    boundary = protected_from(messages, 20)
    assert compacted.messages[boundary:] == messages[boundary:]  # the last ~20 stay whole
    old_tools = [m for m in compacted.messages[:boundary] if m.role == "tool"]
    assert old_tools
    assert all("[сокращено:" in m.content for m in old_tools)
    assert all(
        m.content.startswith("<source_data") and m.content.endswith("</source_data>")
        for m in old_tools
    )
    # assistant turns, reasoning and call ids are untouched
    assert [m.reasoning_content for m in compacted.messages if m.role == "assistant"] == [
        m.reasoning_content for m in messages if m.role == "assistant"
    ]
    assert compact_messages(messages, cap) == compacted  # deterministic


def test_the_protected_tail_never_starts_inside_a_call_group() -> None:
    messages = (
        LlmMessage(role="user", content="q"),
        LlmMessage(
            role="assistant",
            tool_calls=[
                LlmToolCall(id="a", name="t", arguments={}),
                LlmToolCall(id="b", name="t", arguments={}),
                LlmToolCall(id="c", name="t", arguments={}),
            ],
        ),
        LlmMessage(role="tool", tool_call_id="a", content="A"),
        LlmMessage(role="tool", tool_call_id="b", content="B"),
        LlmMessage(role="tool", tool_call_id="c", content="C"),
        LlmMessage(role="assistant", content="готово"),
    )
    for keep in range(1, 7):
        start = protected_from(messages, keep)
        assert messages[start].role != "tool"
    assert protected_from(messages, 1) == 5
    assert protected_from(messages, 2) == 1  # would start at tool "c": back to its assistant
    assert protected_from(messages, 3) == 1  # would start at tool "b": back to its assistant


def test_a_conversation_that_cannot_shrink_does_not_fit() -> None:
    huge_user = (LlmMessage(role="user", content="я" * 5_000_000),)
    result = compact_messages(huge_user, 1_000_000)
    assert not result.fits


async def test_a_big_snapshot_is_compacted_and_saved(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    messages = conversation(60, tool_chars=8000)
    cap = encoded_size(encode_messages(messages)) // 2
    outcome = await f.store(max_bytes=cap).save(f.position(job), snapshot(new_id(), messages))
    assert outcome.saved
    assert outcome.compacted
    assert outcome.bytes <= cap
    loaded = await f.store().latest_for_job(f.user_id, job)
    assert loaded is not None
    assert loaded.bytes == outcome.bytes
    assert_pairs(loaded.messages)


async def test_a_snapshot_over_the_cap_is_not_saved_and_disowns_the_chain(env: Env) -> None:
    f = await fixture(env)
    job = await f.job()
    store = f.store(max_bytes=64 * 1024)
    earlier = new_id()
    assert (await store.save(f.position(job), snapshot(earlier, conversation(1)))).saved

    giant = (LlmMessage(role="user", content="я" * 200_000), *conversation(2))
    sink = SegmentCheckpointer(store, f.position(job, segment=1, parent=earlier))
    await sink.save(snapshot(new_id(), giant, final=True))
    assert not sink.final_saved
    assert sink.final is not None
    assert sink.final.bytes > 64 * 1024

    stale = await store.get(f.user_id, earlier)
    assert stale is not None
    assert stale.resumable is False
    assert await store.resumable_in_thread(f.user_id, f.thread_id, window=WINDOW) is None


async def test_the_synthetic_answer_of_an_interrupted_call_survives_the_round_trip(
    env: Env,
) -> None:
    f = await fixture(env)
    job = await f.job()
    messages = (
        LlmMessage(role="user", content="q"),
        LlmMessage(role="assistant", tool_calls=[LlmToolCall(id="x", name="t", arguments={})]),
        LlmMessage(role="tool", tool_call_id="x", content=INTERRUPTED_TOOL_TEXT),
    )
    await f.store().save(f.position(job), snapshot(new_id(), messages, status="cancelled"))
    loaded = await f.store().latest_for_job(f.user_id, job)
    assert loaded is not None
    assert loaded.messages == messages
    assert loaded.status == "cancelled"


async def test_the_first_save_of_a_resuming_turn_retires_the_checkpoint_it_took_over(
    env: Env,
) -> None:
    f = await fixture(env)
    store = f.store()
    old_job = await f.job()
    old_run = new_id()
    await store.save(f.position(old_job), snapshot(old_run, conversation(2)))
    new_job = await f.job()
    sink = SegmentCheckpointer(store, f.position(new_job, parent=old_run))

    # until the new turn has a snapshot of its own the old one is still where the work stands
    before = await store.get(f.user_id, old_run)
    assert before is not None
    assert before.resumable
    await sink.save(snapshot(new_id(), conversation(3), status="running", final=False))

    taken = await store.get(f.user_id, old_run)
    assert taken is not None
    assert not taken.resumable
    named = await store.resumable_by_run(
        f.user_id, f.thread_id, old_run, window=WINDOW, exclude_job_id=new_job
    )
    assert named is None


async def test_a_later_segment_does_not_retire_its_parent(env: Env) -> None:
    f = await fixture(env)
    store = f.store()
    job = await f.job()
    first = new_id()
    await store.save(f.position(job), snapshot(first, conversation(2)))
    sink = SegmentCheckpointer(store, f.position(job, segment=1, parent=first))
    await sink.save(snapshot(new_id(), conversation(3)))
    parent = await store.get(f.user_id, first)
    assert parent is not None
    assert parent.resumable  # the job's own earlier segment is history of the chain, not taken over


async def test_nul_and_lone_surrogates_do_not_break_the_snapshot(env: Env) -> None:
    f = await fixture(env)
    store = f.store()
    job = await f.job()
    run_id = new_id()
    dirty = (
        LlmMessage(role="user", content="Сделай"),
        LlmMessage(
            role="assistant",
            tool_calls=[
                LlmToolCall(
                    id="c1", name="http_request", arguments={"q": "a\x00b", "k\x00": "\ud800"}
                )
            ],
        ),
        LlmMessage(role="tool", tool_call_id="c1", content="page\x00 text \ud83d and \udc00 end"),
    )

    encoded = encode_messages(dirty)
    assert "\x00" not in str(encoded)
    assert encoded_size(encoded) > 0  # a lone surrogate no longer breaks the size calculation

    outcome = await store.save(f.position(job), snapshot(run_id, dirty))
    assert outcome.saved
    loaded = await store.get(f.user_id, run_id)
    assert loaded is not None
    assert loaded.messages[2].content == "page text � and � end"
    assert loaded.messages[1].tool_calls[0].arguments == {"q": "ab", "k": "�"}
