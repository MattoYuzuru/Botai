"""The single httpx client factory of the v4 runtime (guard: tests/core/test_architecture.py).

The free harness has no egress allowlist (D-005), so this module does not guard hosts. It exists
so timeouts, redirect policy and transports are configured in one place: providers and the
`http_request` tool create clients only through `build_client`. Mock transports in tests are
passed in here and never reach the network.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

# A model call may be slow; a tool request is bounded by the tool spec too.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0)
USER_AGENT = "BotAI/0.4 (+https://botai.mnema.app)"


def build_client(
    *,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
    max_redirects: int = 10,
    headers: Mapping[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build an async client. `transport` is for tests only; production passes nothing."""
    merged = {"user-agent": USER_AGENT, **dict(headers or {})}
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        headers=merged,
        cookies=cookies,
        transport=transport,
    )


__all__ = ["DEFAULT_TIMEOUT", "USER_AGENT", "build_client"]
