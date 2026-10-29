"""A refused replay is a candidate failure; only reproducible reads become recipes."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from botai.browser.session import decode_session
from botai.core.browser import SessionOriginStorage
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn.handlers import make_handlers
from tests.fakeuni import data
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import FAST, new_session_token, session_state


async def test_canary_failure_reauthors_from_execution_feedback_before_asking_for_a_link(
    make_env: EnvFactory,
) -> None:
    wrong = recipes.timetable_endpoint()
    wrong["response"]["itemsPath"] = "/old-items"
    env = await make_env([ls.answer([wrong]), ls.timetable_answer()])

    result = await env.run(payload={"kinds": ["scheduleEvents"]})

    assert result["status"] == "ok"
    assert await env.count(m.ScheduleEvent) == 4
    assert len(env.llm.requests) == 2
    assert "response_format" in env.llm.requests[1].messages[1].content
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
