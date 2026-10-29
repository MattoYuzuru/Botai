"""The rendered reader binds dates once and preserves bounded replay failure/cleanup semantics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from botai.browser.read_capabilities import ReadEffectError
from botai.contracts.browser_plan import BrowserPlan, BrowserStep, ReadTarget
from botai.contracts.specs import RecipeSpec
from botai.core.browser import (
    FakeBrowserGateway,
    FakeBrowserHandle,
    FakePage,
    PageSnapshot,
    SessionBearer,
    SessionState,
)
from botai.core.errors import BotaiError
from botai.core.jobs import InMemoryJobContext
from botai.core.read_capabilities import ReadActionResult, ReadCapability
from botai.sources import runner
from botai.sources.adapter import AdapterResult, SessionExpired
from botai.sources.adapters.recipe import RecipeAdapter
from botai.sources.rendered import RenderedPageError, RenderedPageReader
from tests.sources import recipes
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
            {
                URL: FakePage(
                    '<main><article class="lesson" data-task="task-a" data-ts="1793098800">Пара</article></main>'
                )
            },
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
        self.sessions: list[SessionState | None] = []

    async def open(self, **kwargs: Any) -> PlanHandle:
        assert kwargs["read_only"] is True
        assert kwargs["background"] is True
        self.sessions.append(kwargs.get("session"))
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


def expired_state(mode: str) -> SessionState:
    return SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        entry_url=SecretStr(URL),
        expires_hint=NOW - timedelta(seconds=1) if mode == "global" else None,
        bearer_scopes=[
            SessionBearer(
                origin=ORIGIN,
                token=SecretStr("expired-scoped-fixture"),
                captured_at=NOW,
                expires_hint=NOW - timedelta(seconds=1),
            )
        ],
    )


@pytest.mark.parametrize("mode", ["global", "scope"])
@pytest.mark.parametrize("planned", [False, True])
async def test_expired_credentials_require_renewal_before_any_browser_open(
    mode: str, planned: bool
) -> None:
    ctx = make_ctx(
        session=expired_state(mode), transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    ctx.browser = gateway = PlanGateway()
    reader = RenderedPageReader(ctx)
    read = (
        reader.read_many(URL, ".lesson", plan=PLAN, values={})
        if planned
        else reader.read(URL, ".lesson")
    )
    with pytest.raises(SessionExpired) as error:
        await read
    assert error.value.code == "auth_missing"
    assert gateway.handles == []


def rendered_spec() -> dict[str, Any]:
    endpoint = recipes.deadlines_html_endpoint()
    endpoint["request"].update(path="/schedule", render=True, browserPlan={"version": 1})
    endpoint["response"]["itemsSelector"] = ".lesson"
    endpoint["response"]["fields"]["title"]["selector"] = "."
    endpoint["response"]["fields"]["dueAt"]["selector"] = "."
    spec = recipes.recipe_spec([endpoint, recipes.timetable_endpoint()])
    spec["recipe"]["version"] = 2
    spec["recipe"]["auth"] = {
        "type": "session",
        "version": 1,
        "bindings": {"bMixed": {"cookies": True, "bearer": "observedAuthorization"}},
    }
    spec["recipe"]["endpoints"][1]["request"]["authBinding"] = "bMixed"
    return spec


async def test_scoped_mixed_recipe_renews_expired_render_session_once_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        session=expired_state("global"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )
    ctx.browser = gateway = PlanGateway()
    fresh = SessionState(origin=ORIGIN, captured_at=NOW, entry_url=SecretStr(URL))
    renew = AsyncMock(return_value=fresh)
    services = SimpleNamespace(sessions=SimpleNamespace(ensure=renew))
    snapshot = SimpleNamespace(
        adapter="recipe",
        spec=rendered_spec(),
        data_scope=("deadlines",),
        user_id=ctx.user_id,
        connection_id=ctx.connection_id,
    )
    monkeypatch.setattr(runner, "set_connection", AsyncMock())
    job = InMemoryJobContext(user_id=ctx.user_id, connection_id=ctx.connection_id)
    result = await runner.fetch_with_reauth(job, services, snapshot, ctx)
    assert isinstance(result, AdapterResult)
    assert result.outcomes[0].ok
    assert len(result.records) == 1
    renew.assert_awaited_once_with(
        job, user_id=ctx.user_id, connection_id=ctx.connection_id, force=True
    )
    assert gateway.sessions == [fresh]


async def test_public_render_never_restores_private_credentials_or_rejects_unused_expiry() -> None:
    ctx = make_ctx(
        session=expired_state("global"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )
    ctx.browser = gateway = PlanGateway()
    guard = AsyncMock()
    ctx.before_request = guard
    spec = rendered_spec()
    spec["recipe"]["endpoints"] = spec["recipe"]["endpoints"][:1]
    spec["recipe"]["auth"] = {"type": "none"}
    spec["recipe"]["validation"]["requireKinds"] = ["deadlines"]
    result = await RecipeAdapter(RecipeSpec.model_validate(spec)).fetch(ctx)
    assert result.outcomes[0].ok
    assert gateway.sessions == [None]
    guard.assert_awaited_once()
    assert ctx.session is not None


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
