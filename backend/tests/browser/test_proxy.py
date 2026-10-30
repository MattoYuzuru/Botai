"""EgressProxy (security.md §4, checks C2 and C3). No Chromium here: raw CONNECT requests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from botai.browser.egress import ProxyEgress
from botai.browser.proxy import (
    EgressProxy,
    address_block_reason,
    derive_key,
    is_ip_literal,
    mint_token,
    verify_token,
)

SECRET = derive_key(b"unit-test-master-secret-0123456789", "proxy-token")
PUBLIC_A = "93.184.216.34"
PUBLIC_B = "151.101.1.69"  # documentation ranges are not "global", so use real-looking ones


@dataclass
class Lab:
    proxy: EgressProxy
    answers: dict[str, list[list[str]]]
    resolved: list[str] = field(default_factory=lambda: list[str]())
    connected: list[tuple[str, int]] = field(default_factory=lambda: list[tuple[str, int]]())

    def token(self, hosts: list[str], **kw: object) -> str:
        return mint_token(SECRET, hosts=hosts, ttl_s=60, **kw)  # type: ignore[arg-type]


@pytest.fixture
async def lab() -> AsyncIterator[Lab]:
    """A proxy whose DNS and TCP are replaced by recorders; the upstream is a local echo server."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(4096):
            writer.write(data)
            await writer.drain()
        writer.close()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]
    state = Lab(proxy=None, answers={})  # type: ignore[arg-type]

    async def resolver(host: str, port: int) -> list[str]:
        state.resolved.append(host)
        queue = state.answers.get(host)
        if not queue:
            raise OSError("NXDOMAIN")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def connector(
        address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        state.connected.append((address, port))
        return await asyncio.open_connection("127.0.0.1", echo_port)

    state.proxy = EgressProxy(SECRET, resolver=resolver, connector=connector)
    await state.proxy.start()
    try:
        yield state
    finally:
        await state.proxy.close()
        echo_server.close()
        await echo_server.wait_closed()


async def send(
    proxy: EgressProxy,
    first_line: str,
    token: str | None = None,
    extra: tuple[str, ...] = (),
) -> tuple[int, dict[str, str], asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(*proxy.address)
    lines = [first_line, "Host: example"]
    if token is not None:
        credentials = base64.b64encode(f"botai:{token}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {credentials}")
    lines.extend(extra)
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode()
    status_line, *header_lines = head.strip().split("\r\n")
    headers = {k.lower(): v for k, _, v in (h.partition(": ") for h in header_lines)}
    return int(status_line.split()[1]), headers, reader, writer


async def connect(
    proxy: EgressProxy, authority: str, token: str | None
) -> tuple[int, dict[str, str], asyncio.StreamReader, asyncio.StreamWriter]:
    return await send(proxy, f"CONNECT {authority} HTTP/1.1", token)


async def status_of(proxy: EgressProxy, authority: str, token: str | None) -> int:
    status, _, _, writer = await connect(proxy, authority, token)
    writer.close()
    return status


# ----------------------------------------------------------------------------------- allow path


async def test_allowed_host_is_tunnelled_to_the_pinned_address(lab: Lab) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    status, _, reader, writer = await connect(
        lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])
    )
    assert status == 200
    writer.write(b"hello through the tunnel")
    assert await asyncio.wait_for(reader.read(100), 5) == b"hello through the tunnel"
    writer.close()
    assert lab.connected == [(PUBLIC_A, 443)]


@pytest.mark.parametrize("case", ["allow", "deny", "invalid_auth"])
async def test_persistent_decisions_correlate_grants_without_host_or_credential_values(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    marker = "private-marker-never-retained"
    allowed_host = f"{marker}.fakeuni.test"
    denied_host = f"denied-{marker}.fakeuni.test"
    lab.answers[allowed_host] = [[PUBLIC_A]]
    events: list[tuple[str, dict[str, object]]] = []
    completed = asyncio.Event()

    def record(event: str, **facts: object) -> None:
        events.append((event, facts))
        if event == "egress.decision":
            completed.set()

    monkeypatch.setattr("botai.browser.egress.diagnostic_event", record)
    monkeypatch.setattr("botai.browser.proxy.diagnostic_event", record)
    credentials = await ProxyEgress(SECRET, server_url=lab.proxy.url).credentials(
        [allowed_host], ttl_s=60
    )
    grant = verify_token(SECRET, credentials.token)
    assert grant is not None
    grant_hash = hashlib.sha256(grant.jti.encode()).hexdigest()[:16]
    assert events == [("egress.grant", {"grant_hash": grant_hash, "allow_host_count": 1})]
    target = denied_host if case == "deny" else allowed_host
    token = credentials.token if case != "invalid_auth" else f"invalid-{marker}"
    status, _, reader, writer = await connect(lab.proxy, f"{target}:443", token)
    assert status == {"allow": 200, "deny": 403, "invalid_auth": 407}[case]
    if case == "allow":
        writer.write(marker.encode())
        await writer.drain()
        assert await asyncio.wait_for(reader.read(len(marker)), 1) == marker.encode()
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(completed.wait(), 2)
    event, facts = events[-1]
    assert event == "egress.decision"
    assert all(facts[key] is None for key in ("job_id", "thread_id", "connection_id", "request_id"))
    assert facts["status"] == status
    assert facts["decision"] == ("allow" if case == "allow" else "deny")
    assert (
        facts["reason"]
        == {"allow": "ok", "deny": "not_allowlisted", "invalid_auth": "auth_failed"}[case]
    )
    if case == "invalid_auth":
        assert "grant_hash" not in facts
        assert "host_hash" not in facts
    else:
        assert facts["grant_hash"] == grant_hash
        assert facts["host_hash"] == hashlib.sha256(target.encode()).hexdigest()[:16]
    if case == "allow":
        assert facts["up_bytes"] == len(marker)
        assert facts["down_bytes"] == len(marker)
    assert marker not in repr(events)
    assert grant.jti not in repr(events)
    assert credentials.token not in repr(events)


async def test_host_matching_is_case_insensitive_and_ignores_trailing_dot(lab: Lab) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    assert await status_of(lab.proxy, "FakeUni.TEST.:443", lab.token(["fakeuni.test"])) == 200


# ---------------------------------------------------------------------------------- credentials


async def test_requests_without_valid_token_get_407_and_nothing_is_resolved(lab: Lab) -> None:
    good = lab.token(["fakeuni.test"])
    tampered = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    expired = mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=10, now=time.time() - 3600)
    foreign = mint_token(
        derive_key(b"another-master-secret-0123456789", "x"), hosts=["a.test"], ttl_s=60
    )
    for token in (None, "garbage", tampered, expired, foreign):
        status, headers, _, writer = await connect(lab.proxy, "fakeuni.test:443", token)
        writer.close()
        assert status == 407
        assert "proxy-authenticate" in headers
    assert lab.resolved == []
    assert lab.connected == []


async def test_token_with_tampered_payload_is_rejected() -> None:
    token = mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60)
    body, _, signature = token.partition(".")
    other = mint_token(SECRET, hosts=["evil.test"], ttl_s=60).partition(".")[0]
    assert verify_token(SECRET, token) is not None
    assert verify_token(SECRET, f"{other}.{signature}") is None
    assert verify_token(SECRET, body) is None


async def test_only_connect_is_accepted(lab: Lab) -> None:
    token = lab.token(["fakeuni.test"])
    status, headers, _, writer = await send(lab.proxy, "GET http://fakeuni.test/ HTTP/1.1", token)
    writer.close()
    assert status == 405
    assert headers["allow"] == "CONNECT"
    assert lab.resolved == []


# ----------------------------------------------------------------------------------- allowlist


async def test_host_outside_allowlist_is_refused_before_dns(lab: Lab) -> None:
    lab.answers["evil.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"])
    assert await status_of(lab.proxy, "evil.test:443", token) == 403
    assert await status_of(lab.proxy, "fakeuni.test.evil.test:443", token) == 403
    assert await status_of(lab.proxy, "sub.fakeuni.test:443", token) == 403
    assert lab.resolved == []
    assert lab.connected == []
    assert lab.proxy.stats["deny:not_allowlisted"] == 3


@pytest.mark.parametrize(
    "authority",
    [
        "127.0.0.1:443",
        "169.254.169.254:443",
        "10.0.0.5:443",
        "[::1]:443",
        "[fd00::1]:443",
        "2130706433:443",  # 127.0.0.1 as a decimal integer
        "0x7f.0.0.1:443",
        "127.1:443",
        "0177.0.0.1:443",
        "[::ffff:127.0.0.1]:443",
    ],
)
async def test_ip_literals_are_refused_even_if_listed(lab: Lab, authority: str) -> None:
    host = authority.rsplit(":", 1)[0].strip("[]")
    lab.answers[host] = [["127.0.0.1"]]
    # mint_token refuses IP literals, so the literal is never on the list: the proxy still says no
    token = lab.token(["fakeuni.test"])
    assert await status_of(lab.proxy, authority, token) == 403
    assert lab.connected == []


@pytest.mark.parametrize("port", [80, 8443, 22, 0, 65535])
async def test_only_port_443(lab: Lab, port: int) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    assert await status_of(lab.proxy, f"fakeuni.test:{port}", lab.token(["fakeuni.test"])) == 403
    assert lab.connected == []


@pytest.mark.parametrize(
    "authority", ["fakeuni.test", ":443", "fakeuni.test:abc", "exa mple.test:443"]
)
async def test_malformed_authority(lab: Lab, authority: str) -> None:
    assert await status_of(lab.proxy, authority, lab.token(["fakeuni.test"])) in (400, 403)
    assert lab.connected == []


# ------------------------------------------------------------------------- private ranges (C3)


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["172.16.4.4"],
        ["192.168.1.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],  # noqa: S104
        ["::1"],
        ["fd00::1"],
        ["fe80::1"],
        ["::ffff:10.0.0.1"],
        ["64:ff9b::7f00:1"],
        ["2002:7f00:1::1"],
        ["224.0.0.1"],
        [PUBLIC_A, "10.0.0.5"],  # one bad answer poisons the name
        ["10.0.0.5", PUBLIC_A],
    ],
)
async def test_names_resolving_to_non_public_addresses_are_refused(
    lab: Lab, answer: list[str]
) -> None:
    lab.answers["fakeuni.test"] = [answer]
    assert await status_of(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])) == 403
    assert lab.connected == []
    assert lab.proxy.stats["deny:private_address"] == 1


async def test_dns_failure_is_a_bad_gateway(lab: Lab) -> None:
    assert await status_of(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])) == 502
    assert lab.connected == []


async def test_rebinding_resolves_once_and_connects_to_the_checked_address(lab: Lab) -> None:
    # first answer public, any later lookup would answer a private address (rebinding)
    lab.answers["fakeuni.test"] = [[PUBLIC_A], ["169.254.169.254"]]
    status, _, _, writer = await connect(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"]))
    writer.close()
    assert status == 200
    assert lab.resolved == ["fakeuni.test"]
    assert lab.connected == [(PUBLIC_A, 443)]


def test_address_policy_table() -> None:
    assert address_block_reason(PUBLIC_A) is None
    assert address_block_reason(PUBLIC_B) is None
    assert address_block_reason("2606:4700:4700::1111") is None
    for blocked in ("127.0.0.1", "10.1.2.3", "100.127.255.255", "169.254.0.1", "198.18.0.1"):
        assert address_block_reason(blocked) == "private_address", blocked
    assert address_block_reason("not-an-ip") == "invalid_address"


def test_ip_literal_detection() -> None:
    for literal in ("127.0.0.1", "::1", "[::1]", "2130706433", "0x7f.1", "1.2.3.4.5", "foo.123"):
        assert is_ip_literal(literal), literal
    for name in ("fakeuni.test", "id.fakeuni.test", "a1.example.com", "123abc.example.ru"):
        assert not is_ip_literal(name), name


# ---------------------------------------------------------------------------------------- budget


async def test_connection_budget(lab: Lab) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], max_connects=2)
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 200
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 200
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 429
    # another token has its own budget
    assert await status_of(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])) == 200


async def test_denied_requests_do_not_consume_the_budget(lab: Lab) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], max_connects=1)
    for _ in range(5):
        assert await status_of(lab.proxy, "evil.test:443", token) == 403
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 200


# ------------------------------------------------------------------------------- suffix (login)


async def test_suffix_rule_admits_subdomains_up_to_a_cap(lab: Lab) -> None:
    for i in range(4):
        lab.answers[f"h{i}.fakeuni.test"] = [[PUBLIC_A]]
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    lab.answers["evilfakeuni.test"] = [[PUBLIC_A]]
    token = lab.token([], suffixes=["fakeuni.test"], max_dynamic_hosts=2)
    assert await status_of(lab.proxy, "h0.fakeuni.test:443", token) == 200
    assert await status_of(lab.proxy, "h0.fakeuni.test:443", token) == 200  # already admitted
    assert await status_of(lab.proxy, "h1.fakeuni.test:443", token) == 200
    assert await status_of(lab.proxy, "h2.fakeuni.test:443", token) == 403  # third host: capped
    assert lab.proxy.stats["deny:too_many_hosts"] == 1
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 403  # apex is not a subdomain
    assert await status_of(lab.proxy, "evilfakeuni.test:443", token) == 403


def test_mint_rejects_bad_hosts() -> None:
    for bad in ("127.0.0.1", "*.fakeuni.test", "fakeuni.test:443", "https://fakeuni.test", ""):
        with pytest.raises(ValueError, match="allowable host"):
            mint_token(SECRET, hosts=[bad], ttl_s=60)
    with pytest.raises(ValueError, match="at least one"):
        mint_token(SECRET, hosts=[], ttl_s=60)
    with pytest.raises(ValueError, match="at most"):
        mint_token(SECRET, hosts=[f"h{i}.test" for i in range(13)], ttl_s=60)


# --------------------------------------------------------------------------------------- logging


async def test_only_host_decision_and_sizes_are_logged(
    lab: Lab, caplog: pytest.LogCaptureFixture
) -> None:
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"])
    with caplog.at_level(logging.DEBUG, logger="botai.browser.proxy"):
        status, _, reader, writer = await connect(lab.proxy, "fakeuni.test:443", token)
        assert status == 200
        writer.write(b"GET /private/path?student=12345 HTTP/1.1\r\n\r\n")
        await reader.read(100)
        writer.close()
        await status_of(lab.proxy, "evil.test:443", token)
        await asyncio.sleep(0.1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "host=fakeuni.test decision=allow" in text
    assert "host=evil.test decision=deny reason=not_allowlisted" in text
    for secret in (token, token.partition(".")[0], "private", "12345", "Proxy-Authorization"):
        assert secret not in text


# ------------------------------------------------------------------ real fixture, real redirects


@pytest.mark.parametrize(
    ("path", "denial"),
    [
        ("/redirect/evil", "deny:not_allowlisted"),
        ("/redirect/ip", "deny:ip_literal"),
        ("/redirect/metadata", "deny:ip_literal"),
    ],
)
async def test_redirect_from_an_allowed_host_to_a_foreign_one_is_blocked(
    egress: EgressProxy, trust_fakeuni: object, path: str, denial: str
) -> None:
    """C2: the allowed host answers 302 to a host outside the allowlist; the follow-up CONNECT dies."""
    token = egress.mint(hosts=["fakeuni.test"], ttl_s=60)
    before = egress.stats[denial]
    async with httpx.AsyncClient(proxy=egress.url_for(token), follow_redirects=True) as client:
        ok = await client.get("https://fakeuni.test/auth/login")
        assert ok.status_code == 200
        with pytest.raises(httpx.ProxyError):
            await client.get(f"https://fakeuni.test{path}")
    assert egress.stats[denial] == before + 1


async def test_listed_host_that_resolves_to_loopback_is_still_refused(egress: EgressProxy) -> None:
    """C3 with the system resolver: allow-listing a name never opens loopback."""
    token = egress.mint(hosts=["evil.localhost"], ttl_s=60)
    status = await status_of(egress, "evil.localhost:443", token)
    assert status in (403, 502)  # private_address, or dns_failure where *.localhost is not resolved


# ------------------------------------------------------------------------------------------ CLI


async def _run_cli(env: dict[str, str], *args: str) -> asyncio.subprocess.Process:
    import os
    import sys

    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "botai.browser.proxy",
        *args,
        env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_cli_refuses_to_start_without_a_secret() -> None:
    proc = await _run_cli({"BOTAI_PROXY_SECRET": ""})
    _, stderr = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 2
    assert b"BOTAI_PROXY_SECRET" in stderr


async def test_cli_refuses_test_hosts_in_production() -> None:
    proc = await _run_cli(
        {"BOTAI_PROXY_SECRET": "s" * 40, "BOTAI_ENV": "prod"},
        "--test-host",
        "fakeuni.test=127.0.0.1:9",
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 2
    assert b"--test-host" in stderr


async def test_cli_serves_and_demands_credentials() -> None:
    proc = await _run_cli({"BOTAI_PROXY_SECRET": "s" * 40}, "--listen", "127.0.0.1:0")
    try:
        assert proc.stderr is not None
        address = ""
        async with asyncio.timeout(20):
            while not address:
                line = (await proc.stderr.readline()).decode()
                assert line, "proxy exited before listening"
                if "listening on" in line:
                    address = line.rsplit(" ", 1)[-1].strip()
        host, _, port = address.rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b"CONNECT fakeuni.test:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert (await reader.readline()).startswith(b"HTTP/1.1 407")
        writer.close()
    finally:
        proc.terminate()
        await proc.wait()


# ----------------------------------------------------------------------------------- relays


async def test_a_relayed_host_goes_through_the_relay_and_others_do_not(lab: Lab) -> None:
    """Groq refuses the server's country: the operator routes that one granted host through a
    relay inside the stack. The relay name may resolve privately; other hosts keep the policy."""
    proxy = EgressProxy(
        SECRET,
        resolver=lab.proxy._resolver,  # pyright: ignore[reportPrivateUsage]
        connector=lab.proxy._connector,  # pyright: ignore[reportPrivateUsage]
        relays={"api.groq.com": ("relay", 18443)},
    )
    await proxy.start()
    try:
        lab.answers["relay"] = [["172.18.0.9"]]
        status, _, reader, writer = await connect(
            proxy, "api.groq.com:443", lab.token(["api.groq.com"])
        )
        assert status == 200
        writer.write(b"tls hello")
        assert await asyncio.wait_for(reader.read(100), 5) == b"tls hello"
        writer.close()
        assert lab.connected == [("172.18.0.9", 18443)]
        assert "api.groq.com" not in lab.resolved  # the relay resolves the real host, abroad

        # the relay serves only its granted host: anything else is still refused by the policy
        lab.answers["fakeuni.test"] = [["10.0.0.5"]]
        assert await status_of(proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])) == 403
        # and a relayed host still needs a grant
        assert await status_of(proxy, "api.groq.com:443", lab.token(["fakeuni.test"])) == 403
    finally:
        await proxy.close()


async def test_an_unreachable_relay_is_a_bad_gateway(lab: Lab) -> None:
    proxy = EgressProxy(
        SECRET,
        resolver=lab.proxy._resolver,  # pyright: ignore[reportPrivateUsage]
        connector=lab.proxy._connector,  # pyright: ignore[reportPrivateUsage]
        relays={"api.groq.com": ("relay", 18443)},
    )
    await proxy.start()
    try:
        assert await status_of(proxy, "api.groq.com:443", lab.token(["api.groq.com"])) == 502
        assert lab.connected == []
    finally:
        await proxy.close()


def test_relay_configuration_is_checked() -> None:
    with pytest.raises(ValueError, match="relay"):
        EgressProxy(SECRET, relays={"api.groq.com": ("", 18443)})
    with pytest.raises(ValueError, match="relay"):
        EgressProxy(SECRET, relays={"api.groq.com": ("relay", 0)})
