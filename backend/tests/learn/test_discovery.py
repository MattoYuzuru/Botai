"""Adaptive observation uses executor-owned references and keeps author fixtures separate."""

from __future__ import annotations

import json

import pytest

from botai.browser.read_views import ReadView
from botai.core.browser import (
    FakeBrowserGateway,
    FakeBrowserHandle,
    FakePage,
    ObservedRequest,
    PageSnapshot,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmMessage, LlmRequest, LlmResponse, ScriptedLlm
from botai.db import models as m
from botai.learn.discovery import DiscoveryBudget, ObservationSession, adaptive_observe
from botai.learn.intent import LearningIntent, apply_intent, prepare_turn
from botai.learn.observe import observe
from botai.learn.redact import Redactor
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources.helpers import NOW, new_session_token

ORIGIN = "https://fakeuni.test"


class TrackingHandle(FakeBrowserHandle):
    def __init__(self, pages: dict[str, FakePage]) -> None:
        super().__init__(pages, NOW, ORIGIN, read_only=True)
        self.visits: list[str] = []

    async def goto(self, url: str):
        self.visits.append(url)
        return await super().goto(url)


def plan(
    revision: int,
    action: str,
    ref: str | None = None,
    *,
    kinds: list[str] | None = None,
    evidence: list[str] | None = None,
) -> LlmResponse:
    return LlmResponse(
        content=json.dumps(
            {
                "stateRef": revision,
                "action": action,
                "ref": ref,
                "discoveries": kinds or [],
                "evidenceRefs": evidence or [],
            }
        )
    )


async def test_planner_selects_tenth_link_and_inspects_fresh_network_evidence() -> None:
    home = ORIGIN + "/"
    courses = ORIGIN + "/courses"
    schedule = ORIGIN + "/courses/schedule"
    handle = TrackingHandle(
        {
            home: FakePage(
                "<nav>"
                + "".join(f'<a href="/other/{i}">Сервис {i}</a>' for i in range(9))
                + '<a href="/courses">Курсы</a></nav>'
            ),
            courses: FakePage(
                '<main><article>Базы данных</article><a href="/courses/schedule">Занятия курса</a></main>'
            ),
            schedule: FakePage(
                "<main>Занятия курса</main>",
                requests=[
                    ObservedRequest(
                        method="GET",
                        url=ORIGIN + "/api/schedule",
                        status=200,
                        resource_type="xhr",
                        response_content_type="application/json",
                        response_sample=json.dumps(
                            {
                                "items": [
                                    {
                                        "title": "Курс о графах",
                                        "startsAt": "2026-10-28T09:00:00+03:00",
                                    }
                                ]
                            }
                        ),
                    )
                ],
            ),
        }
    )
    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "navigate", "l9"),
            plan(2, "navigate", "l10", kinds=["courses"], evidence=["p1"]),
            plan(3, "inspect", "r0", kinds=["scheduleEvents"], evidence=["r0"]),
            plan(4, "finish", kinds=["courses", "scheduleEvents"], evidence=["p1", "r0"]),
        ]
    )
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[home],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
    )
    assert handle.visits == [home, courses, schedule]
    assert obs.discovered_kinds == ("courses", "scheduleEvents")
    assert not obs.limited
    assert "Курс о графах" in obs.render()
    assert len(llm.observation_requests) == 4
    assert llm.requests == []
    first = json.loads(llm.observation_requests[0].messages[1].content)
    assert first["observation"]["links"][9]["ref"] == "l9"
    fresh = json.loads(llm.observation_requests[2].messages[1].content)
    assert "r0" in fresh["observation"]["newEvidenceRefs"]


async def test_explicit_kinds_and_absent_kinds_cannot_be_broadened_by_planner() -> None:
    handle = TrackingHandle({ORIGIN + "/": FakePage("<main>Курсы и оценки</main>")})
    llm = ScriptedLlm(
        discovery_queue=[plan(1, "finish", kinds=["courses", "grades"], evidence=["p0"])]
    )
    intent = LearningIntent(requested_kinds=("scheduleEvents",), absent_kinds=("grades",))
    obs = await adaptive_observe(
        handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"], intent=intent, llm=llm
    )
    assert obs.discovered_kinds == ()
    assert intent.requested_kinds == ("scheduleEvents",)


def test_explicit_only_schedule_replaces_types_previously_inferred_in_auto_mode() -> None:
    current = LearningIntent(requested_kinds=("courses", "assignments"), discovery_mode="auto")
    changed = apply_intent(
        current,
        prepare_turn("Подключи только расписание"),
        {"kinds": ["scheduleEvents"], "linkIndices": [], "absentKinds": []},
    )
    assert changed.requested_kinds == ("scheduleEvents",)
    assert changed.discovery_mode == "requested"
    assert "discovery_mode" not in changed.checkpoint()


@pytest.mark.parametrize(
    "invalid",
    [
        {"stateRef": 1, "action": "navigate", "ref": "l999"},
        {"stateRef": 0, "action": "inspect", "ref": "p0"},
        {"stateRef": 1, "action": "navigate", "ref": "https://other.test/"},
        {"stateRef": 1, "action": "finish", "ref": None, "url": "https://other.test/"},
        {
            "stateRef": 1,
            "action": "finish",
            "ref": None,
            "discoveries": ["unknown"],
            "evidenceRefs": ["p0"],
        },
        {
            "stateRef": 1,
            "action": "finish",
            "ref": None,
            "discoveries": ["courses"],
            "evidenceRefs": ["l0"],
        },
        {"stateRef": 1, "action": [], "ref": None},
    ],
)
async def test_invented_stale_or_executable_plan_never_becomes_browser_action(
    invalid: object,
) -> None:
    handle = TrackingHandle({ORIGIN + "/": FakePage('<main><a href="/courses">Курсы</a></main>')})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    with pytest.raises(
        ValueError, match=r"observation|reference|discovered|destination|evidence|link"
    ):
        state.decision(invalid)
    assert handle.visits == [ORIGIN + "/"]


async def test_malformed_planner_falls_back_without_consuming_recipe_response() -> None:
    handle = TrackingHandle({ORIGIN + "/": FakePage("<main>Расписание</main>")})
    answer = ls.timetable_answer()
    llm = ScriptedLlm(queue=[answer], discovery_queue=[LlmResponse(content="invalid fixture plan")])
    obs = await adaptive_observe(
        handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"], intent=LearningIntent(), llm=llm
    )
    assert obs.pages
    assert obs.limited
    assert llm.requests == []
    assert (
        await llm.complete(LlmRequest(messages=[LlmMessage(role="user", content="author")]))
        is answer
    )


async def test_provider_without_planner_capability_does_not_consume_author_calls() -> None:
    class AuthorOnly:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: LlmRequest) -> LlmResponse:
            self.calls += 1
            return ls.timetable_answer()

    handle = TrackingHandle({ORIGIN + "/": FakePage("<main>Расписание</main>")})
    llm = AuthorOnly()
    obs = await adaptive_observe(
        handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"], intent=LearningIntent(), llm=llm
    )
    assert obs.pages
    assert llm.calls == 0


async def test_step_budget_is_reported_as_incomplete_instead_of_missing_data() -> None:
    handle = TrackingHandle(
        {
            ORIGIN + "/": FakePage('<main><a href="/courses">Курсы</a></main>'),
            ORIGIN + "/courses": FakePage("<main>Курсы</main>"),
        }
    )
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0")]),
        budget=DiscoveryBudget(steps=1),
    )
    assert obs.limited
    assert obs.stop_reason == "step_budget"


async def test_general_connect_discovers_courses_and_keeps_recipe_confirmation(
    make_env: EnvFactory,
) -> None:
    env = await make_env([])
    endpoint = {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": "/api/v2/courses"},
        "response": {
            "format": "json",
            "itemsPath": "/results",
            "fields": {"externalId": {"path": "/id"}, "title": {"path": "/name", "required": True}},
        },
    }
    body = await ls.fetch(env.app, "/api/v2/courses", new_session_token())
    env.services.browser = FakeBrowserGateway(
        {
            ORIGIN + "/courses": FakePage(
                "<main>Мои курсы</main>",
                requests=[
                    ObservedRequest(
                        method="GET",
                        url=ORIGIN + "/api/v2/courses",
                        status=200,
                        resource_type="xhr",
                        response_content_type="application/json",
                        response_sample=body,
                    )
                ],
            )
        },
        NOW,
        ORIGIN,
    )
    llm = ScriptedLlm(
        queue=[ls.answer([endpoint])],
        discovery_queue=[plan(1, "finish", kinds=["courses"], evidence=["r0"])],
    )
    env.services.llm = llm
    result = await env.run(
        payload={
            "learning_intent": LearningIntent(
                entry_urls=(ORIGIN + "/courses",), discovery_mode="auto"
            ).checkpoint()
        }
    )
    assert result["status"] == "ok"
    assert result["kinds"] == ["courses"]
    assert await env.count(m.Course) > 0
    assert [block.type for block in env.asked] == ["confirm"]
    assert len(llm.requests) == 1


async def test_auth_required_after_restore_forces_relogin_once_and_never_authors_login_page(
    make_env: EnvFactory,
) -> None:
    env = await make_env([])
    env.services.browser = FakeBrowserGateway(
        {
            ORIGIN + "/": FakePage(
                "<main><h2>Вы не вошли в аккаунт</h2><p>Для продолжения войдите.</p></main>"
            )
        },
        NOW,
        ORIGIN,
    )
    result = await env.run(payload={"kinds": ["scheduleEvents"]})
    assert result["detail"] == ErrorCode.SOURCE_AUTH_REQUIRED.value
    assert env.sessions.forced == [False, True]
    assert env.llm.requests == []
    assert env.llm.observation_requests == []


async def test_auto_discovery_excludes_kinds_the_user_says_are_absent() -> None:
    handle = TrackingHandle({ORIGIN + "/": FakePage("<main>Курсы и сроки сдачи</main>")})
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(absent_kinds=("deadlines",), discovery_mode="auto"),
        llm=ScriptedLlm(
            discovery_queue=[plan(1, "finish", kinds=["courses", "deadlines"], evidence=["p0"])]
        ),
    )
    assert obs.discovered_kinds == ("courses",)


async def test_planner_activates_only_server_owned_read_view_and_receives_new_network() -> None:
    class TabHandle(TrackingHandle):
        def __init__(self) -> None:
            super().__init__({ORIGIN + "/": FakePage("<main>Курс</main>")})
            self.activated: list[str] = []

        async def read_views(self) -> list[ReadView]:
            return [] if self.activated else [ReadView("server-tab", "Занятия")]

        async def activate_read_view(self, ref: str) -> PageSnapshot:
            assert ref == "server-tab"
            self.activated.append(ref)
            return PageSnapshot(url=ORIGIN + "/", html="<main>Занятия курса</main>")

        async def network_log(self) -> list[ObservedRequest]:
            return (
                []
                if not self.activated
                else [
                    ObservedRequest(
                        method="GET",
                        url=ORIGIN + "/api/schedule",
                        status=200,
                        resource_type="xhr",
                        response_content_type="application/json",
                        response_sample='{"items":[{"title":"Курс о графах"}]}',
                    )
                ]
            )

    handle = TabHandle()
    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "activate", "t1-0"),
            plan(2, "finish", kinds=["scheduleEvents"], evidence=["r0"]),
        ]
    )
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
    )
    assert handle.activated == ["server-tab"]
    assert handle.visits == [ORIGIN + "/"]
    assert obs.discovered_kinds == ("scheduleEvents",)
    assert any(call.url == "/api/schedule" for call in obs.calls)


def test_auth_stop_cannot_be_hidden_by_selected_evidence_pages() -> None:
    state = ObservationSession(TrackingHandle({}), ORIGIN, Redactor(), DiscoveryBudget())
    state.pages = {
        f"p{i}": PageSnapshot(url=ORIGIN + f"/course/{i}", html="<main>Курс</main>")
        for i in range(6)
    }
    state.pages["p6"] = PageSnapshot(
        url=ORIGIN + "/auth",
        html="<main><h2>Вы не вошли в аккаунт</h2></main>",
        captcha_suspected=True,
    )
    obs = state.bundle(tuple(f"p{i}" for i in range(6)))
    assert len(obs.pages) == 6
    assert obs.auth_required
    assert obs.bot_protection


async def test_changed_response_invalidates_previously_expanded_body() -> None:
    class ChangingHandle(TrackingHandle):
        def __init__(self) -> None:
            super().__init__({ORIGIN + "/": FakePage("<main>Курс</main>")})
            self.body = '{"items":[{"title":"Старая запись"}]}'

        async def network_log(self) -> list[ObservedRequest]:
            return [
                ObservedRequest(
                    method="GET",
                    url=ORIGIN + "/api/data",
                    status=200,
                    resource_type="xhr",
                    response_content_type="application/json",
                    response_sample=self.body,
                )
            ]

    handle = ChangingHandle()
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    decision = state.decision({"stateRef": 1, "action": "inspect", "ref": "r0"})
    await state.apply(decision)
    assert "Старая запись" in state.details["r0"]
    handle.body = '{"items":[{"title":"Новая запись"}]}'
    await state.capture(PageSnapshot(url=ORIGIN + "/", html="<main>Открыта вкладка</main>"))
    assert "r0" not in state.inspected
    assert "r0" not in state.details
    bundle = state.bundle()
    assert "Новая запись" in bundle.calls[0].body
    assert "Старая запись" not in bundle.calls[0].body


async def test_malformed_plan_fallback_shares_remaining_budget_and_keeps_evidence() -> None:
    pages: dict[str, FakePage] = {}
    for i in range(11):
        url = ORIGIN + "/" if i == 0 else ORIGIN + f"/step/{i}"
        title = "Сохранённое доказательство" if i == 8 else f"Раздел {i}"
        pages[url] = FakePage(f'<main><h2>{title}</h2><a href="/step/{i + 1}">Дальше</a></main>')
    handle = TrackingHandle(pages)
    plans = [plan(i + 1, "navigate", f"l{i}", evidence=[f"p{i}"]) for i in range(9)]
    llm = ScriptedLlm(discovery_queue=[*plans, LlmResponse(content="invalid fixture plan")])
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(),
        llm=llm,
        budget=DiscoveryBudget(steps=11, pages=12),
    )
    # Ten adaptive pages leave only two fallback visits, rather than starting another six.
    assert len(handle.visits) == 12
    assert obs.limited
    assert obs.stop_reason == "invalid_plan"
    assert any("Сохранённое доказательство" in page.html for page in obs.pages)


@pytest.mark.parametrize("inspect_after_activation", [False, True])
async def test_expanded_old_page_cannot_replace_new_tab_at_the_same_url(
    inspect_after_activation: bool,
) -> None:
    class SameUrlTab(TrackingHandle):
        def __init__(self) -> None:
            super().__init__(
                {ORIGIN + "/": FakePage("<main><article>Старое содержимое</article></main>")}
            )
            self.activated = False

        async def read_views(self) -> list[ReadView]:
            return [] if self.activated else [ReadView("server-tab", "Занятия")]

        async def activate_read_view(self, ref: str) -> PageSnapshot:
            assert ref == "server-tab"
            self.activated = True
            return PageSnapshot(
                url=ORIGIN + "/", html="<main><article>Новое содержимое</article></main>"
            )

    actions = (
        [plan(1, "activate", "t1-0"), plan(2, "inspect", "p0")]
        if inspect_after_activation
        else [plan(1, "inspect", "p0"), plan(2, "activate", "t1-0")]
    )
    obs = await adaptive_observe(
        SameUrlTab(),
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(),
        llm=ScriptedLlm(discovery_queue=[*actions, plan(3, "finish", evidence=["p0", "p1"])]),
    )
    by_reference = {page.reference: page for page in obs.pages}
    assert "Старое содержимое" in by_reference["p0"].html
    assert "Новое содержимое" in by_reference["p1"].html
    assert "Старое содержимое" not in by_reference["p1"].html


@pytest.mark.parametrize("collision", ["method", "redacted_query"])
async def test_expanded_response_uses_exact_reference_despite_redacted_url_collision(
    collision: str,
) -> None:
    first_url = (
        ORIGIN + "/api/data?token=first-private"
        if collision == "redacted_query"
        else ORIGIN + "/api/data"
    )
    second_url = (
        ORIGIN + "/api/data?token=second-private" if collision == "redacted_query" else first_url
    )
    requests = [
        ObservedRequest(
            method="GET",
            url=first_url,
            status=200,
            resource_type="xhr",
            response_content_type="application/json",
            response_sample='{"items":[{"title":"Первая запись"}]}',
        ),
        ObservedRequest(
            method="POST" if collision == "method" else "GET",
            url=second_url,
            status=200,
            resource_type="xhr",
            response_content_type="application/json",
            response_sample='{"items":[{"title":"Вторая запись"}]}',
        ),
    ]
    state = ObservationSession(
        TrackingHandle({ORIGIN + "/": FakePage("<main>Курс</main>", requests=requests)}),
        ORIGIN,
        Redactor(),
        DiscoveryBudget(),
    )
    await state.visit(ORIGIN + "/")
    await state.apply(state.decision({"stateRef": 1, "action": "inspect", "ref": "r0"}))
    obs = state.bundle(("r0", "r1"))
    by_reference = {call.reference: call for call in obs.calls}
    assert by_reference["r0"].url == by_reference["r1"].url
    assert "Первая запись" in by_reference["r0"].body
    assert "Вторая запись" in by_reference["r1"].body
    assert "Первая запись" not in by_reference["r1"].body


async def test_unchanged_cumulative_network_history_is_not_new_progress() -> None:
    class CumulativeHandle(TrackingHandle):
        def __init__(self) -> None:
            super().__init__({ORIGIN + "/": FakePage("<main>Курс</main>")})
            self.history = [
                ObservedRequest(
                    method="GET",
                    url=ORIGIN + "/api/data",
                    status=200,
                    resource_type="xhr",
                    response_content_type="application/json",
                    response_sample='{"items":[{"title":"Старая запись"}]}',
                )
            ]

        async def network_log(self) -> list[ObservedRequest]:
            return self.history

    handle = CumulativeHandle()
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    handle.history.append(
        handle.history[0].model_copy(
            update={"response_sample": '{"items":[{"title":"Новая запись"}]}'}
        )
    )
    page = PageSnapshot(url=ORIGIN + "/", html="<main>Курс</main>")
    assert await state.capture(page)
    await state.apply(state.decision({"stateRef": 2, "action": "inspect", "ref": "r0"}))
    assert not await state.capture(page)
    assert "r0" in state.inspected
    assert "r0" not in state.delta
    assert "Новая запись" in state.details["r0"]


async def test_deep_entry_can_return_to_known_home_without_a_back_link() -> None:
    home = ORIGIN + "/"
    entry = ORIGIN + "/courses?view=actual"
    schedule = ORIGIN + "/schedule"
    handle = TrackingHandle(
        {
            entry: FakePage("<main>Курсы</main>"),
            home: FakePage('<nav><a href="/schedule">Расписание</a></nav>'),
            schedule: FakePage("<main>Занятия</main>"),
        }
    )
    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "navigate", "l0"),
            plan(2, "navigate", "l1"),
            plan(3, "finish", kinds=["scheduleEvents"], evidence=["p2"]),
        ]
    )
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[entry, home],
        intent=LearningIntent(),
        llm=llm,
    )
    assert handle.visits == [entry, home, schedule]
    assert obs.discovered_kinds == ()  # requested mode does not let the planner broaden scope
    assert not obs.limited
    first = json.loads(llm.observation_requests[0].messages[1].content)
    assert first["observation"]["links"] == [
        {"ref": "l0", "label": "Стартовая страница", "url": "/"}
    ]


@pytest.mark.parametrize("adaptive", [True, False])
async def test_failed_navigation_is_incomplete_instead_of_confirmed_absence(adaptive: bool) -> None:
    class Unavailable(TrackingHandle):
        async def goto(self, url: str) -> PageSnapshot:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "synthetic transport failure")

    handle = Unavailable({})
    if adaptive:
        obs = await adaptive_observe(
            handle,
            ORIGIN,
            Redactor(),
            start_urls=[ORIGIN + "/"],
            intent=LearningIntent(),
            llm=ScriptedLlm(),
        )
    else:
        obs = await observe(handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"])
    assert obs.empty
    assert obs.limited
    assert obs.stop_reason == "navigation_failed"


@pytest.mark.parametrize("adaptive", [True, False])
async def test_failed_network_capture_is_incomplete_even_when_the_planner_finishes(
    adaptive: bool,
) -> None:
    class BrokenNetwork(TrackingHandle):
        async def network_log(self) -> list[ObservedRequest]:
            raise RuntimeError("synthetic capture failure")

    handle = BrokenNetwork({ORIGIN + "/": FakePage("<main>Курсы</main>")})
    if adaptive:
        obs = await adaptive_observe(
            handle,
            ORIGIN,
            Redactor(),
            start_urls=[ORIGIN + "/"],
            intent=LearningIntent(),
            llm=ScriptedLlm(discovery_queue=[plan(1, "finish")]),
        )
    else:
        obs = await observe(handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"])
    assert obs.pages
    assert obs.limited
    assert obs.stop_reason == "network_log_failed"


async def test_injected_seed_address_is_not_offered_to_the_planner() -> None:
    handle = TrackingHandle({ORIGIN + "/": FakePage("<main>Курсы</main>")})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    state.offer_entries(
        ["https://other.test/", ORIGIN + "/?token=private", ORIGIN + "/courses?view=actual"]
    )
    assert list(state.links.values()) == [("Стартовая страница", ORIGIN + "/courses?view=actual")]


async def test_pending_repeat_does_not_erase_the_last_completed_network_evidence() -> None:
    completed = ObservedRequest(
        method="GET",
        url=ORIGIN + "/api/data",
        status=200,
        resource_type="xhr",
        response_content_type="application/json",
        response_sample='{"items":[{"title":"Подтверждённая запись"}]}',
    )

    class PendingRepeat(TrackingHandle):
        pending = False

        async def network_log(self) -> list[ObservedRequest]:
            return [completed] + (
                [completed.model_copy(update={"status": None, "response_sample": None})]
                if self.pending
                else []
            )

    handle = PendingRepeat({ORIGIN + "/": FakePage("<main>Курс</main>")})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    await state.apply(state.decision({"stateRef": 1, "action": "inspect", "ref": "r0"}))
    handle.pending = True
    assert not await state.capture(PageSnapshot(url=ORIGIN + "/", html="<main>Курс</main>"))
    assert "r0" not in state.delta
    assert "Подтверждённая запись" in state.details["r0"]
    assert state.calls["r0"].status == 200
