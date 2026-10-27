"""EgressProxy under a hostile or merely busy client: findings of an independent review."""

from __future__ import annotations

import asyncio
import base64
import logging
import time

import pytest

from botai.browser.proxy import (
    EgressProxy,
    address_block_reason,
    mint_token,
    normalize_host,
    verify_token,
)
from tests.browser.test_proxy import (  # noqa: F401 - the `lab` fixture
    PUBLIC_A,
    SECRET,
    Lab,
    _run_cli,
    connect,
    lab,
    status_of,
)


@pytest.fixture
def slow(lab: Lab) -> Lab:  # noqa: F811
    """The lab with DNS answering after a delay, so requests overlap like a browser's do."""
    original = lab.proxy._resolver

    async def slow_resolver(host: str, port: int) -> list[str]:
        await asyncio.sleep(0.15)
        return await original(host, port)

    lab.proxy._resolver = slow_resolver
    return lab


# ------------------------------------------------------------------------ parallel budgets


async def test_parallel_connects_cannot_overshoot_the_host_cap(slow: Lab) -> None:
    for i in range(30):
        slow.answers[f"h{i}.fakeuni.test"] = [[PUBLIC_A]]
    token = slow.token([], suffixes=["fakeuni.test"], max_dynamic_hosts=2)
    statuses = await asyncio.gather(
        *(status_of(slow.proxy, f"h{i}.fakeuni.test:443", token) for i in range(30))
    )
    assert statuses.count(200) == 2
    assert statuses.count(403) == 28


async def test_parallel_connects_cannot_overshoot_the_connect_budget(slow: Lab) -> None:
    slow.answers["fakeuni.test"] = [[PUBLIC_A]]
    token = slow.token(["fakeuni.test"], max_connects=3)
    statuses = await asyncio.gather(
        *(status_of(slow.proxy, "fakeuni.test:443", token) for _ in range(40))
    )
    assert statuses.count(200) == 3
    assert statuses.count(429) == 37


async def test_parallel_open_tunnels_are_capped_per_token(slow: Lab) -> None:
    slow.answers["fakeuni.test"] = [[PUBLIC_A]]
    slow.proxy._max_per_token = 3
    token = slow.token(["fakeuni.test"])
    results = await asyncio.gather(
        *(connect(slow.proxy, "fakeuni.test:443", token) for _ in range(20))
    )
    try:
        assert [r[0] for r in results].count(200) == 3
        assert [r[0] for r in results].count(429) == 17
    finally:
        for _, _, _, writer in results:
            writer.close()


async def test_a_failed_attempt_gives_the_slot_back(lab: Lab) -> None:  # noqa: F811
    lab.answers["fakeuni.test"] = [["10.0.0.5"]]  # refused as private: no tunnel, no budget spent
    token = lab.token(["fakeuni.test"], max_connects=1)
    for _ in range(5):
        assert await status_of(lab.proxy, "fakeuni.test:443", token) == 403
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 200


# -------------------------------------------------------------------- hostile input


async def _raw(proxy: EgressProxy, head: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(*proxy.address)
    writer.write(head)
    try:
        return await asyncio.wait_for(reader.read(200), 5)
    finally:
        writer.close()


async def test_non_ascii_credentials_get_an_answer_not_a_traceback(
    lab: Lab,  # noqa: F811
) -> None:
    loop = asyncio.get_running_loop()
    crashes: list[object] = []
    loop.set_exception_handler(lambda _loop, context: crashes.append(context))
    try:
        for header in (
            b"Proxy-Authorization: Basic \xe9\xe9\xe9",
            b"Proxy-Authorization: Basic " + base64.b64encode(b"botai:\xe9\xff.\xc3\xa9"),
            b"Proxy-Authorization: Basic " + base64.b64encode("botai:тест.подпись".encode()),
        ):
            reply = await _raw(
                lab.proxy,
                b"CONNECT fakeuni.test:443 HTTP/1.1\r\nHost: x\r\n" + header + b"\r\n\r\n",
            )
            assert reply.startswith(b"HTTP/1.1 407"), header
        token = lab.token(["fakeuni.test"])
        credentials = base64.b64encode(f"botai:{token}".encode()).decode()
        for authority in (
            b"fakeuni.test:\xc2\xb2",
            b"fakeuni.test:" + b"9" * 5000,
            b"fakeuni.test:-1",
        ):
            reply = await _raw(
                lab.proxy,
                b"CONNECT "
                + authority
                + b" HTTP/1.1\r\nProxy-Authorization: Basic "
                + credentials.encode()
                + b"\r\n\r\n",
            )
            assert reply.startswith(b"HTTP/1.1 400"), authority
    finally:
        loop.set_exception_handler(None)
    assert crashes == []


def test_verify_token_never_raises_on_odd_text() -> None:
    for token in ("é.é", "a.\xe9", "...", "", "x" * 10_000 + ".y", "١.٢"):
        assert verify_token(SECRET, token) is None


def test_a_label_may_not_end_in_a_newline() -> None:
    assert normalize_host("a\n.fakeuni.test") is None
    assert normalize_host("fakeuni.test") == "fakeuni.test"


async def test_what_is_logged_for_a_bad_authority_is_printable(
    lab: Lab,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = lab.token(["fakeuni.test"])
    credentials = base64.b64encode(f"botai:{token}".encode())
    with caplog.at_level(logging.INFO, logger="botai.browser.proxy"):
        await _raw(
            lab.proxy,
            b"CONNECT x\x1b[31mINFO:443 HTTP/1.1\r\nProxy-Authorization: Basic "
            + credentials
            + b"\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "\x1b" not in text
    assert "host=x" in text


# ------------------------------------------------------------------------ addresses


@pytest.mark.parametrize(
    "address",
    [
        "::7f00:1",  # IPv4-compatible 127.0.0.1
        "::a00:1",
        "::ffff:0:7f00:1",  # SIIT
        "ff02::1",
        "ff0e::1",
        "fec0::1",
        "2001:20::1",
        "5f00::1",
        "::",
    ],
)
def test_more_non_public_ipv6_forms_are_refused(address: str) -> None:
    assert address_block_reason(address) == "private_address", address


def test_public_ipv6_and_mapped_public_ipv4_still_pass() -> None:
    assert address_block_reason("2606:4700:4700::1111") is None
    assert address_block_reason("::ffff:93.184.216.34") is None


# ------------------------------------------------------------------ lifetime and tokens


async def test_close_does_not_wait_for_an_open_tunnel(lab: Lab) -> None:  # noqa: F811  # noqa: F811
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    status, _, _, writer = await connect(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"]))
    assert status == 200
    started = time.perf_counter()
    await asyncio.wait_for(lab.proxy.close(), 3)
    assert time.perf_counter() - started < 2
    writer.close()


async def test_a_revoked_token_stops_working(lab: Lab) -> None:  # noqa: F811  # noqa: F811
    lab.answers["fakeuni.test"] = [[PUBLIC_A]]
    token = lab.token(["fakeuni.test"], max_connects=1)
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 200
    lab.proxy.revoke(token)
    assert await status_of(lab.proxy, "fakeuni.test:443", token) == 407
    assert await status_of(lab.proxy, "fakeuni.test:443", lab.token(["fakeuni.test"])) == 200


async def test_idle_time_is_per_tunnel_not_per_direction() -> None:
    """A long server push (the client silent, the server sending) must not be cut after the idle time."""

    async def pusher(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(8):
            writer.write(b"x" * 100)
            await writer.drain()
            await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(pusher, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolver(host: str, _port: int) -> list[str]:
        return [PUBLIC_A]

    async def connector(
        address: str, _port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", port)

    proxy = EgressProxy(SECRET, resolver=resolver, connector=connector, idle_timeout_s=0.5)
    await proxy.start()
    try:
        token = mint_token(SECRET, hosts=["fakeuni.test"], ttl_s=60)
        status, _, reader, writer = await connect(proxy, "fakeuni.test:443", token)
        assert status == 200
        received = b""
        while chunk := await asyncio.wait_for(reader.read(1000), 3):
            received += chunk
        assert len(received) == 800  # all of it: 1.6 s of push over a 0.5 s idle limit
        writer.close()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


def test_token_lifetime_and_budgets_are_bounded() -> None:
    for kwargs in (
        {"ttl_s": 0},
        {"ttl_s": 7 * 24 * 3600},
        {"ttl_s": 60, "max_connects": 0},
        {"ttl_s": 60, "max_connects": 10**9},
        {"ttl_s": 60, "max_dynamic_hosts": 500},
    ):
        with pytest.raises(ValueError, match="out of range"):
            mint_token(SECRET, hosts=["fakeuni.test"], **kwargs)  # type: ignore[arg-type]


async def test_test_hosts_need_an_explicit_development_environment() -> None:
    proc = await _run_cli(
        {"BOTAI_PROXY_SECRET": "s" * 40, "BOTAI_ENV": ""}, "--test-host", "fakeuni.test=127.0.0.1:9"
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == 2
    assert b"BOTAI_ENV" in stderr
