"""Research ledger reconciliation keeps work charged and proposals portable across claims."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr, ValidationError

from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobDeferred, JobInputChanged
from botai.core.llm import LlmMessage, LlmRequest, LlmResponse, LlmUsage, ScriptedLlm
from botai.core.research import ResearchCheckpoint, ResearchUsage
from botai.jobs.research import ResearchConflict
from botai.learn.author import Candidate
from botai.learn.intent import LearningIntent
from botai.learn.progress import ResearchLedger, ResearchProgress, SavedCandidate, intent_key
from botai.settings import Settings
from tests.sources import recipes

ORIGIN = "https://fakeuni.test"
INTENT = LearningIntent(entry_urls=(ORIGIN + "/app",), requested_kinds=("scheduleEvents",))


@dataclass
class MemoryContext:
    stored: ResearchCheckpoint | None = None
    history: list[ResearchCheckpoint] = field(default_factory=list)
    conflict: bool = False
    input_changed: bool = False

    async def load_research(self) -> ResearchCheckpoint | None:
        return self.stored

    async def save_research(
        self, payload: SecretStr, *, intent_key: str, expected_revision: int
    ) -> ResearchCheckpoint:
        if self.input_changed:
            raise JobInputChanged
        revision = self.stored.revision if self.stored else 0
        if self.conflict or revision != expected_revision:
            raise ResearchConflict
        self.stored = ResearchCheckpoint(revision + 1, intent_key, payload)
        self.history.append(self.stored)
        return self.stored


async def load(
    ctx: MemoryContext,
    settings: Settings | None = None,
    *,
    intent: LearningIntent = INTENT,
) -> ResearchLedger:
    return await ResearchLedger.load(
        ctx,
        settings or Settings(_env_file=None),
        user_id="fixture-user",
        job_id="fixture-job",
        connection_id="fixture-connection",
        origin=ORIGIN,
        allow_hosts=("fakeuni.test",),
        intent=intent,
    )


def proposal() -> SavedCandidate:
    return SavedCandidate.of(
        Candidate(
            "scheduleEvents",
            recipes.timetable_endpoint(),
            [],
            {"type": "cookie"},
        )
    )


async def test_proposals_survive_restore_independently_of_verified_candidates() -> None:
    ctx = MemoryContext()
    ledger = await load(ctx)
    original = proposal()
    ledger.state.proposals["scheduleEvents"] = [original]
    await ledger.save()
    original.endpoint["request"]["path"] = "/mutated-after-save"

    restored = await load(ctx)

    assert restored.state.candidates == {}
    saved = restored.state.proposals["scheduleEvents"][0]
    assert saved.endpoint["request"]["path"] == "/api/v2/timetable"
    candidate = saved.candidate()
    candidate.endpoint["request"]["path"] = "/local-copy"
    assert saved.endpoint["request"]["path"] == "/api/v2/timetable"
    assert restored.revision == 1


async def test_successful_model_call_reconciles_actual_usage_and_pending_reservations() -> None:
    ctx = MemoryContext()
    ledger = await load(ctx)
    provider = ScriptedLlm(
        queue=[LlmResponse(content="{}", usage=LlmUsage(input_cache_miss=31, output=7))]
    )
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="synthetic structure")], max_tokens=50
    )

    await ledger.llm(provider).complete(request)

    assert ledger.state.other_tokens == 38
    assert ledger.state.pending_tokens == ledger.state.pending_active_ms == 0
    reserved = ResearchProgress.model_validate_json(ctx.history[0].payload.get_secret_value())
    assert reserved.pending_tokens > 50
    assert reserved.pending_active_ms > 0
    restored = await load(ctx)
    assert restored.state.tokens == 38
    assert restored.state.uncertain_calls == 0


async def test_crash_reservations_are_charged_once_and_not_reset_by_intent_change() -> None:
    ctx = MemoryContext()
    ledger = await load(ctx)
    ledger.state.memory.steps = 7
    ledger.state.memory.tokens = 19
    ledger.state.memory.frontier = [ORIGIN + "/next"]
    ledger.state.proposals["scheduleEvents"] = [proposal()]
    with pytest.raises(asyncio.CancelledError):
        async with ledger.operation("author", reserve_tokens=300):
            raise asyncio.CancelledError
    pending = ResearchProgress.model_validate_json(ctx.history[-1].payload.get_secret_value())
    assert pending.pending_tokens == 300
    assert pending.pending_active_ms > 0

    resumed = await load(ctx)
    assert resumed.state.other_tokens == 300
    assert resumed.state.other_active_ms == pending.pending_active_ms
    assert resumed.state.uncertain_calls == 1
    await resumed.save()
    narrowed = LearningIntent(entry_urls=INTENT.entry_urls, requested_kinds=("deadlines",))
    changed = await load(ctx, intent=narrowed)
    assert changed.state.proposals == changed.state.candidates == {}
    assert changed.state.memory.frontier == []
    assert changed.state.memory.steps == 7
    assert changed.state.memory.tokens == 19
    assert changed.state.other_tokens == 300
    assert changed.state.uncertain_calls == 1
    assert changed.state.intent_key == intent_key(narrowed)


async def test_total_model_and_request_limits_prevent_effects_before_provider_call() -> None:
    ctx = MemoryContext()
    settings = Settings(
        _env_file=None, botai_research_max_tokens=1000, botai_research_max_requests=1
    )
    ledger = await load(ctx, settings)
    provider = ScriptedLlm(queue=[LlmResponse(content="unused")])
    request = LlmRequest(messages=[LlmMessage(role="user", content="x" * 1000)], max_tokens=1000)
    with pytest.raises(BotaiError) as tokens:
        await ledger.llm(provider).complete(request)
    assert tokens.value.code == ErrorCode.QUOTA_EXCEEDED
    assert provider.requests == []
    await ledger.before_request()
    with pytest.raises(BotaiError) as requests:
        await ledger.before_request()
    assert requests.value.code == ErrorCode.SOURCE_RATE_LIMITED
    assert ledger.state.other_requests == 1


async def test_cas_loss_defers_and_input_change_propagates_without_erasing_checkpoint() -> None:
    ctx = MemoryContext()
    ledger = await load(ctx)
    await ledger.save()
    before = ctx.stored
    ctx.conflict = True
    with pytest.raises(JobDeferred):
        await ledger.save()
    assert ctx.stored == before
    ctx.conflict, ctx.input_changed = False, True
    with pytest.raises(JobInputChanged):
        await ledger.save()
    assert ctx.stored == before


async def test_invalid_checkpoint_is_rejected_without_exposing_its_content() -> None:
    marker = "private-malformed-research-FIXTURE"
    ctx = MemoryContext(stored=ResearchCheckpoint(1, intent_key(INTENT), SecretStr(marker)))
    with pytest.raises(BotaiError) as caught:
        await load(ctx)
    assert caught.value.code == ErrorCode.RECIPE_REJECTED
    assert marker not in str(caught.value)
    with pytest.raises(ValidationError):
        SavedCandidate.model_validate(
            {
                "kind": "scheduleEvents",
                "endpoint": {},
                "auth": {"type": "cookie"},
                "html": marker,
            }
        )


@dataclass
class BudgetMemoryContext(MemoryContext):
    usage: ResearchUsage | None = None

    async def load_research_usage(self) -> ResearchUsage | None:
        return self.usage

    async def save_research_with_usage(
        self, payload: SecretStr, *, intent_key: str, expected_revision: int, usage: ResearchUsage
    ) -> ResearchCheckpoint:
        checkpoint = await self.save_research(
            payload, intent_key=intent_key, expected_revision=expected_revision
        )
        self.usage = usage
        return checkpoint


async def test_expired_knowledge_does_not_reset_job_budget_or_uncertain_reservations() -> None:
    ctx = BudgetMemoryContext()
    settings = Settings(_env_file=None, botai_research_max_tokens=1000)
    ledger = await load(ctx, settings)
    ledger.state.other_tokens = 900
    ledger.state.memory.steps = 7
    ledger.state.memory.requests = 11
    ledger.state.memory.frontier = [ORIGIN + "/section"]
    with pytest.raises(asyncio.CancelledError):
        async with ledger.operation("author", reserve_tokens=100):
            raise asyncio.CancelledError
    assert ctx.usage is not None
    assert ctx.usage.tokens == 1000
    assert ctx.usage.active_ms > 0
    ctx.stored = None  # knowledge TTL/maintenance, but the Job survives
    restored = await load(ctx, settings)
    assert restored.state.tokens == 1000
    assert restored.state.active_ms == ctx.usage.active_ms
    assert restored.state.memory.steps == 7
    assert restored.state.other_requests == 11
    assert restored.state.memory.frontier == []
    provider = ScriptedLlm()
    with pytest.raises(BotaiError) as failure:
        await restored.llm(provider).complete(
            LlmRequest(messages=[LlmMessage(role="user", content="structure")], max_tokens=10)
        )
    assert failure.value.code == ErrorCode.QUOTA_EXCEEDED
    assert provider.requests == []


async def test_a_question_already_asked_and_the_probe_count_survive_restart_and_a_new_intent() -> (
    None
):
    ctx = MemoryContext()
    ledger = await load(ctx)
    ledger.state.horizon_asked.append("scheduleEvents")
    ledger.state.memory.probes = 3
    await ledger.save()

    restarted = await load(ctx)
    assert restarted.state.horizon_asked == ["scheduleEvents"]

    # the answer to the question changes the intent; the research keeps what it already spent
    answered = await load(ctx, intent=LearningIntent(entry_urls=INTENT.entry_urls, user_note="Да"))
    assert answered.state.intent_key != restarted.state.intent_key
    assert answered.state.horizon_asked == ["scheduleEvents"]
    assert answered.state.memory.probes == 3


async def test_a_link_to_another_site_does_not_discard_what_the_research_accepted() -> None:
    """The link becomes a source of its own; the proofs of this one stay valid."""
    ctx = MemoryContext()
    ledger = await load(ctx)
    ledger.state.candidates["scheduleEvents"] = proposal()
    await ledger.save()
    foreign = LearningIntent(
        entry_urls=(*INTENT.entry_urls, "https://calendar.example.test/page"),
        requested_kinds=INTENT.requested_kinds,
    )
    assert intent_key(foreign, ORIGIN) == intent_key(INTENT, ORIGIN)
    assert intent_key(foreign) != intent_key(INTENT)  # without an origin every link counts

    restored = await load(ctx, intent=foreign)

    assert restored.state.intent_key == intent_key(INTENT, ORIGIN)
    assert set(restored.state.candidates) == {"scheduleEvents"}


# --- request budget pre-check -----------------------------------------------------------------


async def test_before_request_reserves_a_batch_so_the_total_never_overruns() -> None:
    ctx = MemoryContext()
    settings = Settings(_env_file=None, botai_research_max_requests=10)
    ledger = await load(ctx, settings)

    await ledger.before_request(4)
    await ledger.before_request(4)
    assert ledger.state.other_requests == 8

    # A batch that would not fit is refused before it is made: the count stays below the maximum.
    with pytest.raises(BotaiError) as refused:
        await ledger.before_request(3)
    assert refused.value.code == ErrorCode.SOURCE_RATE_LIMITED
    assert "8 из 10" in refused.value.detail
    assert ledger.state.other_requests == 8

    await ledger.before_request(2)  # exactly the remaining room
    assert ledger.state.other_requests == 10
    with pytest.raises(BotaiError) as exhausted:
        await ledger.before_request()
    assert exhausted.value.code == ErrorCode.SOURCE_RATE_LIMITED


# --- stopping a research that finds nothing new -----------------------------------------------


async def test_slices_without_progress_end_the_research_instead_of_being_deferred_forever() -> None:
    ctx = MemoryContext()
    settings = Settings(_env_file=None, botai_research_no_progress_slices=3)
    ledger = await load(ctx, settings)

    await ledger.end_slice()  # the baseline: what the research knew at the first slice end
    assert ledger.state.no_progress_slices == 0
    await ledger.end_slice()
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 2
    assert await load(ctx, settings) is not None  # the state is durable between attempts

    with pytest.raises(BotaiError) as stopped:
        await ledger.end_slice()
    assert stopped.value.code == ErrorCode.SOURCE_NO_PROGRESS
    assert "3 слайса подряд" in stopped.value.detail
    assert "0 запросов" in stopped.value.detail
    # The counter is an additive field of the same private checkpoint, no migration involved.
    stored = ResearchProgress.model_validate_json(ctx.history[-1].payload.get_secret_value())
    assert stored.no_progress_slices == 3  # the stop itself is in the checkpoint
    assert len(stored.findings_digest) == 64


async def test_a_slice_that_found_a_page_or_a_candidate_resets_the_counter() -> None:
    ctx = MemoryContext()
    settings = Settings(_env_file=None, botai_research_no_progress_slices=3)
    ledger = await load(ctx, settings)
    await ledger.end_slice()
    await ledger.end_slice()
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 2

    ledger.state.memory.visited.append(ORIGIN + "/deadlines")
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 0

    # Accepted candidates are findings too, even when the pages are the same.
    ledger.state.candidates["scheduleEvents"] = proposal()
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 0

    ledger.state.proposals["deadlines"] = [proposal()]
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 0


async def test_a_changed_intent_gets_a_fresh_no_progress_chance() -> None:
    ctx = MemoryContext()
    settings = Settings(_env_file=None, botai_research_no_progress_slices=3)
    ledger = await load(ctx, settings)
    await ledger.end_slice()
    await ledger.end_slice()
    await ledger.end_slice()
    assert ledger.state.no_progress_slices == 2

    changed = await load(
        ctx,
        settings,
        intent=LearningIntent(entry_urls=INTENT.entry_urls, requested_kinds=("deadlines",)),
    )
    assert changed.state.no_progress_slices == 0
    assert changed.state.findings_digest == ""
