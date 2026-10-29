"""A probe repeats one observed read with other dates and nothing else (FR-039)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.core.browser import FakeBrowserHandle, FakePage, ObservedRequest
from botai.core.llm import LlmResponse, ScriptedLlm
from botai.learn.discovery import DiscoveryBudget, ProbeKit, adaptive_observe
from botai.learn.intent import LearningIntent
from botai.learn.probe import (
    ProbeRefused,
    ProbeReply,
    observed_literals,
    probe_url,
    shape_of,
)
from botai.learn.redact import Redactor
from tests.sources.helpers import NOW
from tests.sources.test_date_window import week

ORIGIN = "https://fakeuni.test"
TZ = ZoneInfo("Europe/Moscow")
API = f"{ORIGIN}/api/get-myschedule?begin_date=2026-10-26&view=week"


def call(**changes: Any) -> ObservedRequest:
    return ObservedRequest.model_validate(
        {
            "method": "GET",
            "url": API,
            "status": 200,
            "resource_type": "fetch",
            "auth_proven": True,
            "has_cookie": True,
            **changes,
        }
    )


def url_for(overrides: dict[str, str], observed: ObservedRequest | None = None) -> str:
    observed = observed or call()
    return probe_url(
        observed,
        overrides,
        origin=ORIGIN,
        literals=observed_literals([observed]),
        now=NOW,
        tz=TZ,
    )


def test_a_probe_changes_only_a_value_of_an_observed_query_key() -> None:
    assert url_for({"begin_date": "{plusDays:7}"}) == (
        f"{ORIGIN}/api/get-myschedule?begin_date=2026-11-03&view=week"
    )
    assert url_for({"begin_date": "{minusDays:7}"}).endswith("begin_date=2026-10-20&view=week")
    # A literal is allowed when the observation already showed it.
    assert url_for({"view": "week"}) == API


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"end_date": "{today}"}, "unknown_query_key"),  # a key the request did not have
        ({"begin_date": "2026-12-31"}, "value_not_observed"),  # an invented literal
        ({"begin_date": "{userId}"}, "value_not_observed"),  # not a date template
        ({"access_token": "x"}, "unknown_query_key"),
        ({}, "override_count"),
    ],
)
def test_a_probe_cannot_invent_keys_values_or_credentials(
    overrides: dict[str, str], reason: str
) -> None:
    with pytest.raises(ProbeRefused) as refused:
        url_for(overrides)
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    "observed",
    [
        call(method="POST"),
        call(status=403),
        call(resource_type="document"),
        call(auth_proven=False),
        call(url="https://evil.test/api/get-myschedule?begin_date=2026-10-26"),
    ],
)
def test_only_an_observed_successful_get_of_the_same_origin_is_repeated(
    observed: ObservedRequest,
) -> None:
    with pytest.raises(ProbeRefused):
        url_for({"begin_date": "{today}"}, observed)


def test_shape_counts_the_nested_records_not_only_the_days() -> None:
    sparse = shape_of(json.dumps(week(date(2026, 10, 26))))
    busy = shape_of(json.dumps(week(date(2026, 11, 2))))
    assert (sparse.top_level, sparse.records) == (7, 1)
    assert (busy.top_level, busy.records) == (7, 5)
    assert shape_of(json.dumps({"items": [{"a": 1}, {"a": 2}]})).levels == (2,)
    assert shape_of("[]").levels == (0,)
    assert shape_of("not json").levels == ()
    assert shape_of("<ul><li>a</li><li>b</li></ul>").levels == (2,)


# --- the planner action ----------------------------------------------------------------------


def plan(revision: int, action: str, ref: str | None = None, **extra: Any) -> LlmResponse:
    body = {
        "stateRef": revision,
        "action": action,
        "ref": ref,
        "discoveries": extra.pop("kinds", []),
        "evidenceRefs": extra.pop("evidence", []),
        **extra,
    }
    return LlmResponse(content=json.dumps(body))


def shell(sample: str) -> FakeBrowserHandle:
    request = ObservedRequest(
        method="GET",
        url=API,
        status=200,
        resource_type="fetch",
        response_content_type="application/json",
        response_sample=sample,
        auth_proven=True,
        has_cookie=True,
    )
    pages = {f"{ORIGIN}/": FakePage("<main>Расписание</main>", "Кабинет", requests=[request])}
    return FakeBrowserHandle(pages, NOW, ORIGIN, read_only=True)


async def test_probe_adds_the_neighbour_week_as_new_evidence_with_its_record_count() -> None:
    asked: list[str] = []

    async def prober(observed: ObservedRequest, url: str) -> ProbeReply | None:
        asked.append(url)
        begin = date.fromisoformat(url.split("begin_date=")[1].split("&")[0])
        body = json.dumps(week(begin))
        return ProbeReply(200, body, "application/json", shape_of(body))

    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "probe", "r0", overrides={"begin_date": "{plusDays:7}"}),
            plan(2, "finish", kinds=["scheduleEvents"], evidence=["r0", "r1"]),
        ]
    )
    handle = shell(json.dumps(week(date(2026, 10, 26))))
    obs = await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[f"{ORIGIN}/"],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
        probe_kit=ProbeKit(prober, NOW, TZ),
    )
    assert asked == [f"{ORIGIN}/api/get-myschedule?begin_date=2026-11-03&view=week"]
    second = json.loads(llm.observation_requests[1].messages[1].content)["observation"]
    assert second["probes"] == [
        {
            "ref": "r1",
            "from": "r0",
            "overrides": {"begin_date": "{plusDays:7}"},
            "status": 200,
            "objectsPerLevel": [7, 4],
        }
    ]
    assert "r1" in second["newEvidenceRefs"]
    assert {call.reference for call in obs.calls} >= {"r0", "r1"}
    assert obs.request_facts["r1"].url.endswith("begin_date=2026-11-03&view=week")
    assert obs.request_facts[
        "r1"
    ].has_cookie  # the probe keeps the credentials of the original read


async def test_a_refused_or_failed_probe_is_no_progress_not_a_crash() -> None:
    calls: list[str] = []

    async def prober(observed: ObservedRequest, url: str) -> ProbeReply | None:
        calls.append(url)
        return None

    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "probe", "r0", overrides={"unknown": "{today}"}),  # refused: no such key
            plan(2, "probe", "r0", overrides={"begin_date": "{plusDays:7}"}),  # request failed
            plan(3, "finish", kinds=["scheduleEvents"], evidence=["r0"]),
        ]
    )
    obs = await adaptive_observe(
        shell("[]"),
        ORIGIN,
        Redactor(),
        start_urls=[f"{ORIGIN}/"],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
        probe_kit=ProbeKit(prober, NOW, TZ),
    )
    assert len(calls) == 1  # the refused probe never reached the reader
    assert obs.limited  # a failed read is an evidence gap, not proof of absence


async def test_probe_budget_and_plan_validation() -> None:
    async def prober(observed: ObservedRequest, url: str) -> ProbeReply | None:
        return ProbeReply(200, "[]", "application/json", shape_of("[]"))

    llm = ScriptedLlm(
        discovery_queue=[
            plan(1, "probe", "r0", overrides={"begin_date": "{plusDays:7}"}),
            plan(2, "probe", "r0", overrides={"begin_date": "{plusDays:14}"}),
            plan(3, "finish", evidence=["r0"]),
        ]
    )
    handle = shell("[]")
    await adaptive_observe(
        handle,
        ORIGIN,
        Redactor(),
        start_urls=[f"{ORIGIN}/"],
        intent=LearningIntent(discovery_mode="auto"),
        llm=llm,
        budget=DiscoveryBudget(probes=1),
        probe_kit=ProbeKit(prober, NOW, TZ),
    )
    third = json.loads(llm.observation_requests[2].messages[1].content)["observation"]
    assert len(third["probes"]) == 1  # the second one was over the budget


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "probe", "ref": "r0"},  # no overrides
        {"action": "probe", "ref": "p0", "overrides": {"a": "b"}},  # a page, not a response
        {"action": "probe", "ref": "r0", "overrides": {"a": 1}},  # not text
        {"action": "inspect", "ref": "r0", "overrides": {"a": "b"}},  # overrides elsewhere
    ],
)
async def test_the_plan_must_be_well_formed(decision: dict[str, Any]) -> None:
    from botai.learn.discovery import ObservationSession

    handle = shell("[]")
    snap = await handle.goto(f"{ORIGIN}/")
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.capture(snap)
    with pytest.raises(ValueError, match=r"probe|overrides|reference"):
        state.decision(
            {
                "stateRef": state.revision,
                "discoveries": [],
                "evidenceRefs": [],
                **decision,
            }
        )


def test_week_helper_is_a_sliding_window() -> None:
    assert week(date(2026, 11, 2))[0]["date"] == "2026-11-02"
    assert (date(2026, 11, 2) + timedelta(days=6)).isoformat() == week(date(2026, 11, 2))[-1][
        "date"
    ]
