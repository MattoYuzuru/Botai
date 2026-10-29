"""A replay refusal gets one independent session check, never a forced login by itself."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import httpx
import pytest

from botai.core.jobs import InMemoryJobContext
from botai.learn.author import Candidate
from botai.learn.explorer import Explorer
from botai.learn.lint import LintContext
from botai.learn.revisions import author_digest
from tests.fakeuni import data
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import FAST, session_state


@pytest.mark.parametrize("refreshed_readable", [True, False])
async def test_typed_auth_refusal_checks_entry_once_and_retries_the_exact_binding(
    make_env: EnvFactory, refreshed_readable: bool
) -> None:
    env = await make_env([])
    reads: list[tuple[str, str | None]] = []

    def source(request: httpx.Request) -> httpx.Response:
        reads.append((request.url.path, request.headers.get("cookie")))
        if refreshed_readable and "relogin-1" in request.headers.get("cookie", ""):
            return httpx.Response(200, json={"items": data.timetable()})
        return httpx.Response(403)

    ctx = InMemoryJobContext(user_id=env.seeded.user_id, connection_id=env.seeded.connection_id)
    explorer = Explorer(ctx, env.services, transport=httpx.MockTransport(source), retry=FAST)
    info = await explorer._info(env.seeded.connection_id)
    original = session_state("old-session-fixture")
    sctx = explorer._sync_context(info, original)
    endpoint = recipes.timetable_endpoint()
    endpoint["request"]["authBinding"] = "bCookies"
    candidate = Candidate(
        "scheduleEvents",
        endpoint,
        [],
        {
            "type": "session",
            "version": 1,
            "bindings": {"bCookies": {"cookies": True, "bearer": "none"}},
        },
    )
    lint = LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin)
    checked, feedback = await explorer._verify_candidate(
        "scheduleEvents", candidate, 0, sctx, info, 1, author_digest(info.user_id), lint
    )
    assert env.sessions.calls == 1
    assert env.sessions.forced == [False]
    assert sctx.session is not original
    assert explorer._observed_session is sctx.session
    assert len(reads) == 2
    assert {path for path, _ in reads} == {"/api/v2/timetable"}
    assert ctx.blocks == []  # the denied data request did not itself ask the human to log in
    if refreshed_readable:
        assert checked is not None
        assert checked[0].auth == candidate.auth
        assert checked[1].count("scheduleEvents") == 4
        assert feedback == []
    else:
        assert checked is None
        assert "auth_rejected" in " ".join(feedback)
        await explorer._verify_candidate(
            "scheduleEvents", candidate, 1, sctx, info, 1, author_digest(info.user_id), lint
        )
        assert len(reads) == 3  # the next candidate gets no second session check
        assert env.sessions.calls == 1
        assert env.sessions.forced == [False]
