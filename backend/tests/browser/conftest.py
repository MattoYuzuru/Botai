"""Fixtures for browser tests: fakeuni over HTTPS on loopback, reached only through an EgressProxy.

The browser and httpx see `https://fakeuni.test` (port 443). The proxy maps `*.fakeuni.test` to the
loopback port of the uvicorn server; nothing else is reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.proxy import EgressProxy, derive_key
from tests.fakeuni.app import create_fakeuni

FAKEUNI_HOSTS = ("fakeuni.test", "id.fakeuni.test", "api.fakeuni.test")
PROXY_SECRET = derive_key(b"test-master-secret-0123456789abcdef", "proxy-token")


@dataclass(frozen=True, slots=True)
class Tls:
    cert: Path
    key: Path


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    directory = tmp_path_factory.mktemp("fakeuni-tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fakeuni.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("fakeuni.test"), x509.DNSName("*.fakeuni.test")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    paths = Tls(directory / "cert.pem", directory / "key.pem")
    paths.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


class QuietServer(uvicorn.Server):
    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


@contextlib.asynccontextmanager
async def serve(app: Any) -> AsyncIterator[int]:
    """Run an ASGI app on a free loopback port for the length of the block."""
    server = QuietServer(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="off"
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        await task


@pytest.fixture(scope="session")
async def fakeuni_port(tls: Tls) -> AsyncIterator[int]:
    config = uvicorn.Config(
        create_fakeuni(),
        host="127.0.0.1",
        port=0,
        ssl_certfile=str(tls.cert),
        ssl_keyfile=str(tls.key),
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = QuietServer(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    try:
        yield port
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def trust_fakeuni(tls: Tls, monkeypatch: pytest.MonkeyPatch) -> Path:
    """httpx (and anything using the default SSL context) trusts the fixture certificate."""
    monkeypatch.setenv("SSL_CERT_FILE", str(tls.cert))
    return tls.cert


@pytest.fixture(scope="session")
async def egress(fakeuni_port: int) -> AsyncIterator[EgressProxy]:
    proxy = EgressProxy(
        PROXY_SECRET,
        test_hosts={host: ("127.0.0.1", fakeuni_port) for host in FAKEUNI_HOSTS},
    )
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()


@pytest.fixture(scope="session")
def egress_access(egress: EgressProxy) -> ProxyEgress:
    """How code under test obtains proxy tokens for the shared fixture proxy."""
    return ProxyEgress(PROXY_SECRET, proxy=egress)


@pytest.fixture(scope="session")
async def gateway(egress_access: ProxyEgress) -> AsyncIterator[PlaywrightGateway]:
    # sandbox off and certificate errors ignored: fixture server only, never a real site
    gw = PlaywrightGateway(egress_access, sandbox=False, ignore_https_errors=True)
    yield gw
    await gw.aclose()
