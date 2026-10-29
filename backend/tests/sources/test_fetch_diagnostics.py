"""Replay diagnostics distinguish auth bindings without retaining request contents."""

from __future__ import annotations

import json

import httpx
import pytest

from botai.sources import fetch
from botai.sources.adapter import SessionExpired
from tests.sources.helpers import make_ctx


@pytest.mark.parametrize("status", [200, 401, 403])
async def test_replay_records_status_and_binding_without_source_content(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def emit(name: str, **facts: object) -> None:
        events.append((name, facts))

    monkeypatch.setattr(fetch, "diagnostic_event", emit)
    marker = "private-fixture-access-never-record"
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=marker)))
    async with fetch.SourceClient(ctx, auth_type="cookie") as client:
        url = f"https://fakeuni.test/private/{marker}?token={marker}"
        if status == 200:
            await client.request("GET", url)
        else:
            with pytest.raises(SessionExpired):
                await client.request("GET", url)
    [(name, facts)] = events
    assert name == "auth.source_response"
    assert facts["status"] == status
    assert facts["auth_type"] == "cookie"
    assert facts["transport"] == "http"
    assert facts["auth_required"] is (status != 200)
    assert facts["outcome"] == ("ok" if status == 200 else "rejected")
    assert facts["query_count"] == 1
    assert marker not in json.dumps(events)


async def test_replay_with_expiry_check_disabled_does_not_claim_lost_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []

    def emit(name: str, **facts: object) -> None:
        events.append(facts)

    monkeypatch.setattr(fetch, "diagnostic_event", emit)
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    async with fetch.SourceClient(ctx) as client:
        await client.request("GET", "https://fakeuni.test/data", check_session=False)
    assert events[0]["status"] == 403
    assert events[0]["auth_required"] is False
