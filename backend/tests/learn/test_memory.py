"""Durable knowledge resumes through fresh runtime references without storing source bodies."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from botai.core.browser import FakeBrowserHandle, FakePage, ObservedRequest
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmResponse, LlmUsage, ScriptedLlm
from botai.learn.discovery import DiscoveryBudget, adaptive_observe
from botai.learn.intent import LearningIntent
from botai.learn.memory import MAX_KNOWN_URLS, DiscoveryMemory, MemoryLimits, NegativeOutcome
from botai.learn.redact import Redactor
from tests.learn.test_discovery import plan
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
HOME, COURSE, SCHEDULE = ORIGIN + "/", ORIGIN + "/course", ORIGIN + "/schedule"
PRIVATE_BODY = "Курс о графах, приватное содержимое"


class TrackingHandle(FakeBrowserHandle):
    def __init__(self, pages: dict[str, FakePage]) -> None:
        super().__init__(pages, NOW, ORIGIN, read_only=True)
        self.visits: list[str] = []

    async def goto(self, url: str):
        self.visits.append(url)
        return await super().goto(url)


def pages() -> dict[str, FakePage]:
    def page(path: str, html: str) -> FakePage:
        return FakePage(
            html,
            requests=[
                ObservedRequest(
                    method="GET",
                    url=ORIGIN + "/api" + path,
                    status=200,
                    resource_type="xhr",
                    response_content_type="application/json",
                    response_sample=json.dumps({"items": [{"title": PRIVATE_BODY}]}),
                )
            ],
        )

    return {
        HOME: page("/home", '<main><a href="/course">Курс</a></main>'),
        COURSE: page("/course", '<main><a href="/schedule">Расписание</a></main>'),
        SCHEDULE: page("/schedule", "<main>" + PRIVATE_BODY + "</main>"),
    }


def used(response: LlmResponse, tokens: int) -> LlmResponse:
    return response.model_copy(update={"usage": LlmUsage(input_cache_miss=tokens)})


async def test_slice_checkpoint_restores_frontier_in_a_new_handle_with_cumulative_counters() -> (
    None
):
    memory = DiscoveryMemory(origin=ORIGIN, intent_key="a" * 64)
    saved: list[DiscoveryMemory] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        saved.append(value)

    first = TrackingHandle(pages())
    first_obs = await adaptive_observe(
        first,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[used(plan(1, "navigate", "l0"), 13)]),
        budget=DiscoveryBudget(steps=1),
        memory=memory,
        checkpoint=checkpoint,
    )
    assert first.visits == [HOME, COURSE]
    assert memory.frontier == [SCHEDULE]
    assert memory.resume_needed
    assert not memory.finished
    assert (memory.steps, memory.tokens, memory.requests) == (1, 13, 2)
    assert first_obs.planner_tokens == 13
    assert any(row.pending_planner_tokens > 0 for row in saved)
    serialised = saved[-1].model_dump_json()
    assert PRIVATE_BODY not in serialised
    assert "<main>" not in serialised
    assert '"p0"' not in serialised
    assert '"r0"' not in serialised
    restored = DiscoveryMemory.model_validate_json(serialised)
    restored.validate_for(ORIGIN, "a" * 64)

    second = TrackingHandle(pages())
    llm = ScriptedLlm(
        discovery_queue=[
            used(plan(1002, "navigate", "l0"), 17),
            used(plan(1003, "finish", kinds=["scheduleEvents"], evidence=["p2"]), 19),
        ]
    )
    obs = await adaptive_observe(
        second,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
        memory=restored,
        checkpoint=checkpoint,
    )
    # The two evidence pages were deliberately reread; the completed navigation is not
    # offered as a new frontier action and the deeper saved destination remains exact.
    assert second.visits == [COURSE, HOME, SCHEDULE]
    assert obs.discovered_kinds == ("scheduleEvents",)
    assert PRIVATE_BODY in obs.render()
    assert obs.planner_tokens == 36
    assert (restored.steps, restored.tokens, restored.requests) == (3, 49, 5)
    assert restored.finished
    assert not restored.resume_needed
    first_plan = json.loads(llm.observation_requests[0].messages[1].content)
    assert first_plan["observation"]["links"] == [
        {"ref": "l0", "label": "Расписание", "url": "/schedule"}
    ]


async def test_old_plan_revision_is_rejected_after_runtime_restore() -> None:
    memory = DiscoveryMemory(
        origin=ORIGIN,
        generations=1,
        visited=[HOME, COURSE],
        last_url=COURSE,
        evidence_urls=[HOME, COURSE],
        frontier=[SCHEDULE],
    )
    llm = ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0")])
    obs = await adaptive_observe(
        TrackingHandle(pages()),
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=llm,
        memory=memory,
    )
    assert any(row.reason == "invalid_plan" for row in memory.negative_outcomes)
    assert obs.limited
    assert memory.stop_reason == "invalid_plan"
    assert memory.finished
    assert not memory.resume_needed


async def test_committed_navigation_selection_survives_crash_before_its_read_effect() -> None:
    memory = DiscoveryMemory(origin=ORIGIN)
    saved: list[str] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        saved.append(value.model_dump_json())
        if value.pending_url == COURSE:
            raise asyncio.CancelledError

    first = TrackingHandle(pages())
    with pytest.raises(asyncio.CancelledError):
        await adaptive_observe(
            first,
            ORIGIN,
            Redactor(),
            start_urls=[HOME],
            intent=LearningIntent(),
            llm=ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0")]),
            memory=memory,
            checkpoint=checkpoint,
        )
    assert first.visits == [HOME]
    restored = DiscoveryMemory.model_validate_json(saved[-1])
    assert restored.pending_url == COURSE
    second = TrackingHandle(pages())
    await adaptive_observe(
        second,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(),
        memory=restored,
    )
    assert second.visits[:2] == [COURSE, HOME]
    assert restored.pending_url is None


async def test_unknown_remote_allocation_remains_charged_after_restart() -> None:
    class Interrupted(ScriptedLlm):
        async def complete_observation(self, request):
            raise asyncio.CancelledError

    saved: list[str] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        saved.append(value.model_dump_json())

    with pytest.raises(asyncio.CancelledError):
        await adaptive_observe(
            TrackingHandle(pages()),
            ORIGIN,
            Redactor(),
            start_urls=[HOME],
            intent=LearningIntent(),
            llm=Interrupted(),
            memory=DiscoveryMemory(origin=ORIGIN),
            checkpoint=checkpoint,
        )
    restored = DiscoveryMemory.model_validate_json(saved[-1])
    allocation = restored.pending_planner_tokens
    assert allocation > 0
    assert restored.reserved_tokens == allocation
    next_llm = ScriptedLlm(discovery_queue=[plan(1001, "finish")])
    next_handle = TrackingHandle(pages())
    obs = await adaptive_observe(
        next_handle,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=next_llm,
        memory=restored,
        run_limits=MemoryLimits(max_tokens=allocation),
    )
    assert next_handle.visits == []
    assert next_llm.observation_requests == []
    assert restored.reserved_tokens == allocation
    assert restored.pending_planner_tokens == 0
    assert restored.uncertain_calls == 1
    assert restored.exhausted
    assert not restored.resume_needed
    assert obs.limited
    assert restored.stop_reason == "token_budget"


async def test_run_step_budget_does_not_reset_with_a_new_slice() -> None:
    memory = DiscoveryMemory(origin=ORIGIN)
    limits = MemoryLimits(max_steps=1)
    await adaptive_observe(
        TrackingHandle(pages()),
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0")]),
        memory=memory,
        run_limits=limits,
    )
    assert memory.exhausted
    assert memory.steps == 1
    handle = TrackingHandle(pages())
    await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(),
        memory=memory,
        run_limits=limits,
    )
    assert handle.visits == []
    assert memory.steps == 1
    assert memory.exhausted


@pytest.mark.parametrize("error", [ValueError("storage"), TimeoutError("storage")])
async def test_checkpoint_failure_is_not_a_planner_error_or_source_timeout(
    error: Exception,
) -> None:
    async def checkpoint(value: DiscoveryMemory) -> None:
        if value.pending_planner_tokens:
            raise error

    llm = ScriptedLlm(discovery_queue=[plan(1, "finish")])
    with pytest.raises(type(error), match="storage"):
        await adaptive_observe(
            TrackingHandle(pages()),
            ORIGIN,
            Redactor(),
            start_urls=[HOME],
            intent=LearningIntent(),
            llm=llm,
            memory=DiscoveryMemory(origin=ORIGIN),
            checkpoint=checkpoint,
        )
    assert llm.observation_requests == []


async def test_no_progress_uses_known_frontier_before_stopping_for_the_user() -> None:
    handle = TrackingHandle(pages())
    memory = DiscoveryMemory(origin=ORIGIN)
    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "inspect", "p0"),
            plan(2, "inspect", "p0"),
            plan(2, "inspect", "p0"),
            plan(3, "finish", evidence=["p1"]),
        ]
    )
    await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=llm,
        memory=memory,
    )
    assert handle.visits == [HOME, COURSE]
    assert any(row.reason == "no_progress" for row in memory.negative_outcomes)
    assert memory.finished
    assert not memory.resume_needed


async def test_missing_planner_ends_the_bounded_legacy_fallback_with_frontier_retained() -> None:
    branches = {ORIGIN + f"/branch/{i}": FakePage("<main>Раздел</main>") for i in range(8)}
    home = FakePage(
        "<nav>" + "".join(f'<a href="/branch/{i}">Раздел</a>' for i in range(8)) + "</nav>"
    )
    memory = DiscoveryMemory(origin=ORIGIN)
    obs = await adaptive_observe(
        TrackingHandle({HOME: home, **branches}),
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(),
        memory=memory,
    )
    assert memory.finished
    assert not memory.resume_needed
    assert memory.frontier
    assert obs.limited


@pytest.mark.parametrize(
    "url",
    [
        "https://foreign.fakeuni.test/schedule",
        "/schedule",
        ORIGIN + "/?token=private",
        ORIGIN + "/schedule#access_token=private",
    ],
)
def test_memory_rejects_foreign_relative_or_credential_addresses(url: str) -> None:
    with pytest.raises(ValidationError):
        DiscoveryMemory(origin=ORIGIN, frontier=[url])


def test_memory_rejects_unknown_fields_and_explicitly_marks_knowledge_overflow() -> None:
    with pytest.raises(ValidationError):
        DiscoveryMemory.model_validate({"origin": ORIGIN, "html": "<main>private</main>"})
    memory = DiscoveryMemory(origin=ORIGIN)
    memory.merge_urls("frontier", [ORIGIN + f"/page/{i}" for i in range(MAX_KNOWN_URLS + 1)])
    assert memory.overflowed
    assert len(memory.frontier) == MAX_KNOWN_URLS
    with pytest.raises(ValueError, match="does not match"):
        memory.validate_for("https://other.fakeuni.test")
    memory.frontier.append("https://foreign.fakeuni.test/")
    with pytest.raises(ValidationError):
        memory.validate_for(ORIGIN)


async def test_auth_refusal_is_not_rescheduled_as_an_ordinary_slice_boundary() -> None:
    memory = DiscoveryMemory(origin=ORIGIN)
    obs = await adaptive_observe(
        TrackingHandle({HOME: FakePage("<main>Please log in</main>")}),
        ORIGIN,
        Redactor(),
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(),
        memory=memory,
    )
    assert obs.auth_required
    assert memory.stop_reason == "authentication"
    assert not memory.resume_needed
    assert not memory.finished


async def test_quota_error_is_propagated_with_reservation_reconciled() -> None:
    class Quota(ScriptedLlm):
        async def complete_observation(self, request):
            raise BotaiError(ErrorCode.QUOTA_EXCEEDED)

    memory = DiscoveryMemory(origin=ORIGIN)
    with pytest.raises(BotaiError) as caught:
        await adaptive_observe(
            TrackingHandle(pages()),
            ORIGIN,
            Redactor(),
            start_urls=[HOME],
            intent=LearningIntent(),
            llm=Quota(),
            memory=memory,
        )
    assert caught.value.code == ErrorCode.QUOTA_EXCEEDED
    assert memory.reserved_tokens == memory.pending_planner_tokens == 0


OPAQUE_CREDENTIAL = "opaque-cookie-first"


def encoded_credential(level: int) -> str:
    if level == 0:
        return OPAQUE_CREDENTIAL
    encoded = "".join(f"%{ord(char):02x}" for char in OPAQUE_CREDENTIAL)
    return encoded if level == 1 else quote(encoded, safe="")


@pytest.mark.parametrize("encoding", [0, 1, 2])
async def test_credential_in_neutral_href_is_omitted_before_checkpoint_and_rotation_resume(
    encoding: int,
) -> None:
    secret_value = encoded_credential(encoding)
    unsafe = ORIGIN + "/private?view=" + secret_value
    safe, deeper = ORIGIN + "/safe", ORIGIN + "/deeper"
    before_rotation = {
        HOME: FakePage(
            f'<main><a href="{unsafe}">Приватная ссылка</a>'
            '<a href="/safe">Учебный раздел</a></main>'
        ),
        safe: FakePage('<main><a href="/deeper">Дальше</a></main>'),
    }
    redactor = Redactor()
    redactor.protect_values([OPAQUE_CREDENTIAL])
    memory = DiscoveryMemory(origin=ORIGIN)
    checkpoints: list[str] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        checkpoints.append(value.model_dump_json())

    first = TrackingHandle(before_rotation)
    first_obs = await adaptive_observe(
        first,
        ORIGIN,
        redactor,
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[plan(1, "navigate", "l1")]),
        memory=memory,
        checkpoint=checkpoint,
        budget=DiscoveryBudget(steps=1),
    )
    assert first.visits == [HOME, safe]
    assert first_obs.limited
    assert memory.frontier == [deeper]
    assert memory.resume_needed
    assert all(OPAQUE_CREDENTIAL not in row for row in checkpoints)
    assert all(secret_value not in row for row in checkpoints)

    # A new runtime only knows the new credentials. Its newly rendered pages no longer carry
    # the previous session's address; the checkpoint must not replay or expose that old URL.
    after_rotation = {
        HOME: FakePage('<main><a href="/safe">Учебный раздел</a></main>'),
        safe: before_rotation[safe],
    }
    new_redactor = Redactor()
    new_redactor.protect_values(["opaque-cookie-second"])
    restored = DiscoveryMemory.model_validate_json(checkpoints[-1])
    planner = ScriptedLlm(discovery_queue=[plan(1002, "finish", evidence=["p0"])])
    second = TrackingHandle(after_rotation)
    await adaptive_observe(
        second,
        ORIGIN,
        new_redactor,
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=planner,
        memory=restored,
        checkpoint=checkpoint,
    )
    assert second.visits == [safe, HOME]
    assert len(planner.observation_requests) == 1
    prompts = "\n".join(
        message.content for request in planner.observation_requests for message in request.messages
    )
    assert OPAQUE_CREDENTIAL not in prompts
    assert secret_value not in prompts


@pytest.mark.parametrize("encoding", [0, 1, 2])
async def test_loaded_credential_addresses_are_removed_before_any_checkpoint_or_replay(
    encoding: int,
) -> None:
    value = encoded_credential(encoding)
    unsafe = ORIGIN + "/private?view=" + value
    memory = DiscoveryMemory(
        origin=ORIGIN,
        frontier=[unsafe, HOME],
        visited=[unsafe],
        evidence_urls=[unsafe],
        last_url=unsafe,
        pending_url=unsafe,
        negative_outcomes=[NegativeOutcome(url=unsafe, reason="navigation_failed")],
    )
    redactor = Redactor()
    redactor.protect_values([OPAQUE_CREDENTIAL])
    saved: list[str] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        saved.append(value.model_dump_json())

    handle = TrackingHandle({HOME: FakePage("<main>Учебный раздел</main>")})
    await adaptive_observe(
        handle,
        ORIGIN,
        redactor,
        start_urls=[unsafe, HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(),
        memory=memory,
        checkpoint=checkpoint,
    )
    assert handle.visits == [HOME]
    assert memory.pending_url is None
    assert all(OPAQUE_CREDENTIAL not in row for row in saved)
    assert all(value not in row for row in saved)
    assert all(outcome.url != unsafe for outcome in memory.negative_outcomes)


def test_secret_matcher_does_not_classify_ordinary_profile_values_as_credentials() -> None:
    redactor = Redactor()
    redactor.know("student-profile-123")
    assert not redactor.contains_secret(ORIGIN + "/students/student-profile-123")
    redactor.protect_values([OPAQUE_CREDENTIAL])
    for level in (0, 1, 2):
        assert redactor.contains_secret(ORIGIN + "/schedule?view=" + encoded_credential(level))
        assert redactor.contains_secret_address(
            ORIGIN + "/schedule?view=" + encoded_credential(level)
        )
    redactor.protect_values(["ru"])
    assert not redactor.contains_secret_address("https://fakeuni.ru/schedule")
    assert redactor.contains_secret_address("https://fakeuni.ru/schedule?view=ru")


async def test_pending_navigation_is_rechecked_after_checkpoint_before_the_read_effect() -> None:
    unsafe, safe = ORIGIN + "/private?view=" + OPAQUE_CREDENTIAL, ORIGIN + "/safe"
    redactor = Redactor()
    memory = DiscoveryMemory(origin=ORIGIN)
    saved: list[str] = []

    async def checkpoint(value: DiscoveryMemory) -> None:
        if value.pending_url == unsafe:
            # A newly inventoried credential can become known while checkpoint I/O yields.
            redactor.protect_values([OPAQUE_CREDENTIAL])
        saved.append(value.model_dump_json())

    handle = TrackingHandle(
        {
            HOME: FakePage(f'<main><a href="{unsafe}">Один</a><a href="/safe">Другой</a></main>'),
            safe: FakePage("<main>Учебный раздел</main>"),
        }
    )
    await adaptive_observe(
        handle,
        ORIGIN,
        redactor,
        start_urls=[HOME],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0")]),
        memory=memory,
        checkpoint=checkpoint,
    )
    assert handle.visits == [HOME, safe]
    assert memory.pending_url is None
    assert OPAQUE_CREDENTIAL not in saved[-1]
