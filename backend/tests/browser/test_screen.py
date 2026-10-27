"""The fallback remote screen over a real WebSocket, with real Chromium (marker `browser`).

Measures the ADR-0001 targets on the fixture: first frame <= 2 s, input -> frame p95 <= 150 ms,
reconnection <= 3 s without losing the page. Numbers are printed (run with `-s`) and attached to
the report as properties.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from playwright.async_api import async_playwright
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from botai.auth.deps import CurrentUser, current_user
from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.login import LoginFlow
from botai.browser.presets import InMemoryLoginStore
from botai.browser.router import get_login_store, get_screens_dep
from botai.browser.router import router as browser_router
from botai.browser.screen import (
    QualityController,
    ScreenConfig,
    ScreenRegistry,
    ScreenSession,
    mint_ticket,
    verify_ticket,
)
from botai.contracts.agent_ui import LoginBlock
from botai.core.errors import BotaiError, botai_error_handler
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.vault import InMemoryVault
from tests.browser.conftest import serve
from tests.browser.test_login import FAST, ORIGIN, Student, preset_dict, works
from tests.fakeuni import data

pytestmark = pytest.mark.browser

PUBLIC = "http://localhost:5173"
IN_CI = bool(os.environ.get("CI"))
SECRET = b"s" * 32


# ------------------------------------------------------------------------------- harness


class ScreenClient:
    """A viewer: collects frames (with arrival times) and other messages, acks like a client."""

    def __init__(self, ws: ClientConnection, *, auto_ack: bool = True) -> None:
        self.ws = ws
        self.auto_ack = auto_ack
        self.frames: list[tuple[float, dict[str, Any]]] = []
        self.messages: list[dict[str, Any]] = []
        self.opened = time.perf_counter()
        self.close_code: int | None = None
        self._arrived = asyncio.Event()
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for raw in self.ws:
                at = time.perf_counter()
                message = json.loads(raw)
                if message["t"] == "frame":
                    self.frames.append((at, message))
                    if self.auto_ack:
                        await self.ws.send(json.dumps({"t": "ack", "seq": message["seq"]}))
                else:
                    self.messages.append(message)
                self._arrived.set()
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else 1006
        if self.close_code is None:
            self.close_code = self.ws.close_code  # a normal close ends the iteration silently
        self._arrived.set()

    async def send(self, **message: Any) -> None:
        await self.ws.send(json.dumps(message))

    async def ack(self, seq: int) -> None:
        await self.send(t="ack", seq=seq)

    async def until(self, predicate: Callable[[], bool], within: float = 5.0) -> None:
        deadline = time.perf_counter() + within
        while not predicate():
            left = deadline - time.perf_counter()
            if left <= 0:
                raise TimeoutError("condition not reached")
            self._arrived.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._arrived.wait(), min(left, 0.2))

    async def first_frame(self, within: float = 5.0) -> dict[str, Any]:
        await self.until(lambda: bool(self.frames), within)
        return self.frames[0][1]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["t"] == kind]

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()
        await self._task


@dataclass
class ScreenRig:
    """`registry` is what the API process holds, `session` the screen next to the browser. In the
    `local` topology they are one process; in `relay` the API has no screens of its own and
    reaches the worker's through the internal link."""

    registry: ScreenRegistry
    port: int
    handle: PlaywrightHandle
    session: ScreenSession
    user: str
    conn: str
    worker: ScreenRegistry | None = None
    clients: list[ScreenClient] = field(default_factory=lambda: list[ScreenClient]())

    def url(self, ticket: str | None = None, session_id: str | None = None) -> str:
        ticket = ticket or self.registry.mint_ticket(self.session).ticket
        return (
            f"ws://127.0.0.1:{self.port}/api/v1/login-sessions/"
            f"{session_id or self.session.id}/ws?ticket={ticket}"
        )

    async def connect(
        self,
        *,
        ticket: str | None = None,
        origin: str | None = PUBLIC,
        auto_ack: bool = True,
        session_id: str | None = None,
    ) -> ScreenClient:
        ws = await connect(self.url(ticket, session_id), origin=origin, max_size=None)
        client = ScreenClient(ws, auto_ack=auto_ack)
        self.clients.append(client)
        return client

    async def settle(self) -> None:
        await asyncio.sleep(0.15)


def build_app(registry: ScreenRegistry, store: InMemoryLoginStore, user: str) -> FastAPI:
    """Only the browser router: `create_app()` would reconfigure logging mid-test."""
    app = FastAPI()
    app.add_exception_handler(BotaiError, botai_error_handler)
    app.include_router(browser_router)
    app.dependency_overrides[get_screens_dep] = lambda: registry
    app.dependency_overrides[get_login_store] = lambda: store
    app.dependency_overrides[current_user] = lambda: CurrentUser(user, "u@t.test", False)
    return app


@pytest.fixture(scope="session")
async def decoder() -> AsyncIterator[Callable[[list[str]], Awaitable[list[float]]]]:
    """Mean brightness (0..255) of the bottom-right corner of base64 JPEGs (a plain Chromium decodes)."""
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(chromium_sandbox=False)
    page = await browser.new_page()

    async def luma(frames: list[str]) -> list[float]:
        return await page.evaluate(
            """async (frames) => {
              const out = [];
              for (const b64 of frames) {
                const img = new Image();
                img.src = 'data:image/jpeg;base64,' + b64;
                await img.decode();
                const c = document.createElement('canvas'); c.width = 8; c.height = 8;
                const g = c.getContext('2d'); g.drawImage(img, img.width - 40, img.height - 40, 40, 40, 0, 0, 8, 8);
                const d = g.getImageData(0, 0, 8, 8).data;
                let sum = 0;
                for (let i = 0; i < d.length; i += 4) sum += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
                out.push(sum / 64);
              }
              return out;
            }""",
            frames,
        )

    yield luma
    await browser.close()
    await playwright.stop()


async def _rigs(
    gateway: PlaywrightGateway, relay: bool
) -> AsyncIterator[Callable[..., Awaitable[ScreenRig]]]:
    rigs: list[ScreenRig] = []
    stack = contextlib.AsyncExitStack()

    async def build(
        config: ScreenConfig | None = None, path: str = "/screen/lab", user: str | None = None
    ) -> ScreenRig:
        user = user or new_id()
        conn = new_id()
        cfg = config or ScreenConfig()
        store = InMemoryLoginStore()
        store.add_connection(user, conn, ORIGIN)
        handle = await gateway.open(
            user_id=user, connection_id=conn, allow_hosts=["fakeuni.test"], origin=ORIGIN
        )
        await handle.goto(f"{ORIGIN}{path}")
        if relay:
            worker = ScreenRegistry(
                secret=SECRET, public_base_url=PUBLIC, config=cfg, internal_bind=("127.0.0.1", 0)
            )
            session = await worker.open(handle, user_id=user, connection_id=conn)
            registry = ScreenRegistry(
                secret=SECRET,
                public_base_url=PUBLIC,
                config=cfg,
                remote_urls=[f"http://127.0.0.1:{worker.internal_port}"],
            )
        else:
            worker = None
            registry = ScreenRegistry(secret=SECRET, public_base_url=PUBLIC, config=cfg)
            session = await registry.open(handle, user_id=user, connection_id=conn)
        port = await stack.enter_async_context(serve(build_app(registry, store, user)))
        rig = ScreenRig(registry, port, handle, session, user, conn, worker)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        for client in rig.clients:
            await client.close()
        await rig.session.close("test over")
        if rig.worker is not None:
            await rig.worker.close_all()
        await rig.handle.close()
    await stack.aclose()


@pytest.fixture(params=["local", "relay"])
async def make_screen(
    request: pytest.FixtureRequest, gateway: PlaywrightGateway
) -> AsyncIterator[Callable[..., Awaitable[ScreenRig]]]:
    """Every test using it runs twice: API and browser in one process, and as in production,
    where the API relays the viewer's socket to the worker that holds the browser."""
    async for build in _rigs(gateway, request.param == "relay"):
        yield build


@pytest.fixture
async def make_relayed_screen(
    gateway: PlaywrightGateway,
) -> AsyncIterator[Callable[..., Awaitable[ScreenRig]]]:
    """Only the production topology, for tests about the link itself."""
    async for build in _rigs(gateway, True):
        yield build


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


# ------------------------------------------------------------------- targets: first frame, latency


async def test_first_frame_arrives_within_two_seconds(
    make_screen: Callable[..., Awaitable[ScreenRig]], record_property: Callable[[str, object], None]
) -> None:
    rig = await make_screen()
    started = time.perf_counter()
    client = await rig.connect()
    frame = await client.first_frame()
    first_ms = (time.perf_counter() - started) * 1000
    print(f"\n[remote screen] first frame after connect: {first_ms:.0f} ms")
    record_property("first_frame_ms", round(first_ms))
    assert first_ms <= 2000
    assert (frame["mime"], frame["w"], frame["h"]) == ("image/jpeg", 390, 760)
    assert frame["seq"] == 1
    assert frame["data"].startswith("/9j/")  # a JPEG
    await client.until(lambda: bool(client.of("state")))
    state = client.of("state")[0]
    assert state["url"] == "https://fakeuni.test/screen/lab"
    assert state["title"] == "Screen lab"
    assert state["loading"] is False


@pytest.mark.parametrize(
    ("label", "path"), [("plain page", "/screen/lab"), ("busy page", "/screen/lab?busy=1")]
)
async def test_input_to_frame_latency_p95(
    make_screen: Callable[..., Awaitable[ScreenRig]],
    decoder: Callable[[list[str]], Awaitable[list[float]]],
    record_property: Callable[[str, object], None],
    label: str,
    path: str,
) -> None:
    rig = await make_screen(path=path)
    client = await rig.connect()
    await client.first_frame()
    await asyncio.sleep(0.3)
    clicks: list[float] = []
    for _ in range(40):
        await asyncio.sleep(0.2)
        sent = time.perf_counter()
        clicks.append(sent)
        await client.send(t="mouse", kind="click", x=200, y=600, ts=sent * 1000)
    await asyncio.sleep(0.5)

    brightness = await decoder([f["data"] for _, f in client.frames])
    arrivals = [at for at, _ in client.frames]
    latencies: list[float] = []
    for i, sent in enumerate(clicks):
        want_white = i % 2 == 0  # the page starts black and flips on every click
        seen = [
            at
            for at, luma in zip(arrivals, brightness, strict=True)
            if at >= sent and (luma > 128) == want_white
        ]
        assert seen, f"click {i} never showed on the screen"
        latencies.append((min(seen) - sent) * 1000)
    p50, p95, worst = statistics.median(latencies), percentile(latencies, 0.95), max(latencies)
    fps_window = arrivals[-1] - arrivals[0]
    sizes = [len(f["data"]) for _, f in client.frames]
    print(
        f"\n[remote screen] ({label}) input -> frame over {len(latencies)} clicks: "
        f"p50 {p50:.0f} ms, p95 {p95:.0f} ms, max {worst:.0f} ms; "
        f"{len(client.frames)} frames in {fps_window:.1f} s, median frame {statistics.median(sizes) / 1024:.1f} KiB"
    )
    for name, value in (("p50", p50), ("p95", p95), ("max", worst)):
        record_property(f"input_to_frame_{name}_ms ({label})", round(value))
    assert p95 <= (400 if IN_CI else 150)  # ADR-0001 §3 locally; CI machines are shared
    # the echoed client timestamp lets a real client measure the same thing without clocks
    echoed = [f["inputTs"] for _, f in client.frames if f.get("inputTs") is not None]
    assert echoed
    assert max(echoed) == pytest.approx(clicks[-1] * 1000)


async def test_reconnect_within_three_seconds_keeps_the_page(
    make_screen: Callable[..., Awaitable[ScreenRig]],
    decoder: Callable[[list[str]], Awaitable[list[float]]],
    record_property: Callable[[str, object], None],
) -> None:
    rig = await make_screen()
    first = await rig.connect()
    await first.first_frame()
    await first.send(t="mouse", kind="click", x=200, y=600)  # the page turns white
    await first.until(lambda: len(first.frames) >= 2)
    events_before = await rig.handle.page.evaluate("window.__log.length")
    assert events_before >= 1

    dropped = time.perf_counter()
    first.ws.transport.abort()  # the link dies without a goodbye
    ticket = rig.registry.mint_ticket(rig.session).ticket  # what POST /login-session returns
    second = await rig.connect(ticket=ticket)
    await second.first_frame()
    back_ms = (time.perf_counter() - dropped) * 1000
    print(f"\n[remote screen] reconnect to first frame: {back_ms:.0f} ms")
    record_property("reconnect_ms", round(back_ms))
    assert back_ms <= 3000
    # same page, same state: not reloaded, still white
    assert await rig.handle.page.evaluate("window.__log.length") == events_before
    (luma,) = await decoder([second.frames[0][1]["data"]])
    assert luma > 128
    assert rig.session.has_viewer


# ------------------------------------------------------------------------------------ input


async def test_mouse_keyboard_scroll_and_resize_reach_the_page(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    page = rig.handle.page
    client = await rig.connect()
    await client.first_frame()

    # typing: `char` writes; key down/up of a printable key does not write it a second time
    await client.send(t="mouse", kind="click", x=100, y=28)
    await client.send(t="key", kind="down", code="KeyA")
    await client.send(t="key", kind="char", text="a")
    await client.send(t="key", kind="up", code="KeyA")
    await client.send(t="key", kind="char", text="Пр")
    await client.send(t="key", kind="char", text="1")
    await _until(lambda: _field_is(page, "aПр1"))
    # control keys
    await client.send(t="key", kind="down", code="Backspace")
    await client.send(t="key", kind="up", code="Backspace")
    await _until(lambda: _field_is(page, "aПр"))

    # a link is followed by a click; the client is told where it is (host and path only)
    await client.send(t="mouse", kind="click", x=40, y=85)
    await client.until(lambda: any(m["url"].endswith("/screen/lab2") for m in client.of("state")))
    assert await page.title() == "Screen lab 2"
    assert all("?" not in m["url"] for m in client.of("state"))

    await page.go_back()
    await _until(lambda: _scrolled(page, lambda y: y == 0))
    await client.send(t="scroll", x=100, y=300, dx=0, dy=500)
    await _until(lambda: _scrolled(page, lambda y: y > 100))

    await client.send(t="resize", w=320, h=640)
    await _until(lambda: _size_is(page, (320, 640)))
    await client.until(lambda: client.frames[-1][1]["w"] == 320)
    assert client.frames[-1][1]["h"] == 640
    await client.send(t="resize", w=8000, h=8000)  # clamped, not obeyed blindly
    await _until(lambda: _size_is(page, (1920, 1920)))


async def test_enter_submits_a_form(make_screen: Callable[..., Awaitable[ScreenRig]]) -> None:
    rig = await make_screen(path="/plain/login")
    client = await rig.connect()
    await client.first_frame()
    page = rig.handle.page
    await page.focus("#u")
    for ch in data.USERNAME:
        await client.send(t="key", kind="char", text=ch)
    await _until(lambda: _values_are(page, data.USERNAME, ""))
    await page.focus("#p")
    for ch in data.PASSWORD:
        await client.send(t="key", kind="char", text=ch)
    await _until(lambda: _values_are(page, data.USERNAME, data.PASSWORD))
    await client.send(t="key", kind="down", code="Enter")
    await client.send(t="key", kind="up", code="Enter")
    await _until(lambda: _path_is(page, "/app"))


async def _until(condition: Callable[[], Awaitable[bool]], within: float = 5.0) -> None:
    deadline = time.perf_counter() + within
    while not await condition():
        if time.perf_counter() > deadline:
            raise TimeoutError("page did not reach the expected state")
        await asyncio.sleep(0.05)


async def _field_is(page: Any, expected: str) -> bool:
    return await page.input_value("#t") == expected


async def _scrolled(page: Any, ok: Callable[[float], bool]) -> bool:
    return ok(await page.evaluate("window.scrollY"))


async def _size_is(page: Any, expected: tuple[int, int]) -> bool:
    return tuple(await page.evaluate("[innerWidth, innerHeight]")) == expected


async def _values_are(page: Any, user: str, password: str) -> bool:
    return (await page.input_value("#u"), await page.input_value("#p")) == (user, password)


async def _path_is(page: Any, expected: str) -> bool:
    return await page.evaluate("location.pathname") == expected


# ------------------------------------------------------------------------ pacing and quality


async def test_a_slow_viewer_gets_the_newest_frame_not_a_queue(
    make_screen: Callable[..., Awaitable[ScreenRig]],
    decoder: Callable[[list[str]], Awaitable[list[float]]],
) -> None:
    rig = await make_screen()
    client = await rig.connect(auto_ack=False)
    await client.first_frame()
    for _ in range(5):  # the page flips five times: black, white, ..., white
        await client.send(t="mouse", kind="click", x=200, y=600)
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    assert len(client.frames) <= 2  # the window: nothing more until the viewer acknowledges
    for _, frame in list(client.frames):
        await client.ack(frame["seq"])
    seen = len(client.frames)
    await client.until(lambda: len(client.frames) > seen)
    latest = client.frames[-1][1]
    (luma,) = await decoder([latest["data"]])
    assert luma > 128  # five clicks: white, and it is the newest picture that arrived
    assert rig.session.stats.frames_dropped >= 1


def test_quality_drops_on_a_slow_link_and_recovers_on_a_fast_one() -> None:
    q = QualityController(max_fps=30, start_quality=65)
    assert q.profile.fps == 30
    now = 0.0
    changes = []
    for _ in range(80):  # every ack takes 300 ms
        now += 0.35
        if (change := q.observe(300, now)) is not None:
            changes.append(change)
    assert changes
    assert q.profile.quality == QualityController.MIN_QUALITY  # bytes first
    assert q.profile.fps == 10  # then frames, down to the floor
    low = q.profile
    for _ in range(600):  # the link recovers
        now += 0.2
        q.observe(10, now)
    assert q.profile.fps == 30
    assert q.profile.quality > low.quality


def test_a_lower_frame_rate_cap_is_respected() -> None:
    assert QualityController(max_fps=15).profile.fps == 15
    assert QualityController(max_fps=10).profile.fps == 10


def test_quality_changes_are_rare_and_need_evidence() -> None:
    q = QualityController()
    assert [q.observe(500, 0.1 * i) for i in range(3)] == [None, None, None]  # too little proof
    assert q.observe(500, 0.3) is not None  # the fourth slow ack is enough
    assert all(q.observe(500, 0.3 + 0.01 * i) is None for i in range(1, 20))  # then a pause
    assert q.observe(500, 1.5) is not None


def test_ticket_roundtrip_and_tampering() -> None:
    now = time.time()
    ticket = mint_ticket(SECRET, "S" * 26, "U" * 26, ttl_s=60, now=now)
    assert len(ticket) <= 200
    claims = verify_ticket(SECRET, ticket, now=now + 1)
    assert claims is not None
    assert (claims.session_id, claims.user_id) == ("S" * 26, "U" * 26)
    assert verify_ticket(SECRET, ticket, now=now + 61) is None
    assert verify_ticket(b"o" * 32, ticket, now=now) is None
    body, _, mac = ticket.partition(".")
    assert verify_ticket(SECRET, body + "." + mac[:-2] + "AA", now=now) is None
    assert verify_ticket(SECRET, "garbage", now=now) is None


# ---------------------------------------------------------------------- tickets and origin


async def refused(rig: ScreenRig, **kwargs: Any) -> int:
    with pytest.raises(InvalidStatus) as exc:
        await rig.connect(**kwargs)
    return exc.value.response.status_code


async def test_ticket_is_single_use_and_bound_to_its_session(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    ticket = rig.registry.mint_ticket(rig.session).ticket
    client = await rig.connect(ticket=ticket)
    await client.first_frame()
    assert await refused(rig, ticket=ticket) == 403  # used
    assert await refused(rig, ticket="nonsense") == 403
    other = await make_screen()
    foreign = other.registry.mint_ticket(other.session).ticket  # other secret and session
    assert await refused(rig, ticket=foreign) == 403
    # a genuine ticket for another session id
    assert (
        await refused(rig, ticket=rig.registry.mint_ticket(rig.session).ticket, session_id="X" * 26)
        == 403
    )


async def test_expired_ticket_is_refused(make_screen: Callable[..., Awaitable[ScreenRig]]) -> None:
    rig = await make_screen(ScreenConfig(ticket_ttl_s=0.3))
    ticket = rig.registry.mint_ticket(rig.session).ticket
    await asyncio.sleep(0.6)
    assert await refused(rig, ticket=ticket) == 403


async def test_origin_must_match_before_the_upgrade(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    for origin in ("https://evil.test", "http://localhost:5174", "http://localhost", None):
        assert await refused(rig, origin=origin) == 403, origin
    # the ticket was not spent by the refused attempts
    client = await rig.connect(ticket=rig.registry.mint_ticket(rig.session).ticket)
    await client.first_frame()


async def test_a_second_viewer_replaces_the_first(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    first = await rig.connect()
    await first.first_frame()
    second = await rig.connect()
    await second.first_frame()
    await first.until(lambda: first.close_code is not None)
    assert first.close_code == 4001
    assert second.close_code is None
    assert rig.session.has_viewer


async def test_login_session_endpoint_issues_tickets_to_the_owner_only(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rig.port}") as http:
        reply = await http.post(f"/api/v1/connections/{rig.conn}/login-session")
        assert reply.status_code == 201
        body = reply.json()
        assert set(body) == {"sessionId", "wsUrl", "ticket", "expiresAt"}
        assert body["sessionId"] == rig.session.id
        assert body["wsUrl"].startswith(
            f"ws://localhost:5173/api/v1/login-sessions/{rig.session.id}/ws?ticket="
        )
        assert body["wsUrl"].endswith(body["ticket"])
        # the ticket works on a real socket
        client = await rig.connect(ticket=body["ticket"])
        await client.first_frame()
        # a connection nobody has opened a screen for, and one that is not the user's
        unknown = await http.post(f"/api/v1/connections/{new_id()}/login-session")
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "not_found"


async def test_no_waiting_screen_means_not_found(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    await rig.session.close("done")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rig.port}") as http:
        reply = await http.post(f"/api/v1/connections/{rig.conn}/login-session")
    assert reply.status_code == 404


# ----------------------------------------------------------------------------- lifetime


async def test_the_session_expires_and_its_browser_is_destroyed(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen(ScreenConfig(ttl_s=1.2, max_ttl_s=1.2))
    client = await rig.connect()
    await client.first_frame()
    await client.until(lambda: client.close_code is not None, within=5)
    assert client.close_code == 4000
    assert client.of("expired")[0]["reason"] == "ttl"
    await _until(_handle_closed(rig.handle))
    assert (rig.worker or rig.registry).find(rig.user, rig.conn) is None


async def test_activity_extends_the_session_up_to_the_cap(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen(ScreenConfig(ttl_s=1.5, max_ttl_s=4.0))
    client = await rig.connect()
    await client.first_frame()
    started = time.perf_counter()
    while time.perf_counter() - started < 3.0:  # busy for twice the plain TTL
        await client.send(t="mouse", kind="move", x=50, y=50)
        await asyncio.sleep(0.3)
    assert client.close_code is None
    assert rig.handle.closed is False
    await client.until(lambda: client.close_code is not None, within=6)
    assert client.close_code == 4000  # the hard cap (4 s) or the idle TTL, whichever first


async def test_detaching_keeps_the_page_and_stops_the_stream(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    client = await rig.connect()
    await client.first_frame()
    await client.close()
    await asyncio.sleep(0.3)
    assert not rig.session.has_viewer
    assert rig.handle.closed is False
    assert (rig.worker or rig.registry).find(rig.user, rig.conn) is rig.session
    sent = rig.session.stats.frames_sent
    await rig.handle.page.evaluate("document.body.style.background = 'red'")
    await asyncio.sleep(0.4)
    assert rig.session.stats.frames_sent == sent  # nobody watching, nothing encoded for them


# ---------------------------------------------------------- the login flow's side of the link


async def test_done_needs_and_success_travel_through_the_link(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    client = await rig.connect()
    await client.first_frame()
    assert rig.session.take_done() is False
    await client.send(t="done")
    await _until(_taken(rig.session))
    assert rig.session.take_done() is False  # consumed once
    await rig.session.needs("captcha", "Пройдите проверку")
    await client.until(lambda: bool(client.of("needs")))
    assert client.of("needs")[0] == {"t": "needs", "what": "captcha", "hint": "Пройдите проверку"}
    # a viewer that reconnects hears what is still being asked
    again = await rig.connect()
    await again.until(lambda: bool(again.of("needs")))
    await rig.session.succeed()
    await again.until(lambda: again.close_code is not None)
    assert again.of("success") == [{"t": "success"}]
    assert again.close_code == 1000
    assert rig.session.closed


async def test_idle_time_is_measured_from_the_viewers_last_input(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen()
    assert rig.session.idle_s() == math.inf
    client = await rig.connect()
    await client.first_frame()
    await client.send(t="mouse", kind="move", x=10, y=10)
    await _until(_idle_below(rig.session, 1))
    await asyncio.sleep(0.4)
    assert 0.3 <= rig.session.idle_s() < 1.5


# -------------------------------------------------------------------------------- abuse


async def test_oversized_malformed_and_flooding_messages_are_dropped_then_the_viewer_is_closed(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen(ScreenConfig(max_violations=20))
    client = await rig.connect()
    await client.first_frame()
    await client.ws.send("x" * 9000)  # over 8 KiB
    await client.ws.send(b"binary")
    await client.ws.send('{"t":"launch-missiles"}')
    await client.ws.send("not json")
    await _until(_violations_at_least(rig.session, 4))
    assert client.close_code is None  # tolerated, only counted
    for _ in range(30):
        await client.ws.send('{"t":"nope"}')
    await client.until(lambda: client.close_code is not None)
    assert client.close_code == 1008


async def test_message_rate_is_capped(make_screen: Callable[..., Awaitable[ScreenRig]]) -> None:
    rig = await make_screen(ScreenConfig(max_messages_per_s=5, max_violations=10_000))
    client = await rig.connect()
    await client.first_frame()
    for i in range(600):
        await client.ws.send(json.dumps({"t": "mouse", "kind": "move", "x": i % 300, "y": 100}))
    await asyncio.sleep(0.5)
    assert rig.session.stats.violations > 0
    assert rig.session.stats.inputs < 600


# ------------------------------------------------------------------- nothing leaks


async def test_frames_and_input_are_not_logged(
    make_screen: Callable[..., Awaitable[ScreenRig]], caplog: pytest.LogCaptureFixture
) -> None:
    rig = await make_screen()
    with caplog.at_level(logging.DEBUG):
        client = await rig.connect()
        await client.first_frame()
        await client.send(t="mouse", kind="click", x=100, y=28)
        for ch in "TYPED-MARKER-7":
            await client.send(t="key", kind="char", text=ch)
        await _until(lambda: _field_is(rig.handle.page, "TYPED-MARKER-7"))
        await client.close()
        await rig.session.close("done")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TYPED-MARKER" not in text
    assert "/9j/" not in text  # no JPEG data
    assert rig.session.stats.frames_sent > 0
    assert "screen" in text  # lifecycle is logged, content is not


# --------------------------------------------------------------- the whole way, end to end


async def test_captcha_is_solved_by_a_human_through_the_real_screen(
    make_screen: Callable[..., Awaitable[ScreenRig]],
    gateway: PlaywrightGateway,
    egress_access: ProxyEgress,
    trust_fakeuni: Path,
) -> None:
    """LoginFlow hands the captcha page to the screen; a viewer ticks the box and submits."""
    registry = ScreenRegistry(secret=SECRET, public_base_url=PUBLIC)
    user, conn = new_id(), new_id()
    store = InMemoryLoginStore()
    store.add_connection(user, conn, ORIGIN)
    store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}/captcha/login"))
    flow = LoginFlow(
        gateway=gateway,
        vault=InMemoryVault(),
        store=store,
        egress=egress_access,
        screens=registry,
        timing=replace(FAST, human_wait_s=30.0),
    )
    async with serve(build_app(registry, store, user)) as port:
        ctx = InMemoryJobContext(user_id=user, connection_id=conn, responder=Student())
        login_task = asyncio.create_task(flow.ensure(ctx, user_id=user, connection_id=conn))
        viewer: ScreenClient | None = None
        try:
            while not any(isinstance(b, LoginBlock) for b in ctx.blocks):
                if login_task.done():
                    login_task.result()
                await asyncio.sleep(0.05)
            block = next(b for b in ctx.blocks if isinstance(b, LoginBlock))
            assert len(block.ticket) <= 200
            assert str(block.ws_url).startswith("ws://localhost:5173/api/v1/login-sessions/")
            session = registry.find(user, conn)
            assert session is not None
            # connect as the browser would (the dev server proxies localhost:5173 to us)
            url = str(block.ws_url).replace("localhost:5173", f"127.0.0.1:{port}")
            viewer = ScreenClient(await connect(url, origin=PUBLIC, max_size=None))
            await viewer.first_frame()
            page = session.handle.page
            checkbox = await page.frame_locator("iframe").locator("#human").bounding_box()
            submit = await page.locator("#go").bounding_box()
            assert checkbox is not None
            assert submit is not None
            await viewer.send(t="mouse", kind="click", x=checkbox["x"] + 6, y=checkbox["y"] + 6)
            await asyncio.sleep(0.4)
            await viewer.send(t="mouse", kind="click", x=submit["x"] + 10, y=submit["y"] + 10)
            result = await asyncio.wait_for(login_task, 20)
            await viewer.until(lambda: bool(viewer.of("success")), within=5)
            assert await works(result)
            assert viewer.close_code in (None, 1000)
        finally:
            if viewer is not None:
                await viewer.close()
            if not login_task.done():
                login_task.cancel()


def _taken(session: ScreenSession) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return session.take_done()

    return check


def _idle_below(session: ScreenSession, limit: float) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return session.idle_s() < limit

    return check


def _violations_at_least(session: ScreenSession, n: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return session.stats.violations >= n

    return check


def _handle_closed(handle: PlaywrightHandle) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return handle.closed

    return check


async def test_a_flood_of_mouse_moves_does_not_delay_the_click_behind_it(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    """Moves arrive faster than Chromium takes them: only the newest matters."""
    rig = await make_screen(ScreenConfig(max_messages_per_s=100_000))
    client = await rig.connect()
    await client.first_frame()
    before = await rig.handle.page.evaluate("window.__log.length")
    started = time.perf_counter()
    for i in range(400):
        await client.send(t="mouse", kind="move", x=10 + i % 300, y=200)
    await client.send(t="mouse", kind="click", x=200, y=600)
    await _until(lambda: _log_grew(rig.handle.page, before))
    took = time.perf_counter() - started
    print(f"\n[remote screen] click behind 400 queued moves handled after {took * 1000:.0f} ms")
    assert took < 0.6


async def _log_grew(page: Any, before: int) -> bool:
    return await page.evaluate("window.__log.length") > before


async def test_a_slow_link_lowers_the_picture_quality_and_a_fast_one_restores_it(
    make_screen: Callable[..., Awaitable[ScreenRig]],
) -> None:
    rig = await make_screen(path="/screen/lab?busy=1&anim=1")
    client = await rig.connect(auto_ack=False)
    start = rig.session._quality.profile
    pending: list[asyncio.Task[None]] = []

    async def slow_ack(seq: int) -> None:
        await asyncio.sleep(0.25)  # a link with a quarter second round trip
        with contextlib.suppress(Exception):
            await client.ack(seq)

    async def acker(seen: int) -> int:
        for _, frame in client.frames[seen:]:
            pending.append(asyncio.create_task(slow_ack(frame["seq"])))
        return len(client.frames)

    seen = 0
    for _ in range(80):  # about eight seconds
        await asyncio.sleep(0.1)
        seen = await acker(seen)
    slow = rig.session._quality.profile
    assert slow.quality < start.quality
    assert slow.fps < start.fps
    assert len(client.frames) > 5  # still moving, not stalled
    assert rig.session.stats.restarts >= 2

    # the link recovers: acknowledge at once
    client.auto_ack = True
    for _, frame in client.frames[seen:]:
        await client.ack(frame["seq"])
    await asyncio.sleep(7)
    fast = rig.session._quality.profile
    assert fast.fps > slow.fps
    for task in pending:
        task.cancel()
