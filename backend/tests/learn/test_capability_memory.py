"""Planner refs cannot smuggle selectors; only compiled, secret-free plans become durable."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from botai.contracts.browser_plan import BrowserPlan, BrowserStep, ReadTarget
from botai.core.browser import FakeBrowserHandle, FakePage, PageSnapshot
from botai.core.read_capabilities import ReadActionResult, ReadCapability, ReadValue
from botai.learn.discovery import DiscoveryBudget, ObservationSession, _sanitize_memory_addresses
from botai.learn.memory import DiscoveryMemory, PortableBrowserPlan
from botai.learn.redact import Redactor
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
URL = ORIGIN + "/schedule"
REGION = ReadTarget(selector="#rows", tag="section", role="region", label="Результаты")
PLAN = BrowserPlan(
    steps=[
        BrowserStep(
            op="set_date",
            target=ReadTarget(selector="#date", tag="input", label="Начало", controls="rows"),
            region=REGION,
            value="{weekStart}",
        )
    ]
)


class CapabilityHandle(FakeBrowserHandle):
    def __init__(self) -> None:
        super().__init__(
            {URL: FakePage('<section id="rows"><article>Пара</article></section>')}, NOW, ORIGIN
        )
        self.applied: list[tuple[str, str | None]] = []

    async def read_capabilities(self) -> list[ReadCapability]:
        return [
            ReadCapability(
                "server-current", "set_date", "Начало", (ReadValue("weekStart", "{weekStart}"),)
            )
        ]

    async def apply_read_capability(
        self, ref: str, *, value_ref: str | None = None
    ) -> ReadActionResult:
        self.applied.append((ref, value_ref))
        return ReadActionResult(await self.snapshot(), PLAN.model_copy(deep=True), URL)

    async def replay_browser_plan(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]:
        return [await self.snapshot()]


async def test_planner_chooses_current_capability_and_finite_value_then_gets_portable_plan() -> (
    None
):
    handle = CapabilityHandle()
    state = ObservationSession(handle, ORIGIN, Redactor(), DiscoveryBudget())
    await state.visit(URL)
    summary = state.summary()
    assert summary["capabilities"][0]["ref"] == "c1-0"
    assert "selector" not in json.dumps(summary["capabilities"])
    with pytest.raises(ValueError, match="unknown capability value"):
        state.decision({"stateRef": 1, "action": "use", "ref": "c1-0", "valueRef": "2026-12-31"})
    with pytest.raises(ValueError, match="invalid observation plan fields"):
        state.decision(
            {
                "stateRef": 1,
                "action": "use",
                "ref": "c1-0",
                "valueRef": "weekStart",
                "selector": "#date",
            }
        )
    await state.apply(
        state.decision({"stateRef": 1, "action": "use", "ref": "c1-0", "valueRef": "weekStart"})
    )
    assert handle.applied == [("server-current", "weekStart")]
    obs = state.bundle(())
    assert obs.browser_plans == {"bp0": PLAN}
    assert obs.browser_plan_urls == {"bp0": URL}
    assert obs.browser_plan_results == {"bp0": URL}
    rendered = obs.render()
    assert "[browserPlan] bp0" in rendered
    assert "#date" not in rendered
    with pytest.raises(ValueError, match="stale"):
        state.decision({"stateRef": 1, "action": "use", "ref": "c1-0", "valueRef": "weekStart"})


def test_portable_memory_roundtrip_has_descriptors_but_no_old_runtime_references() -> None:
    portable = PortableBrowserPlan(start_url=URL, result_url=URL, plan=PLAN)
    memory = DiscoveryMemory(origin=ORIGIN, intent_key="a" * 64, browser_plans={"bp0": portable})
    saved = memory.model_dump_json()
    assert "#date" in saved
    assert "server-current" not in saved
    assert "c1-0" not in saved
    restored = DiscoveryMemory.model_validate_json(saved)
    restored.validate_for(ORIGIN, "a" * 64)
    assert restored.browser_plans["bp0"].plan == PLAN


def test_rotated_secret_inventory_discards_whole_plan_instead_of_redacting_replay_target() -> None:
    marker = "rotated-secret-FIXTURE"
    plan = PLAN.model_copy(deep=True)
    plan.steps[0].target.selector = "#" + marker
    memory = DiscoveryMemory(
        origin=ORIGIN,
        browser_plans={"bp0": PortableBrowserPlan(start_url=URL, result_url=URL, plan=plan)},
    )
    redactor = Redactor()
    redactor.protect_values([marker])
    assert _sanitize_memory_addresses(memory, redactor)
    assert memory.browser_plans == {}


def test_portable_plan_cannot_rebind_to_another_origin() -> None:
    with pytest.raises(ValueError, match="another source"):
        DiscoveryMemory(
            origin=ORIGIN,
            browser_plans={
                "bp0": PortableBrowserPlan(
                    start_url=URL, result_url="https://other.invalid/page", plan=PLAN
                )
            },
        )
