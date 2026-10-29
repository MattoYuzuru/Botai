"""A proven full replacement does not inherit the retired recipe's auth or dependencies."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import httpx
import pytest
from pydantic import SecretStr

from botai.contracts.specs import Kind
from botai.core.browser import SessionBearer
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext
from botai.learn.author import Candidate, candidate_spec
from botai.learn.canary import run_canary
from botai.learn.explorer import Chosen, Explorer
from botai.learn.intent import LearningIntent
from botai.learn.lint import LintContext, lint_spec
from botai.learn.revisions import author_digest
from tests.fakeuni import data
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import FAST, NOW, session_state


@pytest.mark.parametrize("retain_old_kind", [False, True])
async def test_assembly_replaces_retired_custom_auth_and_bootstrap_only_when_nothing_survives(
    make_env: EnvFactory, retain_old_kind: bool
) -> None:
    old_endpoint = recipes.timetable_endpoint()
    old_endpoint["request"]["query"] = {"account": "{accountId}"}
    base = recipes.recipe_spec(
        [old_endpoint, *([recipes.deadlines_endpoint()] if retain_old_kind else [])],
        require=("scheduleEvents", "deadlines") if retain_old_kind else ("scheduleEvents",),
        auth={"type": "bearer", "headerName": "X-Api-Token"},
    )
    base["recipe"]["bootstrap"] = [
        {
            "id": "oldAccount",
            "method": "GET",
            "path": "/api/retired-context",
            "extract": {"accountId": "/id"},
        }
    ]
    env = await make_env([], with_spec=True, spec=base)
    calls: list[str] = []

    def source(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/api/v2/timetable", "retired preparation must not execute"
        assert fake_cookie_present(request)
        assert request.headers.get("authorization") == "Bearer replacement-fixture"
        assert "x-api-token" not in request.headers
        return httpx.Response(200, json={"items": data.timetable()})

    explorer = Explorer(
        InMemoryJobContext(user_id=env.seeded.user_id),
        env.services,
        transport=httpx.MockTransport(source),
        retry=FAST,
    )
    explorer.intent = LearningIntent(requested_kinds=("scheduleEvents",))
    info = await explorer._info(env.seeded.connection_id)
    lint = LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin)
    assert lint_spec(base, lint).ok
    state = session_state()
    state.bearer_scopes = [
        SessionBearer(origin=data.ORIGIN, token=SecretStr("replacement-fixture"), captured_at=NOW)
    ]
    sctx = explorer._sync_context(info, state)
    fresh = recipes.timetable_endpoint()
    fresh["request"]["authBinding"] = "proven"
    candidate = Candidate(
        "scheduleEvents",
        fresh,
        [],
        {
            "type": "session",
            "version": 1,
            "bindings": {"proven": {"cookies": True, "bearer": "observedAuthorization"}},
        },
    )
    digest = author_digest(info.user_id)
    proposed = candidate_spec(
        candidate, origin=info.origin, revision=2, now=NOW, digest=digest, index=0
    )
    assert lint_spec(proposed, lint).ok
    proof = await run_canary(proposed, sctx, require={"scheduleEvents"})
    assert proof.ok
    chosen: dict[Kind, Chosen] = {"scheduleEvents": Chosen("scheduleEvents", candidate, proof)}
    if retain_old_kind:
        with pytest.raises(BotaiError) as error:
            await explorer._assemble(info, chosen, base, 2, digest, sctx, lint)
        assert error.value.code == ErrorCode.RECIPE_REJECTED
        assert calls == ["/api/v2/timetable"]
    else:
        assembled = await explorer._assemble(info, chosen, base, 2, digest, sctx, lint)
        assert assembled is not None
        spec, report = assembled
        assert report.ok
        assert report.count("scheduleEvents") == 4
        assert spec["recipe"].get("bootstrap", []) == []
        assert spec["recipe"]["auth"] == {
            "type": "session",
            "version": 1,
            "bindings": {"bCookiesBearer": {"cookies": True, "bearer": "observedAuthorization"}},
        }
        assert spec["recipe"]["endpoints"][0]["request"]["authBinding"] == "bCookiesBearer"
        assert calls == ["/api/v2/timetable"] * 2
    assert env.llm.requests == []
    assert await env.specs() == [(1, "active")]


def fake_cookie_present(request: httpx.Request) -> bool:
    return "FAKEUNI_SESSION=" in request.headers.get("cookie", "")
