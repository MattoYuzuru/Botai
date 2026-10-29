"""A refused replay is a candidate failure; only reproducible reads become recipes."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from botai.browser.session import decode_session, encode_session
from botai.core.browser import SessionOriginStorage
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn.explorer import Explorer
from botai.learn.handlers import make_handlers
from botai.learn.intent import LearningIntent
from botai.learn.lint import LintContext
from botai.learn.revisions import author_digest
from tests.fakeuni import data
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import FAST, new_session_token, session_state


async def test_executor_checks_available_bearer_binding_and_publishes_only_proven_auth(
    make_env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
    env = await make_env([ls.timetable_answer()], with_spec=True, spec=base)
    original_ensure = env.sessions.ensure

    async def ensure_with_bearer(ctx, *, user_id, connection_id, force=False):
        state = await original_ensure(
            ctx, user_id=user_id, connection_id=connection_id, force=force
        )
        state.bearer = SecretStr("opaque-fixture-bearer")
        await env.vault.put(user_id, connection_id, SecretKind.SESSION, encode_session(state))
        return state

    monkeypatch.setattr(env.sessions, "ensure", ensure_with_bearer)
    modes: list[tuple[bool, bool]] = []

    def source(request: httpx.Request) -> httpx.Response:
        modes.append(("cookie" in request.headers, "authorization" in request.headers))
        if request.headers.get("authorization") != "Bearer opaque-fixture-bearer":
            return httpx.Response(401)
        return httpx.Response(200, json={"items": data.timetable()})

    env.transport = httpx.MockTransport(source)
    env.handlers = make_handlers(transport=env.transport, retry=FAST)
    result = await env.run(payload={"kinds": ["scheduleEvents"], "url": data.ORIGIN + "/app"})

    assert result["status"] == "ok"
    assert (await env.spec(2))["recipe"]["auth"] == {"type": "bearer"}
    assert (await env.spec(1))["recipe"]["auth"] == {"type": "cookie"}
    assert await env.count(m.ScheduleEvent) == 4
    assert (await env.sync())["status"] == "ok"
    assert modes == [(True, False), *([(False, True)] * 4)]
    assert len(env.llm.requests) == 1
    assert env.sessions.calls == 1
    assert "opaque-fixture-bearer" not in "\n".join(
        message.content for request in env.llm.requests for message in request.messages
    )


@pytest.mark.parametrize(
    ("compatible", "include_retained"), [(True, True), (False, True), (True, False)]
)
async def test_assembly_checks_retained_endpoint_auth_without_reading_excluded_kinds(
    make_env: EnvFactory, compatible: bool, include_retained: bool
) -> None:
    env = await make_env([ls.timetable_answer()], with_spec=True)
    base = await env.spec(1)
    state = session_state()
    state.bearer = SecretStr("opaque-fixture-bearer")
    calls: list[tuple[str, str]] = []

    def source(request: httpx.Request) -> httpx.Response:
        auth = "bearer" if "authorization" in request.headers else "cookie"
        calls.append((request.url.path, auth))
        if request.url.path == "/api/v2/tasks/deadlines":
            if auth != ("bearer" if compatible else "cookie"):
                return httpx.Response(401)
            return httpx.Response(200, json=data.deadlines())
        if auth != "bearer":
            return httpx.Response(401)
        return httpx.Response(200, json={"items": data.timetable()})

    ctx = InMemoryJobContext(user_id=env.seeded.user_id)
    explorer = Explorer(ctx, env.services, transport=httpx.MockTransport(source), retry=FAST)
    explorer.intent = LearningIntent(
        requested_kinds=("scheduleEvents", "deadlines") if include_retained else ("scheduleEvents",)
    )
    info = await explorer._info(env.seeded.connection_id)
    sctx = explorer._sync_context(info, state)
    lint = LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin)
    chosen = await explorer._author_and_verify(
        "scheduleEvents",
        "<observation>synthetic read-only source</observation>",
        lint,
        sctx,
        info,
        2,
        author_digest(info.user_id),
    )
    assert chosen is not None
    assert chosen.candidate.auth == {"type": "bearer"}

    assembled = await explorer._assemble(
        info, {"scheduleEvents": chosen}, base, 2, author_digest(info.user_id), sctx, lint
    )

    if compatible and include_retained:
        assert assembled is not None
        spec, report = assembled
        assert spec["recipe"]["auth"] == {"type": "bearer"}
        assert report.count("scheduleEvents") == 4
        assert report.count("deadlines") == 2
    else:
        assert assembled is None
    if not include_retained:
        assert calls == [
            ("/api/v2/timetable", "cookie"),
            ("/api/v2/timetable", "bearer"),
            ("/api/v2/timetable", "cookie"),
        ]
    assert await env.specs() == [(1, "active")]
    assert "Сайт не принял сессию" not in ls.all_text(ctx)


async def test_all_auth_refusals_are_bounded_and_do_not_claim_the_browser_login_expired(
    make_env: EnvFactory,
) -> None:
    env = await make_env([ls.timetable_answer(), ls.timetable_answer()])
    state = session_state()
    state.bearer = SecretStr("opaque-fixture-bearer")
    calls: list[str] = []

    def denied(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(403)

    ctx = InMemoryJobContext(user_id=env.seeded.user_id)
    explorer = Explorer(ctx, env.services, transport=httpx.MockTransport(denied), retry=FAST)
    info = await explorer._info(env.seeded.connection_id)
    selected = await explorer._author_and_verify(
        "scheduleEvents",
        "<observation>browser view is available</observation>",
        LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin),
        explorer._sync_context(info, state),
        info,
        1,
        author_digest(info.user_id),
    )

    assert selected is None
    assert explorer._verification_failed
    assert len(calls) == 4  # two supported bindings, two author rounds
    assert len(env.llm.requests) == 2
    feedback = env.llm.requests[1].messages[1].content
    assert "доступ к данным не воспроизведён" in feedback
    assert "render=true" in feedback
    assert "opaque-fixture-bearer" not in feedback
    assert env.sessions.calls == 0  # HTTP refusal alone must not trigger another MFA/login
    assert not any(block.type in {"input", "login"} for block in ctx.blocks)
    assert "Сайт не принял сессию сразу после входа" not in ls.all_text(ctx)
    assert await env.specs() == []


async def test_canary_failure_reauthors_from_execution_feedback_before_asking_for_a_link(
    make_env: EnvFactory,
) -> None:
    wrong = recipes.timetable_endpoint(path="/api/old-timetable")
    env = await make_env([ls.answer([wrong]), ls.timetable_answer()])

    result = await env.run(payload={"kinds": ["scheduleEvents"]})

    assert result["status"] == "ok"
    assert await env.count(m.ScheduleEvent) == 4
    assert len(env.llm.requests) == 2
    assert "not_found" in env.llm.requests[1].messages[1].content
    assert [block.type for block in env.asked] == ["confirm"]
    stored = await env.spec(1)
    assert stored["recipe"]["endpoints"][0]["request"]["path"] == "/api/v2/timetable"


async def test_updated_browser_state_reaches_canary_vault_and_first_sync(
    make_env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make_env([ls.timetable_answer()])
    refreshed = session_state(new_session_token("opaque-refreshed-cookie"))
    refreshed.bearer = SecretStr("opaque-refreshed-bearer")
    refreshed.browser_storage = [
        SessionOriginStorage(
            origin=data.ORIGIN,
            local_storage={"neutral": SecretStr("opaque-refreshed-local")},
            session_storage={"neutral": SecretStr("opaque-refreshed-transient")},
        )
    ]
    opened = env.services.browser.open

    async def open_with_refresh(**kwargs: Any):
        handle = await opened(**kwargs)

        async def export():
            assert not handle.closed
            return refreshed

        monkeypatch.setattr(handle, "export_session", export)
        return handle

    monkeypatch.setattr(env.services.browser, "open", open_with_refresh)
    used_updated: list[bool] = []

    def source(request: httpx.Request) -> httpx.Response:
        current = "opaque-refreshed-cookie" in request.headers.get("cookie", "")
        used_updated.append(current)
        return (
            httpx.Response(200, json={"items": data.timetable()})
            if current
            else httpx.Response(401)
        )

    env.transport = httpx.MockTransport(source)
    env.handlers = make_handlers(transport=env.transport, retry=FAST)
    result = await env.run(payload={"kinds": ["scheduleEvents"]})

    assert result["status"] == "ok"
    assert used_updated == [True, True, True]  # candidate, assembled recipe, first sync
    saved = await env.vault.get(env.seeded.user_id, env.seeded.connection_id, SecretKind.SESSION)
    assert saved is not None
    assert decode_session(saved.get_secret_value()) == refreshed
    assert (await env.sync())["status"] == "ok"
    assert used_updated == [True] * 4
    assert len(env.llm.requests) == 1
    prompts = "\n".join(
        message.content for request in env.llm.requests for message in request.messages
    )
    for marker in ("cookie", "bearer", "local", "transient"):
        assert f"opaque-refreshed-{marker}" not in prompts + ls.all_text(env.ctx)


async def test_failed_final_session_export_closes_browser_without_authoring_or_leaking_error(
    make_env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make_env([])
    opened = env.services.browser.open

    async def open_with_broken_export(**kwargs: Any):
        handle = await opened(**kwargs)

        async def inventory():
            return ()

        async def export():
            raise RuntimeError("opaque-export-error-with-secret")

        monkeypatch.setattr(handle, "observation_secrets", inventory, raising=False)
        monkeypatch.setattr(handle, "export_session", export)
        return handle

    monkeypatch.setattr(env.services.browser, "open", open_with_broken_export)
    result = await env.run(payload={"kinds": ["scheduleEvents"]})

    assert result == {"status": "error", "detail": "source_unavailable"}
    assert env.llm.requests == []
    assert all(handle.closed for handle in env.services.browser.handles)
    assert "opaque-export-error-with-secret" not in ls.all_text(env.ctx)
    assert await env.specs() == []


async def test_egress_failure_propagates_without_auth_fallback_or_reauthoring(
    make_env: EnvFactory,
) -> None:
    env = await make_env([ls.timetable_answer()])

    def blocked(request: httpx.Request) -> httpx.Response:
        raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Адрес не разрешён")

    explorer = Explorer(
        InMemoryJobContext(user_id=env.seeded.user_id),
        env.services,
        transport=httpx.MockTransport(blocked),
        retry=FAST,
    )
    info = await explorer._info(env.seeded.connection_id)
    state = session_state()
    state.bearer = SecretStr("opaque-fixture-bearer")
    with pytest.raises(BotaiError) as error:
        await explorer._author_and_verify(
            "scheduleEvents",
            "<observation>synthetic read-only source</observation>",
            LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin),
            explorer._sync_context(info, state),
            info,
            1,
            author_digest(info.user_id),
        )
    assert error.value.code == ErrorCode.EGRESS_BLOCKED
    assert len(env.llm.requests) == 1
