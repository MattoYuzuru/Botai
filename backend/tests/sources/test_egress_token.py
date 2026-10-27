"""In production the EgressProxy refuses a bare proxy URL (407); every source client must use a
token minted for its connection's allowlist (security.md §4, W2 report)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from botai.sources import fetch
from botai.sources.adapter import SyncContext


async def test_source_client_uses_a_token_for_its_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted: list[list[str]] = []
    built: dict[str, Any] = {}

    async def fake_proxy_url_for(settings: object, hosts: list[str], **_: object) -> str:
        minted.append(hosts)
        return "http://token-for-conn@proxy:3128"

    real_build = fetch.build_client

    def spy_build(**kwargs: Any) -> Any:
        built.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr("botai.browser.egress.proxy_url_for", fake_proxy_url_for)
    monkeypatch.setattr(fetch, "build_client", spy_build)
    ctx = SyncContext(
        user_id="u",
        connection_id="c",
        origin="https://fakeuni.test",
        allow_hosts=["fakeuni.test"],
        now=datetime(2026, 10, 27, tzinfo=UTC),
        vault=None,
        tz="Europe/Moscow",
        session=None,  # type: ignore[arg-type]
        proxy_url="http://proxy:3128",
    )
    async with fetch.SourceClient(ctx, auth_type="none", use_session=False):
        pass
    assert minted == [["fakeuni.test"]]
    assert built["proxy_url"] == "http://token-for-conn@proxy:3128"
