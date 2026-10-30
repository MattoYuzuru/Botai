"""The asset gate's decisions that need no browser (security.md §4.4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from botai.browser.assets import (
    ASSET_TYPES,
    _foreign_pattern,  # pyright: ignore[reportPrivateUsage]
    asset_refusal,
    cookie_reaches,
    host_of,
    referer_leaks,
)

ALLOWED = ("fakeuni.test", "id.fakeuni.test", "my-site.example")


def foreign(url: str, *, schemes: str = "https?", suffixes: tuple[str, ...] = ()) -> bool:
    return bool(_foreign_pattern(schemes, ALLOWED, suffixes).match(url))


@pytest.mark.parametrize(
    "url",
    [
        "https://fakeuni.test/",
        "https://fakeuni.test",
        "https://fakeuni.test:443/app?x=1#f",
        "https://FakeUni.TEST/App",
        "https://id.fakeuni.test/sso",
        "https://my-site.example/a",
        "http://fakeuni.test/",
    ],
)
def test_the_hosts_of_the_connection_are_not_the_gates_business(url: str) -> None:
    assert not foreign(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.fakeuni-assets.test/x.js",
        "https://static.fakeuni.test/x.js",  # a sibling is not the connection's host
        "https://fakeuni.test.evil.test/x.js",
        "https://evilfakeuni.test/x.js",
        "https://fakeuni.testx/x.js",
        "https://user@evil.test/",
        "https://fakeuni.test@evil.test/",  # the host is what follows the @
        "https://fakeuni.test:evil@evil.test/",
        "https://127.0.0.1/x.png",
        "https://[::1]/x.png",
        "https://fakeuni.test./x.js",  # (re-checked per request: a trailing dot is the same host)
    ],
)
def test_every_other_host_goes_through_the_gate(url: str) -> None:
    assert foreign(url)


def test_login_mode_suffix_hosts_are_the_connections_too() -> None:
    assert not foreign("https://sso.corp.example/", suffixes=("corp.example",))
    assert not foreign("https://a.b.corp.example:443/x", suffixes=("corp.example",))
    assert foreign(
        "https://corp.example/", suffixes=("corp.example",)
    )  # the apex is not a subdomain
    assert foreign("https://evilcorp.example/", suffixes=("corp.example",))
    assert foreign("https://corp.example.evil.test/", suffixes=("corp.example",))


def test_sockets_and_pages_have_patterns_of_their_own() -> None:
    assert foreign("wss://cdn.test/s", schemes="wss?")
    assert not foreign("wss://fakeuni.test/s", schemes="wss?")
    assert not foreign("https://cdn.test/x", schemes="wss?")
    assert not foreign("wss://cdn.test/s")
    assert not foreign("data:text/plain,hi")
    assert not foreign("about:blank")
    assert not foreign("chrome-error://chromewebdata/")


def test_the_pattern_is_a_plain_regular_expression_javascript_can_run() -> None:
    """Playwright hands the source to JavaScript: no Python-only syntax (inline flags, `(?P<…>)`)."""
    pattern = _foreign_pattern("https?", ALLOWED, ("corp.example",))
    assert pattern.flags & re.IGNORECASE
    assert "(?i" not in pattern.pattern
    assert "(?P" not in pattern.pattern


@pytest.mark.parametrize("resource_type", sorted(ASSET_TYPES))
def test_a_get_of_a_page_asset_without_credentials_passes(resource_type: str) -> None:
    assert (
        asset_refusal("GET", resource_type, {"accept": "*/*", "referer": "https://a.test/"}) is None
    )


@pytest.mark.parametrize(
    "resource_type",
    [
        "document",
        "xhr",
        "fetch",
        "websocket",
        "eventsource",
        "ping",
        "manifest",
        "texttrack",
        "other",
        "prefetch",
    ],
)
def test_anything_that_carries_data_is_not_an_asset(resource_type: str) -> None:
    assert asset_refusal("GET", resource_type, {}) == "asset_type"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get", ""])
@pytest.mark.parametrize("resource_type", ["image", "script", "xhr", "document"])
def test_only_a_get_is_an_asset(method: str, resource_type: str) -> None:
    assert asset_refusal(method, resource_type, {}) == "asset_method"


@pytest.mark.parametrize("name", ["authorization", "Authorization", "AUTHORIZATION"])
def test_an_authorization_header_is_a_credential(name: str) -> None:
    assert asset_refusal("GET", "script", {name: "Bearer x"}) == "asset_credentials"


@pytest.mark.parametrize(
    ("domain", "host", "reaches"),
    [
        (".fakeuni.test", "fakeuni.test", True),
        (".fakeuni.test", "static.fakeuni.test", True),
        (".FakeUni.test", "a.b.fakeuni.test", True),
        ("fakeuni.test", "fakeuni.test", True),
        ("fakeuni.test", "static.fakeuni.test", False),  # host-only: this host alone
        (".fakeuni.test", "evilfakeuni.test", False),
        (".fakeuni.test", "cdn.fakeuni-assets.test", False),
        ("cdn.fakeuni-assets.test", "cdn.fakeuni-assets.test", True),
    ],
)
def test_cookie_reach_follows_the_browsers_domain_matching(
    domain: str, host: str, reaches: bool
) -> None:
    assert cookie_reaches(domain, host) is reaches


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://CDN.Test./x", "cdn.test"),
        ("https://user:pw@cdn.test:8443/x?y#z", "cdn.test"),
        ("https://[::1]/x", "::1"),
        ("data:text/plain,hi", ""),
        ("https://[bad/x", ""),
    ],
)
def test_host_of_is_lowercase_without_dot_and_never_raises(url: str, host: str) -> None:
    assert host_of(url) == host


def test_only_the_browser_gateway_asks_for_an_allowance() -> None:
    """A guard against drift: no other module passes `assets=` to `credentials`."""
    root = Path(__file__).resolve().parents[2] / "src" / "botai"
    askers = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(r"(?<![A-Za-z_])assets=", path.read_text(encoding="utf-8"))
    }
    assert askers == {"browser/gateway.py"}, askers


@pytest.mark.parametrize(
    ("headers", "leaks"),
    [
        ({"referer": "https://fakeuni.test/"}, False),  # the default policy: the origin only
        ({"referer": "https://fakeuni.test/login/step"}, False),  # a path is not a code
        ({"referer": "https://fakeuni.test/login?code=x"}, True),
        ({"Referer": "https://fakeuni.test/?ticket=ST-1"}, True),
        ({"referer": "https://fakeuni.test/cb?"}, False),
        ({}, False),
    ],
)
def test_a_referer_with_a_query_is_a_leak(headers: dict[str, str], leaks: bool) -> None:
    assert referer_leaks(headers) is leaks
