from __future__ import annotations

import httpx
import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import RetryPolicy, build_client, host_allowed, request_with_retries

FAST = RetryPolicy(attempts=3, backoff_s=(0.0, 0.0), jitter=0.0)


def test_host_allowed() -> None:
    assert host_allowed("https://fakeuni.test/api", ["fakeuni.test"])
    assert not host_allowed("http://fakeuni.test/api", ["fakeuni.test"])
    assert not host_allowed("https://evil.test/", ["fakeuni.test"])
    assert not host_allowed("https://user:pw@fakeuni.test/", ["fakeuni.test"])


async def test_blocks_foreign_host_including_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fakeuni.test":
            return httpx.Response(302, headers={"location": "https://evil.test/steal"})
        return httpx.Response(200)

    async with build_client(
        allow_hosts=["fakeuni.test"], proxy_url=None, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(BotaiError) as exc:
            await request_with_retries(client, "GET", "https://fakeuni.test/a", policy=FAST)
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED


async def test_retries_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200, json={"ok": True})

    async with build_client(
        allow_hosts=["fakeuni.test"], proxy_url=None, transport=httpx.MockTransport(handler)
    ) as client:
        resp = await request_with_retries(client, "GET", "https://fakeuni.test/x", policy=FAST)
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_does_not_retry_401() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    async with build_client(
        allow_hosts=["fakeuni.test"], proxy_url=None, transport=httpx.MockTransport(handler)
    ) as client:
        resp = await request_with_retries(client, "GET", "https://fakeuni.test/x", policy=FAST)
    assert resp.status_code == 401
    assert calls["n"] == 1


async def test_exhausted_429_is_rate_limited() -> None:
    async with build_client(
        allow_hosts=["fakeuni.test"],
        proxy_url=None,
        transport=httpx.MockTransport(lambda r: httpx.Response(429)),
    ) as client:
        with pytest.raises(BotaiError) as exc:
            await request_with_retries(client, "GET", "https://fakeuni.test/x", policy=FAST)
    assert exc.value.code == ErrorCode.SOURCE_RATE_LIMITED
