"""Observed API refusals and outages cannot be reported as confirmed missing data."""

from __future__ import annotations

import pytest

from botai.core.browser import FakePage, ObservedRequest, PageSnapshot
from botai.core.llm import ScriptedLlm
from botai.learn.discovery import DiscoveryBudget, ObservationSession, adaptive_observe
from botai.learn.intent import LearningIntent
from botai.learn.observe import data_response_health, observe
from botai.learn.redact import Redactor
from tests.learn.test_discovery import ORIGIN, TrackingHandle, plan


def response(status: int | None) -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=ORIGIN + "/api/data",
        status=status,
        resource_type="fetch",
        response_content_type="application/json",
        response_sample='{"error":"synthetic"}'
        if status != 200
        else '{"items":[{"title":"Данные"}]}',
    )


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("status", [401, 403, 408, 429, 503, 404])
async def test_data_response_health_is_preserved_by_both_observation_paths(
    adaptive: bool,
    status: int,
) -> None:
    handle = TrackingHandle(
        {
            ORIGIN + "/": FakePage(
                "<main><h1>Мои курсы</h1>Кабинет открыт</main>",
                requests=[response(status)],
            )
        }
    )
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
    facts = (
        obs.auth_required,
        obs.limited,
        tuple(call.status for call in obs.calls),
        obs.stop_reason,
    )
    expected_reason = (
        "authentication"
        if status == 401
        else "data_request_failed"
        if status != 404
        else "finished"
        if adaptive
        else ""
    )
    assert facts == (status == 401, status not in {401, 404}, (status,), expected_reason)


@pytest.mark.parametrize("initial_status", [401, 503])
async def test_pending_repeat_does_not_recover_or_progress_but_completed_success_does(
    initial_status: int,
) -> None:
    history = [response(initial_status)]

    class Changing(TrackingHandle):
        async def network_log(self) -> list[ObservedRequest]:
            return history

    handle = Changing({ORIGIN + "/": FakePage("<main>Кабинет открыт</main>")})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    page = PageSnapshot(url=ORIGIN + "/", html="<main>Кабинет открыт</main>")
    history.append(response(None))
    pending_progress = await state.capture(page)
    pending_health = (state.auth_required, "data_request_failed" in state.failures)
    assert not pending_progress
    assert pending_health == (initial_status == 401, initial_status == 503)
    assert "r0" not in state.delta
    history.append(response(200))
    recovered_progress = await state.capture(page)
    recovered_health = (state.auth_required, "data_request_failed" in state.failures)
    assert recovered_progress
    assert recovered_health == (False, False)
    assert state.calls["r0"].status == 200
    assert "r0" in state.delta


async def test_failed_response_is_not_new_data_progress_and_latest_error_replaces_old_success() -> (
    None
):
    history = [response(200)]

    class Changing(TrackingHandle):
        async def network_log(self) -> list[ObservedRequest]:
            return history

    handle = Changing({ORIGIN + "/": FakePage("<main>Кабинет открыт</main>")})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    history.append(response(503))
    progressed = await state.capture(
        PageSnapshot(url=ORIGIN + "/", html="<main>Кабинет открыт</main>")
    )
    assert not progressed
    assert "r0" not in state.delta
    assert "data_request_failed" in state.failures
    obs = await observe(handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"])
    facts = (obs.auth_required, obs.limited, tuple(call.status for call in obs.calls))
    assert facts == (False, True, (503,))


def test_unrelated_static_or_foreign_requests_do_not_change_the_source_data_health() -> None:
    entries = [
        response(503).model_copy(update={"url": "https://other.test/api/data"}),
        response(503).model_copy(update={"url": ORIGIN + "/app.js"}),
        response(503).model_copy(update={"method": "POST"}),
        response(404),
    ]
    assert data_response_health(entries, ORIGIN) == (False, False)


async def test_legacy_observer_stops_browsing_after_a_data_request_requires_login() -> None:
    handle = TrackingHandle(
        {
            ORIGIN + "/": FakePage(
                '<main><a href="/next">Курсы</a></main>', requests=[response(401)]
            ),
            ORIGIN + "/next": FakePage("<main>Курсы</main>"),
        }
    )
    obs = await observe(handle, ORIGIN, Redactor(), start_urls=[ORIGIN + "/"])
    auth = obs.auth_required
    assert auth
    assert handle.visits == [ORIGIN + "/"]


@pytest.mark.parametrize("error_status", [401, 503])
@pytest.mark.parametrize("evidence", [(), ("r0", "r1"), ("r1", "r0")])
async def test_query_reordering_and_evidence_order_cannot_replay_a_stale_error_after_recovery(
    error_status: int,
    evidence: tuple[str, ...],
) -> None:
    original = response(200).model_copy(update={"url": ORIGIN + "/api/data?a=1&b=2"})
    other = response(200).model_copy(update={"url": ORIGIN + "/api/other"})
    outage = response(error_status).model_copy(update={"url": ORIGIN + "/api/data?b=2&a=1"})
    recovered = original.model_copy(
        update={"response_sample": '{"items":[{"title":"Восстановлено"}]}'}
    )
    history = [original, other]

    class Changing(TrackingHandle):
        async def network_log(self) -> list[ObservedRequest]:
            return history

    handle = Changing({ORIGIN + "/": FakePage("<main>Кабинет открыт</main>")})
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(ORIGIN + "/")
    page = PageSnapshot(url=ORIGIN + "/", html="<main>Кабинет открыт</main>")
    history.append(outage)
    await state.capture(page)
    await state.apply(
        state.decision({"stateRef": state.revision, "action": "inspect", "ref": "r0"})
    )
    history.append(outage.model_copy(update={"status": None, "response_sample": None}))
    pending_progress = await state.capture(page)
    pending_status = state.calls["r0"].status
    assert not pending_progress
    assert pending_status == error_status
    history.append(recovered)
    recovered_progress = await state.capture(page)
    assert recovered_progress
    assert set(state.calls) == {"r0", "r1"}
    assert "r0" not in state.details
    obs = state.bundle(evidence)
    facts = (
        obs.auth_required,
        obs.limited,
        tuple(sorted((call.reference, call.status) for call in obs.calls)),
    )
    assert facts == (False, False, (("r0", 200), ("r1", 200)))
    data_call = next(call for call in obs.calls if call.reference == "r0")
    fresh_body = "Восстановлено" in data_call.body
    assert fresh_body
    assert data_response_health(history, ORIGIN) == (False, False)
