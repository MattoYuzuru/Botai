"""The asset allowance of the EgressProxy (security.md §4.4). No Chromium: raw `ARM` and CONNECT.

The proxy cannot see what a request is, so it never decides what an asset is. The browser guard
does, and *arms* a host; the proxy then spends one of the token's asset slots and keeps what only it
can keep: public addresses only, DNS pinning, port 443, a cap of hosts. A host nobody armed (the
target of a server redirect) stays refused; tokens of API clients carry no allowance at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import time
from urllib.parse import unquote, urlsplit

import pytest

from botai.browser import egress as egress_module
from botai.browser.egress import ProxyEgress, proxy_url_for
from botai.browser.proxy import (
    ASSET_HOSTS,
    MAX_ASSET_HOSTS,
    EgressProxy,
    derive_key,
    mint_token,
    verify_token,
)
from botai.settings import Settings
from tests.browser.test_proxy import (  # noqa: F401 - the `lab` fixture
    PUBLIC_A,
    PUBLIC_B,
    SECRET,
    Lab,
    lab,
    send,
    status_of,
)


async def arm(proxy: EgressProxy, host: str, token: str | None, *, port: int = 443) -> int:
    status, _, _, writer = await send(proxy, f"ARM {host}:{port} HTTP/1.1", token)
    writer.close()
    return status


# ------------------------------------------------------------------------------ the allowance


def test_the_allowance_is_part_of_the_signed_grant_and_is_bounded() -> None:
    grant = verify_token(SECRET, mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60))
    assert grant is not None
    assert grant.max_asset_hosts == 0  # nothing by default
    token = mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60, max_asset_hosts=ASSET_HOSTS)
    grant = verify_token(SECRET, token)
    assert grant is not None
    assert grant.max_asset_hosts == ASSET_HOSTS == 16
    for bad in (-1, MAX_ASSET_HOSTS + 1, 500):
        with pytest.raises(ValueError, match="out of range"):
            mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60, max_asset_hosts=bad)
    # the number cannot be raised after the fact: the token is signed
    body, _, signature = token.partition(".")
    forged = base64.urlsafe_b64encode(
        base64.urlsafe_b64decode(body + "==").replace(b'"a":16', b'"a":32')
    ).rstrip(b"=")
    assert verify_token(SECRET, f"{forged.decode()}.{signature}") is None


def test_a_token_from_before_the_allowance_existed_still_verifies() -> None:
    """A rolling deploy: the worker may mint a token the proxy of the other version reads."""
    import json

    from botai.browser.proxy import _b64, _sign  # pyright: ignore[reportPrivateUsage]

    payload = {"v": 1, "j": "abc", "x": int(time.time()) + 60, "h": ["a.test"], "s": [], "m": 5}
    payload["d"] = 1
    body = _b64(json.dumps(payload).encode())
    grant = verify_token(SECRET, f"{body}.{_sign(SECRET, body)}")
    assert grant is not None
    assert grant.max_asset_hosts == 0


async def test_armed_hosts_are_tunnelled_up_to_the_cap_and_no_further(lab: Lab) -> None:  # noqa: F811
    hosts = ["cdn-a.test", "cdn-b.test", "cdn-c.test"]
    for host in hosts:
        lab.answers[host] = [[PUBLIC_B]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=2)
    assert await arm(lab.proxy, "cdn-a.test", token) == 204
    assert await arm(lab.proxy, "cdn-a.test", token) == 204  # already armed: costs nothing
    assert await arm(lab.proxy, "cdn-b.test", token) == 204
    assert await arm(lab.proxy, "cdn-c.test", token) == 403  # the third host: capped
    assert lab.proxy.stats["deny:too_many_hosts"] == 1
    assert await status_of(lab.proxy, "cdn-a.test:443", token) == 200
    assert await status_of(lab.proxy, "cdn-b.test:443", token) == 200
    assert await status_of(lab.proxy, "cdn-c.test:443", token) == 403  # never armed
    assert lab.proxy.stats["deny:not_allowlisted"] == 1
    assert lab.connected == [(PUBLIC_B, 443), (PUBLIC_B, 443)]


async def test_a_host_nobody_armed_stays_refused_even_with_an_allowance(lab: Lab) -> None:  # noqa: F811
    """The target of a server redirect: the browser guard never saw it, so it never armed it."""
    lab.answers["passport.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    assert await status_of(lab.proxy, "passport.test:443", token) == 403
    assert lab.resolved == []  # refused before any DNS lookup
    assert lab.proxy.stats["deny:not_allowlisted"] == 1


@pytest.mark.parametrize(
    ("name", "address", "reason"),
    [
        ("internal.test", "10.0.0.5", "private_address"),
        ("loopback.test", "127.0.0.1", "private_address"),
        ("metadata.test", "169.254.169.254", "private_address"),
        ("cgnat.test", "100.64.0.9", "private_address"),
        ("six.test", "fe80::1", "private_address"),
    ],
)
async def test_an_armed_host_that_resolves_to_a_private_address_is_still_refused(
    lab: Lab,  # noqa: F811
    name: str,
    address: str,
    reason: str,
) -> None:
    lab.answers[name] = [[address]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    assert await arm(lab.proxy, name, token) == 204
    assert await status_of(lab.proxy, f"{name}:443", token) == 403
    assert lab.proxy.stats[f"deny:{reason}"] == 1
    assert lab.connected == []


async def test_a_mixed_answer_with_one_private_address_is_refused(lab: Lab) -> None:  # noqa: F811
    lab.answers["split.test"] = [[PUBLIC_A, "10.1.2.3"]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    assert await arm(lab.proxy, "split.test", token) == 204
    assert await status_of(lab.proxy, "split.test:443", token) == 403
    assert lab.connected == []


async def test_an_armed_host_is_pinned_to_one_resolution(lab: Lab) -> None:  # noqa: F811
    """DNS rebinding: the name answers a public address, then a private one. One lookup only."""
    lab.answers["rebind.test"] = [[PUBLIC_A], ["127.0.0.1"]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    assert await arm(lab.proxy, "rebind.test", token) == 204
    assert await status_of(lab.proxy, "rebind.test:443", token) == 200
    assert lab.connected == [(PUBLIC_A, 443)]
    assert lab.resolved == ["rebind.test"]


# --------------------------------------------------------- tokens that must not carry assets


async def test_a_token_without_an_allowance_cannot_arm_anything(lab: Lab) -> None:  # noqa: F811
    """What `proxy_url_for` (API fetch, probes, search, speech) hands out: ARM is refused."""
    lab.answers["cdn.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"])
    assert await arm(lab.proxy, "cdn.test", token) == 403
    assert lab.proxy.stats["deny:asset_not_allowed"] == 1
    assert await status_of(lab.proxy, "cdn.test:443", token) == 403


async def test_api_fetch_tokens_carry_no_asset_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOTAI_PROXY_SECRET", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[arg-type]
    url = await proxy_url_for(settings, ["fakeuni.test"])
    token = unquote(urlsplit(url).password or "")
    key = egress_module.get_egress(settings)._secret  # pyright: ignore[reportPrivateUsage]
    grant = verify_token(key, token)
    assert grant is not None
    assert grant.max_asset_hosts == 0
    assert "assets" not in inspect.signature(proxy_url_for).parameters
    default = inspect.signature(ProxyEgress.credentials).parameters["assets"].default
    assert default == 0  # only the browser gateway asks for more
    await egress_module.close_egress()


# ----------------------------------------------------------------------- who may arm, and what


async def test_arm_needs_a_genuine_token_and_a_plain_public_name(lab: Lab) -> None:  # noqa: F811
    good = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    tampered = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    expired = mint_token(
        SECRET, hosts=["fakeuni.test"], ttl_s=10, max_asset_hosts=4, now=time.time() - 3600
    )
    foreign = mint_token(
        derive_key(b"another-master-secret-0123456789", "x"),
        hosts=["a.test"],
        ttl_s=60,
        max_asset_hosts=4,
    )
    for token in (None, "garbage", tampered, expired, foreign):
        assert await arm(lab.proxy, "cdn.test", token) == 407
    assert await arm(lab.proxy, "127.0.0.1", good) == 403  # an IP literal is no asset host
    assert lab.proxy.stats["deny:ip_literal"] == 1
    assert await arm(lab.proxy, "0x7f.1", good) == 403
    assert await arm(lab.proxy, "cdn.test", good, port=80) == 403
    assert await arm(lab.proxy, "cdn.test", good, port=8443) == 403
    status, _, _, writer = await send(lab.proxy, "ARM bad_host:443 HTTP/1.1", good)
    writer.close()
    assert status == 400


async def test_arming_is_per_token_and_a_revoked_token_cannot_arm(lab: Lab) -> None:  # noqa: F811
    lab.answers["cdn.test"] = [[PUBLIC_A]]
    first = lab.token(["fakeuni.test"], max_asset_hosts=2)
    second = lab.token(["fakeuni.test"], max_asset_hosts=2)
    assert await arm(lab.proxy, "cdn.test", first) == 204
    assert await status_of(lab.proxy, "cdn.test:443", second) == 403  # the other token's slot
    lab.proxy.revoke(second)
    assert await arm(lab.proxy, "cdn.test", second) == 407


async def test_a_host_the_token_already_allows_costs_no_slot(lab: Lab) -> None:  # noqa: F811
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    lab.answers["a.fakeuni.test"] = [[PUBLIC_A]]
    lab.answers["cdn.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], suffixes=["fakeuni.test"], max_asset_hosts=1)
    assert await arm(lab.proxy, "fakeuni.test", token) == 204
    assert await arm(lab.proxy, "a.fakeuni.test", token) == 204  # a suffix host
    assert await arm(lab.proxy, "cdn.test", token) == 204  # the one slot is still free
    assert await arm(lab.proxy, "other.test", token) == 403


async def test_parallel_arms_cannot_overshoot_the_cap(lab: Lab) -> None:  # noqa: F811
    token = lab.token(["fakeuni.test"], max_asset_hosts=3)
    statuses = await asyncio.gather(*(arm(lab.proxy, f"cdn{i}.test", token) for i in range(20)))
    assert statuses.count(204) == 3
    assert statuses.count(403) == 17


async def test_an_armed_host_counts_against_the_connect_budget_like_any_other(lab: Lab) -> None:  # noqa: F811
    lab.answers["cdn.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], max_asset_hosts=2, max_connects=2)
    assert await arm(lab.proxy, "cdn.test", token) == 204
    assert await status_of(lab.proxy, "cdn.test:443", token) == 200
    assert await status_of(lab.proxy, "cdn.test:443", token) == 200
    assert await status_of(lab.proxy, "cdn.test:443", token) == 429


async def test_arm_is_logged_by_host_decision_and_status_only(
    lab: Lab,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "botai.browser.proxy.diagnostic_event", lambda e, **f: events.append((e, f))
    )
    token = lab.token(["fakeuni.test"], max_asset_hosts=2)
    with caplog.at_level("DEBUG", logger="botai.browser.proxy"):
        assert await arm(lab.proxy, "cdn.test", token) == 204
        await asyncio.sleep(0.1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "host=cdn.test decision=allow reason=asset_armed" in text
    assert token not in text
    (event, facts) = events[-1]
    assert event == "egress.decision"
    assert (facts["decision"], facts["reason"], facts["status"]) == ("allow", "asset_armed", 204)
    assert facts["host_hash"] == hashlib.sha256(b"cdn.test").hexdigest()[:16]
    assert "cdn.test" not in repr(events)


# --------------------------------------------------------- the gateway's side: Egress.arm_asset_host


async def test_egress_arms_over_the_wire_for_a_proxy_in_another_process(lab: Lab) -> None:  # noqa: F811
    egress = ProxyEgress(SECRET, server_url=lab.proxy.url)
    creds = await egress.credentials(["fakeuni.test"], ttl_s=60, assets=2)
    grant = verify_token(SECRET, creds.token)
    assert grant is not None
    assert grant.max_asset_hosts == 2
    assert await egress.arm_asset_host(creds.token, "cdn-a.test") is True
    assert await egress.arm_asset_host(creds.token, "cdn-b.test") is True
    assert await egress.arm_asset_host(creds.token, "cdn-c.test") is False  # capped
    assert await egress.arm_asset_host(creds.token, "127.0.0.1") is False
    assert await egress.arm_asset_host(creds.token, "cdn.test\r\nX: y") is False
    plain = await egress.credentials(["fakeuni.test"], ttl_s=60)
    assert await egress.arm_asset_host(plain.token, "cdn-a.test") is False


async def test_egress_arms_in_process_with_the_same_answers() -> None:
    proxy = EgressProxy(SECRET)
    egress = ProxyEgress(SECRET, proxy=proxy)
    creds = await egress.credentials(["fakeuni.test"], ttl_s=60, assets=1)
    try:
        assert await egress.arm_asset_host(creds.token, "cdn-a.test") is True
        assert await egress.arm_asset_host(creds.token, "cdn-b.test") is False
        assert await egress.arm_asset_host("garbage", "cdn-a.test") is False
        proxy.revoke(creds.token)
        assert await egress.arm_asset_host(creds.token, "cdn-a.test") is False
    finally:
        await egress.aclose()


async def test_egress_treats_an_unreachable_or_silent_proxy_as_a_refusal() -> None:
    nobody = ProxyEgress(SECRET, server_url="http://127.0.0.1:1")
    creds = await nobody.credentials(["fakeuni.test"], ttl_s=60, assets=2)
    assert await nobody.arm_asset_host(creds.token, "cdn.test") is False

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        answered = await egress_module._arm_remote(  # pyright: ignore[reportPrivateUsage]
            f"http://127.0.0.1:{port}", creds.token, "cdn.test", timeout_s=0.3
        )
        assert answered is False
        assert time.monotonic() - started < 3
    finally:
        server.close()
        await server.wait_closed()


async def test_the_grant_event_names_the_allowance_only_when_there_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "botai.browser.egress.diagnostic_event", lambda e, **f: events.append((e, f))
    )
    egress = ProxyEgress(SECRET, server_url="http://127.0.0.1:3128")
    await egress.credentials(["fakeuni.test"], ttl_s=60)
    await egress.credentials(["fakeuni.test"], ttl_s=60, assets=ASSET_HOSTS)
    plain, assets = (facts for _, facts in events)
    assert "asset_host_cap" not in plain
    assert assets["asset_host_cap"] == ASSET_HOSTS


# ------------------------------------------------------------------- how ARM is spelled


@pytest.mark.parametrize(
    "target",
    [
        "http://cdn.test:443/",  # the absolute form Chromium uses for an http:// URL
        "https://cdn.test:443/x.js",
        "cdn.test",
        "cdn.test:",
        "cdn.test:44a",
        ":443",
        "cdn.test:443/path",
    ],
)
async def test_arm_takes_a_host_and_port_and_nothing_else(lab: Lab, target: str) -> None:  # noqa: F811
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    status, _, _, writer = await send(lab.proxy, f"ARM {target} HTTP/1.1", token)
    writer.close()
    assert status == 400
    assert lab.proxy.stats["allow:asset_armed"] == 0


async def test_a_plain_http_request_through_the_proxy_is_not_a_connect_and_not_an_arm(
    lab: Lab,  # noqa: F811
) -> None:
    """For an http:// URL Chromium sends the absolute form (`GET http://host/ HTTP/1.1`) to a
    proxy; this one speaks CONNECT (and the private ARM) only."""
    token = lab.token(["fakeuni.test"], max_asset_hosts=ASSET_HOSTS)
    status, headers, _, writer = await send(lab.proxy, "GET http://cdn.test/x.js HTTP/1.1", token)
    writer.close()
    assert status == 405
    assert headers["allow"] == "CONNECT"


def test_a_proxy_url_is_plain_http_and_its_port_is_taken_as_chromium_takes_it() -> None:
    for bad in ("https://proxy:3128", "proxy:3128", "socks5://proxy:1080", "http://", ""):
        with pytest.raises(ValueError, match=r"http://host"):
            ProxyEgress(SECRET, server_url=bad)
    ProxyEgress(SECRET, server_url="http://proxy:3128")  # (accepted)


async def test_a_proxy_url_without_a_port_means_port_80() -> None:
    seen: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # no port: Chromium would go to 80, so must we (nothing listens there in this test)
        assert (
            await egress_module._arm_remote(  # pyright: ignore[reportPrivateUsage]
                "http://127.0.0.1", "token", "cdn.test", timeout_s=0.5
            )
            is False
        )
        assert seen == []
        assert (
            await egress_module._arm_remote(  # pyright: ignore[reportPrivateUsage]
                f"http://127.0.0.1:{port}", "token", "cdn.test"
            )
            is True
        )
        assert seen[0].startswith(b"ARM cdn.test:443 HTTP/1.1\r\n")
    finally:
        server.close()
        await server.wait_closed()


async def test_the_in_process_arm_is_counted_and_logged_like_the_one_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "botai.browser.proxy.diagnostic_event", lambda e, **f: events.append((e, f))
    )
    proxy = EgressProxy(SECRET)
    token = mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60, max_asset_hosts=1)
    assert proxy.arm_asset_host(token, "cdn.test") is None
    assert proxy.arm_asset_host(token, "other.test") is not None
    assert proxy.stats["allow:asset_armed"] == 1
    assert proxy.stats["deny:too_many_hosts"] == 1
    assert [(f["decision"], f["reason"], f["status"]) for _, f in events] == [
        ("allow", "asset_armed", 204),
        ("deny", "too_many_hosts", 403),
    ]
    assert "cdn.test" not in repr(events)
