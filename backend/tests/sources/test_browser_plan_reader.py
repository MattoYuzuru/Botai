"""The rendered reader binds dates once and preserves bounded replay failure/cleanup semantics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from botai.browser.read_capabilities import ReadEffectError
from botai.contracts.browser_plan import BrowserPlan, BrowserStep, ReadTarget
from botai.core.browser import FakeBrowserGateway, FakeBrowserHandle, FakePage, PageSnapshot
from botai.core.errors import BotaiError
from botai.core.read_capabilities import ReadActionResult, ReadCapability
from botai.sources.rendered import RenderedPageError, RenderedPageReader
from tests.sources.helpers import NOW, make_ctx

ORIGIN = "https://fakeuni.test"
URL = ORIGIN + "/schedule"
PLAN = BrowserPlan(
    steps=[
        BrowserStep(
            op="set_date",
            target=ReadTarget(selector="#date", tag="input", label="Начало", controls="rows"),
            region=ReadTarget(selector="#rows", tag="section", role="region"),
            value="{today}",
        )
    ]
)


class PlanHandle(FakeBrowserHandle):
    def __init__(self, *, reason: str | None = None) -> None:
        super().__init__(
            {URL: FakePage('<main><article class="lesson">Пара</article></main>')},
            NOW,
            ORIGIN,
            read_only=True,
        )
        self.reason = reason
        self.values: dict[str, str] = {}
        self.items_selector = ""

    async def read_capabilities(self) -> list[ReadCapability]:
        return []

    async def apply_read_capability(
        self, ref: str, *, value_ref: str | None = None
    ) -> ReadActionResult:
        raise ReadEffectError("unsupported")

    async def replay_browser_plan(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]:
        self.values = dict(values)
        self.items_selector = items_selector
        if self.reason:
            raise ReadEffectError(self.reason)
        return [await self.snapshot()]


class PlanGateway(FakeBrowserGateway):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__({}, NOW, ORIGIN)
        self.reason = reason

    async def open(self, **kwargs: Any) -> PlanHandle:
        assert kwargs["read_only"] is True
        assert kwargs["background"] is True
        handle = PlanHandle(reason=self.reason)
        self.handles.append(handle)
        return handle


async def test_reader_binds_date_from_fetch_context_and_forwards_final_leaf_selector() -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    gateway = PlanGateway()
    ctx.browser = gateway
    pages = await RenderedPageReader(ctx).read_many(URL, ".day .lesson", plan=PLAN, values={})
    assert len(pages) == 1
    assert isinstance(gateway.handles[0], PlanHandle)
    assert gateway.handles[0].values["{today}"] == ctx.now.astimezone(ctx.tz).date().isoformat()
    assert gateway.handles[0].items_selector == ".day .lesson"
    assert gateway.handles[0].closed


@pytest.mark.parametrize("reason", ["limit", "no_progress", "target_changed"])
async def test_replay_failure_closes_handle_and_never_returns_partial_pages(reason: str) -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    gateway = PlanGateway(reason)
    ctx.browser = gateway
    with pytest.raises(RenderedPageError) as error:
        await RenderedPageReader(ctx).read_many(URL, ".lesson", plan=PLAN, values={})
    assert error.value.problem == (reason if reason in {"limit", "no_progress"} else "selector")
    assert gateway.handles[0].closed


async def test_old_browser_without_new_capability_cannot_silently_ignore_a_plan() -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    gateway = FakeBrowserGateway({URL: FakePage("<main>Пара</main>")}, NOW, ORIGIN)
    ctx.browser = gateway
    with pytest.raises(BotaiError, match="recipe_rejected"):
        await RenderedPageReader(ctx).read_many(URL, ".lesson", plan=PLAN, values={})
    assert gateway.handles[0].closed
