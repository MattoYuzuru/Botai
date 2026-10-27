"""How the backend obtains proxy credentials (browser.egress) and the factory wiring."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlsplit

import pytest

from botai.browser import egress as egress_module
from botai.browser.egress import ProxyEgress, close_egress, get_egress, proxy_url_for
from botai.browser.factory import build_gateway
from botai.browser.gateway import PlaywrightGateway
from botai.browser.proxy import EgressProxy, derive_key, verify_token
from botai.settings import Settings

SECRET = derive_key(b"egress-test-master-secret-0123456789", "proxy-token")


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
async def fresh_egress() -> AsyncIterator[None]:
    await close_egress()
    yield
    await close_egress()


async def test_in_process_proxy_credentials_verify_with_the_secret() -> None:
    proxy = EgressProxy(SECRET)
    egress = ProxyEgress(SECRET, proxy=proxy)
    try:
        creds = await egress.credentials(["fakeuni.test", "id.fakeuni.test"], ttl_s=60)
        assert creds.server == proxy.url
        grant = verify_token(SECRET, creds.token)
        assert grant is not None
        assert grant.hosts == {"fakeuni.test", "id.fakeuni.test"}
        parts = urlsplit(creds.url)
        assert parts.username == "botai"
        assert unquote(parts.password or "") == creds.token
    finally:
        await egress.aclose()


async def test_external_proxy_credentials_do_not_start_anything() -> None:
    egress = ProxyEgress(SECRET, server_url="http://127.0.0.1:3128/")
    creds = await egress.credentials(["fakeuni.test"], ttl_s=60)
    assert creds.server == "http://127.0.0.1:3128"
    assert verify_token(SECRET, creds.token) is not None


def test_exactly_one_of_server_or_proxy() -> None:
    with pytest.raises(ValueError, match="either"):
        ProxyEgress(SECRET)
    with pytest.raises(ValueError, match="either"):
        ProxyEgress(SECRET, server_url="http://x:1", proxy=EgressProxy(SECRET))


async def test_external_mode_needs_the_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOTAI_PROXY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="BOTAI_PROXY_SECRET"):
        get_egress(settings(egress_proxy_url="http://127.0.0.1:3128"))
    monkeypatch.setenv("BOTAI_PROXY_SECRET", "k" * 40)
    external = get_egress(settings(egress_proxy_url="http://127.0.0.1:3128"))
    creds = await external.credentials(["fakeuni.test"], ttl_s=60)
    assert verify_token(derive_key(b"k" * 40, "proxy-token"), creds.token) is not None


async def test_embedded_mode_serves_tokens_it_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOTAI_PROXY_SECRET", raising=False)
    url = await proxy_url_for(settings(), ["fakeuni.test"])
    assert get_egress(settings()) is get_egress(settings())
    parts = urlsplit(url)
    token = unquote(parts.password or "")
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    credentials = base64.b64encode(f"botai:{token}".encode()).decode()
    request = (
        "CONNECT evil.test:443 HTTP/1.1\r\nHost: x\r\n"
        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
    )
    writer.write(request.encode())
    assert (await reader.readline()).startswith(b"HTTP/1.1 403")  # authenticated, then refused
    writer.close()


async def test_factory_builds_the_playwright_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOTAI_PROXY_SECRET", raising=False)
    assert isinstance(build_gateway(settings()), PlaywrightGateway)
    assert egress_module.get_egress(settings()) is not None
