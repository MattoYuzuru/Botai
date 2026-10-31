"""The fallback remote screen: a CDP screencast of the server's Chromium to one viewer, and the
viewer's mouse, touch and keyboard back (contracts/events.md §2, ADR-0001 §3).

Targets: first frame <= 2 s after the connection, input -> frame p95 <= 150 ms on a steady link,
reconnection <= 3 s without losing the page. How:

* frames are JPEG straight from Chromium (base64 as it arrives, no re-encoding); the newest frame
  wins, a slow viewer never gets a queue of stale ones; the client's `ack` paces the sender;
* quality and frame rate adapt to the acknowledgement round trip (`QualityController`);
* the viewer is a thin socket: when it drops, the browser context lives on and a fresh ticket
  re-attaches to the same page.

Frames and input are never written to disk or logs: this module logs lifecycle events only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from websockets.asyncio.client import ClientConnection

from botai.browser.gateway import PlaywrightHandle, in_container
from botai.browser.proxy import derive_key
from botai.browser.screen_remote import RemoteScreens, ScreenInternalServer
from botai.core.clock import Clock, SystemClock
from botai.core.ids import new_id
from botai.settings import Settings

log = logging.getLogger(__name__)

# WebSocket close codes (application range)
CLOSE_EXPIRED = 4000
CLOSE_REPLACED = 4001
CLOSE_BAD_TICKET = 4401
CLOSE_BAD_ORIGIN = 4403
CLOSE_NO_SESSION = 4404
CLOSE_ABUSE = 1008
CLOSE_NORMAL = 1000


# --------------------------------------------------------------------------------------------
# Wire messages
# --------------------------------------------------------------------------------------------


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ts: float | None = None  # the client's clock; echoed in `frame.inputTs` for latency checks


class MouseMsg(_Msg):
    t: Literal["mouse"]
    kind: Literal["move", "down", "up", "click"]
    x: float = Field(ge=0, le=8192)
    y: float = Field(ge=0, le=8192)
    button: Literal["left", "middle", "right"] = "left"


class KeyMsg(_Msg):
    t: Literal["key"]
    kind: Literal["down", "up", "char"]
    code: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9_+-]*$")] = ""
    text: Annotated[str, Field(max_length=256)] = ""


class ScrollMsg(_Msg):
    t: Literal["scroll"]
    x: float = Field(ge=0, le=8192)
    y: float = Field(ge=0, le=8192)
    dx: float = Field(default=0, ge=-5000, le=5000)
    dy: float = Field(default=0, ge=-5000, le=5000)


class ResizeMsg(_Msg):
    t: Literal["resize"]
    w: int = Field(ge=1, le=8192)
    h: int = Field(ge=1, le=8192)


class AckMsg(_Msg):
    t: Literal["ack"]
    seq: int = Field(ge=0)


class DoneMsg(_Msg):
    t: Literal["done"]


ClientMessage = Annotated[
    MouseMsg | KeyMsg | ScrollMsg | ResizeMsg | AckMsg | DoneMsg, Field(discriminator="t")
]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

_BUTTONS = {"left": 1, "right": 2, "middle": 4}


class Viewer(Protocol):
    """The one party looking at the screen (a WebSocket in production)."""

    async def send(self, text: str) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...


# --------------------------------------------------------------------------------------------
# Adaptive quality
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    quality: int  # JPEG quality, 40..85 by default
    fps: int  # at most this many frames per second to the viewer


def _quality_limits(min_quality: int, start_quality: int, max_quality: int) -> tuple[int, int, int]:
    """Clamp configured JPEG quality values to a workable range: (min, start, max)."""
    low = max(1, min(100, min_quality))
    high = max(low, min(100, max_quality))
    return low, max(low, min(high, start_quality)), high


class QualityController:
    """Trades frame rate and picture quality against the viewer's acknowledgement round trip.

    The picture is what the person at the screen reads, so frame rate is the first lever: a slow
    link drops the frame rate step by step (which does not restart the screencast) and only at
    the frame-rate floor does the JPEG quality go down, never below `min_quality`. A fast link
    brings the frame rate back first and the picture last. Changes are rare (at most one per
    second) because a quality change restarts the screencast."""

    MIN_QUALITY = 40
    MAX_QUALITY = 85
    START_QUALITY = 75
    SLOW_MS = 120.0
    FAST_MS = 45.0
    FPS_STEPS = (30, 24, 20, 15, 12, 10)

    def __init__(
        self,
        *,
        max_fps: int = 30,
        start_quality: int = START_QUALITY,
        min_quality: int = MIN_QUALITY,
        max_quality: int = MAX_QUALITY,
    ) -> None:
        self._min_quality, self._start_quality, self._max_quality = _quality_limits(
            min_quality, start_quality, max_quality
        )
        self._steps = tuple(f for f in self.FPS_STEPS if f <= max_fps) or (max_fps,)
        if max_fps not in self._steps and max_fps > self._steps[0]:
            self._steps = (max_fps, *self._steps)
        self.profile = Profile(self._start_quality, self._steps[0])
        self._ewma: float | None = None
        self._samples = 0
        self._last_change = float("-inf")
        self._last_degrade = float("-inf")

    @property
    def rtt_ms(self) -> float | None:
        return self._ewma

    def _step(self, fps: int, direction: int) -> int:
        index = self._steps.index(fps) if fps in self._steps else 0
        return self._steps[max(0, min(len(self._steps) - 1, index + direction))]

    def observe(self, rtt_ms: float, now: float) -> Profile | None:
        """Feed one ack round trip. Returns the new profile when it should change."""
        self._ewma = rtt_ms if self._ewma is None else 0.7 * self._ewma + 0.3 * rtt_ms
        self._samples += 1
        if self._samples < 4 or now - self._last_change < 1.0:
            return None
        p = self.profile
        slower = self._step(p.fps, 1)
        better = self._step(p.fps, -1)
        if self._ewma > self.SLOW_MS:
            if slower != p.fps:  # frame rate first: the picture stays readable
                new = Profile(p.quality, slower)
            elif p.quality > self._min_quality:
                new = Profile(max(self._min_quality, p.quality - 10), p.fps)
            else:
                return None
            self._last_degrade = now
        elif self._ewma < self.FAST_MS and now - self._last_degrade > 3.0:
            if better != p.fps:  # frame rate first here too: smoothness comes back
                new = Profile(p.quality, better)
            elif p.quality < self._max_quality:
                new = Profile(min(self._max_quality, p.quality + 5), p.fps)
            else:
                return None
        else:
            return None
        self.profile = new
        self._last_change = now
        self._samples = 0
        return new


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    ttl_s: float = 600.0  # a screen session lives 10 minutes ...
    max_ttl_s: float = 1800.0  # ... extended by viewer activity up to 30
    ticket_ttl_s: float = 60.0
    max_fps: int = 30
    min_quality: int = QualityController.MIN_QUALITY
    start_quality: int = QualityController.START_QUALITY
    max_quality: int = QualityController.MAX_QUALITY
    window: int = 2  # frames in flight to the viewer
    max_message_bytes: int = 8 * 1024
    max_messages_per_s: float = 120.0
    max_violations: int = 100
    viewport_min: tuple[int, int] = (240, 240)
    viewport_max: tuple[int, int] = (1920, 1920)
    viewport_default: tuple[int, int] = (390, 760)
    stall_s: float = 2.0  # no ack for this long: assume the frames in flight were lost
    first_frame_wait_s: float = 0.3


def screen_config_from_env() -> ScreenConfig:
    """Quality limits of the remote screen, tunable without a rebuild.

    `BOTAI_SCREEN_MAX_FPS`, `BOTAI_SCREEN_MIN_QUALITY`, `BOTAI_SCREEN_START_QUALITY` and
    `BOTAI_SCREEN_MAX_QUALITY` override the defaults; an unusable value keeps the default
    (QualityController clamps the accepted ones into a workable range)."""

    def number(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            log.warning("ignoring %s=%r: not an integer", name, raw)
            return default

    return ScreenConfig(
        max_fps=max(1, number("BOTAI_SCREEN_MAX_FPS", 30)),
        min_quality=number("BOTAI_SCREEN_MIN_QUALITY", QualityController.MIN_QUALITY),
        start_quality=number("BOTAI_SCREEN_START_QUALITY", QualityController.START_QUALITY),
        max_quality=number("BOTAI_SCREEN_MAX_QUALITY", QualityController.MAX_QUALITY),
    )


# --------------------------------------------------------------------------------------------
# Tickets: signed, short-lived, single use
# --------------------------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class TicketClaims:
    session_id: str
    user_id: str
    expires_at: float
    nonce: str


def mint_ticket(key: bytes, session_id: str, user_id: str, *, ttl_s: float, now: float) -> str:
    body = _b64(
        json.dumps(
            {"s": session_id, "u": user_id, "e": int(now + ttl_s), "n": secrets.token_hex(6)},
            separators=(",", ":"),
        ).encode()
    )
    mac = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest()[:16])
    return f"{body}.{mac}"


def verify_ticket(key: bytes, ticket: str, *, now: float) -> TicketClaims | None:
    if not ticket.isascii():
        return None
    body, dot, mac = ticket.partition(".")
    if not dot or not body or not mac:
        return None
    expected = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest()[:16])
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        raw: Any = json.loads(_unb64(body))
        claims = TicketClaims(str(raw["s"]), str(raw["u"]), float(raw["e"]), str(raw["n"]))
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    return claims if now < claims.expires_at else None


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket: str
    ws_url: str
    expires_at: datetime


class ScreenError(Exception):
    """A refused attach. `code` is the WebSocket close code, `reason` a short phrase."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------------------------
# Keyboard
# --------------------------------------------------------------------------------------------

_VK: dict[str, int] = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "NumpadEnter": 13, "ShiftLeft": 16, "ShiftRight": 16,
    "ControlLeft": 17, "ControlRight": 17, "AltLeft": 18, "AltRight": 18, "Escape": 27,
    "Space": 32, "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36, "ArrowLeft": 37,
    "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Delete": 46, "MetaLeft": 91,
    "MetaRight": 92,
}  # fmt: skip
_MODIFIER_BIT = {
    "AltLeft": 1, "AltRight": 1, "ControlLeft": 2, "ControlRight": 2,
    "MetaLeft": 4, "MetaRight": 4, "ShiftLeft": 8, "ShiftRight": 8,
}  # fmt: skip
_KEY_NAME = {
    "Backspace": "Backspace", "Tab": "Tab", "Enter": "Enter", "NumpadEnter": "Enter",
    "Escape": "Escape", "Space": " ", "PageUp": "PageUp", "PageDown": "PageDown", "End": "End",
    "Home": "Home", "ArrowLeft": "ArrowLeft", "ArrowUp": "ArrowUp", "ArrowRight": "ArrowRight",
    "ArrowDown": "ArrowDown", "Delete": "Delete", "ShiftLeft": "Shift", "ShiftRight": "Shift",
    "ControlLeft": "Control", "ControlRight": "Control", "AltLeft": "Alt", "AltRight": "Alt",
    "MetaLeft": "Meta", "MetaRight": "Meta",
}  # fmt: skip


def virtual_key(code: str) -> int:
    if code in _VK:
        return _VK[code]
    if len(code) == 4 and code.startswith("Key"):
        return ord(code[3].upper())
    if len(code) == 6 and code.startswith("Digit"):
        return ord(code[5])
    if code.startswith("F") and code[1:].isdigit() and 1 <= int(code[1:]) <= 12:
        return 111 + int(code[1:])
    return 0


def _key_name(code: str) -> str:
    if code in _KEY_NAME:
        return _KEY_NAME[code]
    if len(code) == 4 and code.startswith("Key"):
        return code[3].lower()
    if len(code) == 6 and code.startswith("Digit"):
        return code[5]
    return code


# --------------------------------------------------------------------------------------------
# One screen
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Frame:
    data: str  # base64 JPEG as Chromium produced it
    input_ts: float | None


@dataclass(slots=True)
class ScreenStats:
    """Counters for tests and for a one-line summary at close. Never content."""

    frames_sent: int = 0
    bytes_sent: int = 0
    frames_dropped: int = 0
    inputs: int = 0
    violations: int = 0
    restarts: int = 0


class ScreenSession:
    """Implements browser.login.ScreenLink."""

    def __init__(
        self,
        *,
        session_id: str,
        user_id: str,
        connection_id: str,
        handle: PlaywrightHandle,
        config: ScreenConfig,
        on_closed: Callable[[ScreenSession], None],
    ) -> None:
        self.id = session_id
        self.user_id = user_id
        self.connection_id = connection_id
        self.handle = handle
        self._cfg = config
        self._on_closed = on_closed
        self.ticket = ""
        self.ws_url = ""
        self.expires_at = datetime.min
        self._closed = False
        self._close_reason: str | None = None
        self._cdp: CDPSession | None = None
        self._viewer: Viewer | None = None
        self._quality = QualityController(
            max_fps=config.max_fps,
            start_quality=config.start_quality,
            min_quality=config.min_quality,
            max_quality=config.max_quality,
        )
        self._viewport = config.viewport_default
        self._tasks: set[asyncio.Task[Any]] = set()
        self._latest: _Frame | None = None
        self._sending = False
        self._seq = 0
        self._sent_at: dict[int, float] = {}
        self._last_send = 0.0
        self._screencasting = False
        self._first_frame = asyncio.Event()
        self._pace_timer = False
        self._pending_move: MouseMsg | None = None
        self._pending_scroll: ScrollMsg | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._input_ts: float | None = None
        self._last_input = float("-inf")
        self._done = False
        self.human_control = False
        self._last_needs: tuple[str, str] | None = None
        self._modifiers = 0
        self._buttons = 0
        self._bucket = config.max_messages_per_s
        self._bucket_at = time.monotonic()
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._deadline = now + config.ttl_s
        self._hard_deadline = now + config.max_ttl_s
        self.stats = ScreenStats()
        self._ttl_task: asyncio.Task[None] | None = None
        self._page_listeners: list[tuple[Any, str, Callable[..., Any]]] = []

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._ttl_task = loop.create_task(self._watch_ttl())
        self.handle.on_close(self._handle_closed)
        self.handle.on_page_change(self._page_changed)
        await self._attach_page()
        log.info("screen %s opened for connection %s", self.id, self.connection_id)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _attach_page(self) -> None:
        page = self.handle.page
        self._cdp = await self.handle.context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", self._on_cdp_frame)
        for event in ("framenavigated", "load"):
            callback = self._on_page_event
            page.on(event, callback)  # type: ignore[arg-type]
            self._page_listeners.append((page, event, callback))
        with contextlib.suppress(PlaywrightError):
            await page.set_viewport_size({"width": self._viewport[0], "height": self._viewport[1]})

    def _detach_page(self) -> None:
        for page, event, callback in self._page_listeners:
            with contextlib.suppress(Exception):
                page.remove_listener(event, callback)
        self._page_listeners.clear()
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            self._screencasting = False
            self._spawn(_detach_cdp(cdp))

    def _page_changed(self) -> None:
        """A popup became the current page: follow it."""
        if self._closed:
            return
        self._detach_page()
        self._spawn(self._reattach_page())

    async def _reattach_page(self) -> None:
        await self._attach_page()
        if self._viewer is not None:
            await self._start_screencast()
            await self._send_state()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_reason(self) -> str | None:
        """Why the screen ended (`ttl`, `success`, `browser closed`, `mirrored`, ...).

        The login flow tells a screen that ran out of time from one that was closed on purpose,
        so a timeout becomes a clear error instead of a silent cancellation."""
        return self._close_reason

    @property
    def has_viewer(self) -> bool:
        return self._viewer is not None

    def _handle_closed(self) -> None:
        if not self._closed:
            self._spawn(self.close("browser closed", notify="expired"))

    async def close(self, reason: str, *, notify: str | None = None) -> None:
        """Stop the screen. The browser context belongs to the login flow and is left alone."""
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        viewer, self._viewer = self._viewer, None
        if viewer is not None:
            if notify == "expired":
                await _quietly(viewer.send(json.dumps({"t": "expired", "reason": reason})))
            await _quietly(
                viewer.close(CLOSE_EXPIRED if notify == "expired" else CLOSE_NORMAL, reason)
            )
        self._screencasting = False
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            await _detach_cdp(cdp)
        for page, event, callback in self._page_listeners:
            with contextlib.suppress(Exception):
                page.remove_listener(event, callback)
        self._page_listeners.clear()
        if self._ttl_task is not None and self._ttl_task is not asyncio.current_task():
            self._ttl_task.cancel()
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()
        self._on_closed(self)
        quality, rtt = self._quality.profile, self._quality.rtt_ms
        log.info(
            "screen %s closed (%s): %d frames, %d bytes, %d inputs, %d dropped, %d restarts, "
            "quality=%d fps=%d rtt=%s",
            self.id,
            reason,
            self.stats.frames_sent,
            self.stats.bytes_sent,
            self.stats.inputs,
            self.stats.frames_dropped,
            self.stats.restarts,
            quality.quality,
            quality.fps,
            "unknown" if rtt is None else f"{rtt:.0f}ms",
        )

    async def _watch_ttl(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            remaining = self._deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 1.0))
        log.info("screen %s timed out", self.id)
        await self._expire("ttl")

    async def _expire(self, reason: str) -> None:
        """The login session ran out: the screen and the browser context are destroyed."""
        await self.close(reason, notify="expired")
        await self.handle.close()

    def _touch(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_input = loop.time()
        self._deadline = min(
            self._hard_deadline, max(self._deadline, loop.time() + self._cfg.ttl_s)
        )

    # -- ScreenLink (what the login flow sees) ------------------------------------------------

    def idle_s(self) -> float:
        if self._last_input == float("-inf"):
            return float("inf")
        return max(0.0, asyncio.get_running_loop().time() - self._last_input)

    def take_done(self) -> bool:
        done, self._done = self._done, False
        return done

    async def needs(self, what: str, hint: str) -> None:
        self._last_needs = (what, hint)
        await self._send_json({"t": "needs", "what": what, "hint": hint[:500]})

    async def succeed(self) -> None:
        viewer, self._viewer = self._viewer, None
        if viewer is not None:
            await _quietly(viewer.send('{"t":"success"}'))
            await _quietly(viewer.close(CLOSE_NORMAL, "success"))
        await self.close("success")

    # -- the viewer -----------------------------------------------------------------------------

    async def attach(self, viewer: Viewer) -> None:
        """Make `viewer` the one viewer. A previous one (a half-open socket after a reconnect)
        is closed: the holder of a fresh ticket is the same person."""
        if self._closed:
            raise ScreenError(CLOSE_NO_SESSION, "session closed")
        previous, self._viewer = self._viewer, viewer
        if previous is not None:
            await _quietly(previous.close(CLOSE_REPLACED, "replaced by a new connection"))
        self.stats.violations = 0
        self._bucket = self._cfg.max_messages_per_s
        self._latest = None
        self._sent_at.clear()
        self._sending = False
        await self._start_screencast()
        await self._send_state()
        if self._last_needs is not None:
            await self._send_json(
                {"t": "needs", "what": self._last_needs[0], "hint": self._last_needs[1]}
            )

    async def detach(self, viewer: Viewer) -> None:
        if viewer is not self._viewer:
            return
        self._viewer = None
        self._latest = None
        await self._stop_screencast()

    # -- screencast ----------------------------------------------------------------------------

    async def _start_screencast(self) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        profile = self._quality.profile
        w, h = self._viewport
        self._first_frame.clear()
        try:
            if self._screencasting:
                await cdp.send("Page.stopScreencast")
            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": profile.quality,
                    "maxWidth": w,
                    "maxHeight": h,
                    "everyNthFrame": 1,  # more would swallow the single frame of a one-off change
                },
            )
        except PlaywrightError:
            return
        self._screencasting = True
        self.stats.restarts += 1
        self._spawn(self._ensure_first_frame())

    async def _stop_screencast(self) -> None:
        cdp = self._cdp
        if cdp is None or not self._screencasting:
            return
        self._screencasting = False
        with contextlib.suppress(PlaywrightError):
            await cdp.send("Page.stopScreencast")

    async def _ensure_first_frame(self) -> None:
        """An idle page may not repaint: if no frame arrives, take one screenshot."""
        try:
            await asyncio.wait_for(self._first_frame.wait(), self._cfg.first_frame_wait_s)
            return
        except TimeoutError:
            pass
        cdp = self._cdp
        if cdp is None or self._viewer is None:
            return
        with contextlib.suppress(PlaywrightError):
            shot = await cdp.send(
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": self._quality.profile.quality},
            )
            self._latest = _Frame(shot["data"], self._input_ts)
            self._first_frame.set()
            self._kick()

    def _on_cdp_frame(self, params: dict[str, Any]) -> None:
        cdp = self._cdp
        if cdp is not None:
            # acknowledge at once so Chromium keeps its own pace; frames the viewer cannot take
            # yet are replaced by newer ones below
            self._spawn(
                _quietly(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
            )
        self._first_frame.set()
        if self._viewer is None:
            return
        if self._latest is not None:
            self.stats.frames_dropped += 1
        self._latest = _Frame(params["data"], self._input_ts)
        self._kick()

    def _kick(self) -> None:
        viewer = self._viewer
        if viewer is None or self._latest is None or self._sending or self._closed:
            return
        now = time.monotonic()
        if len(self._sent_at) >= self._cfg.window:
            if now - self._last_send < self._cfg.stall_s:
                return
            self._sent_at.clear()  # the acknowledgements are lost: start over
        wait = self._last_send + 1.0 / self._quality.profile.fps - now
        if wait > 0:  # too soon: the newest frame goes out when the pace allows
            if not self._pace_timer:
                self._pace_timer = True
                asyncio.get_running_loop().call_later(wait, self._pace_elapsed)
            return
        frame, self._latest = self._latest, None
        self._seq += 1
        seq = self._seq
        self._sent_at[seq] = now
        self._last_send = now
        self._sending = True
        self._spawn(self._send_frame(viewer, seq, frame))

    def _pace_elapsed(self) -> None:
        self._pace_timer = False
        self._kick()

    async def _send_frame(self, viewer: Viewer, seq: int, frame: _Frame) -> None:
        w, h = self._viewport
        echo = "null" if frame.input_ts is None else repr(frame.input_ts)
        text = (
            f'{{"t":"frame","seq":{seq},"mime":"image/jpeg","w":{w},"h":{h},'
            f'"inputTs":{echo},"data":"{frame.data}"}}'
        )
        try:
            await viewer.send(text)
            self.stats.frames_sent += 1
            self.stats.bytes_sent += len(text)
        except Exception:  # the socket is gone; `detach` follows from the receive loop
            self._sent_at.pop(seq, None)
        finally:
            self._sending = False
            self._kick()

    async def _send_json(self, message: dict[str, Any]) -> None:
        viewer = self._viewer
        if viewer is not None:
            await _quietly(viewer.send(json.dumps(message, ensure_ascii=False)))

    async def _send_state(self) -> None:
        parts = urlsplit(self.handle.url)
        shown = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else self.handle.url
        try:
            title = await asyncio.wait_for(self.handle.page.title(), 1.0)
        except (PlaywrightError, TimeoutError):
            title = ""
        await self._send_json({"t": "state", "url": shown, "title": title[:200], "loading": False})

    def _on_page_event(self, *_: Any) -> None:
        if self._viewer is not None and not self._closed:
            self._spawn(self._send_state())

    # -- input ---------------------------------------------------------------------------------

    def _violation(self, why: str) -> bool:
        """Count a client mistake; True once the connection should be dropped."""
        self.stats.violations += 1
        return self.stats.violations >= self._cfg.max_violations

    async def handle_message(self, viewer: Viewer, raw: str | bytes) -> None:
        if viewer is not self._viewer or self._closed:
            return
        size = len(raw)
        if isinstance(raw, bytes) or size > self._cfg.max_message_bytes:
            if self._violation("oversize"):
                await self._abuse(viewer)
            return
        now = time.monotonic()
        self._bucket = min(
            self._cfg.max_messages_per_s * 2,
            self._bucket + (now - self._bucket_at) * self._cfg.max_messages_per_s,
        )
        self._bucket_at = now
        if self._bucket < 1:
            if self._violation("rate"):  # dropped, as the contract says; many drops close
                await self._abuse(viewer)
            return
        self._bucket -= 1
        try:
            message = _client_adapter.validate_json(raw)
        except ValidationError:
            if self._violation("malformed"):
                await self._abuse(viewer)
            return
        if message.ts is not None:
            self._input_ts = message.ts
        if isinstance(message, AckMsg):
            await self._on_ack(message.seq)
            return
        if isinstance(message, DoneMsg):
            self._done = True
            self._touch()
            return
        if isinstance(message, KeyMsg | ScrollMsg) or (
            isinstance(message, MouseMsg) and message.kind != "move"
        ):
            self.human_control = True
            if not isinstance(message, ScrollMsg):  # scrolling sends nothing: it needs no window
                await self.handle.arm_human_auth()
        self._touch()
        self.stats.inputs += 1
        if isinstance(message, MouseMsg) and message.kind == "move":
            self._queue_move(message)  # Chromium takes about one input per frame: keep the newest
            return
        if isinstance(message, ScrollMsg):
            self._queue_scroll(message)
            return
        try:
            await self._flush_pending()  # what the viewer did before this lands before it
            if isinstance(message, MouseMsg):
                await self._mouse(message)
            elif isinstance(message, KeyMsg):
                await self._key(message)
            else:
                await self._resize(message)
        except PlaywrightError:
            pass  # the page navigated under the event; the next one lands

    # Moves and wheel ticks can arrive faster than the page takes them (about one per frame).
    # They are merged while waiting: the last position wins, wheel deltas add up.

    def _queue_move(self, m: MouseMsg) -> None:
        self._pending_move = m
        self._ensure_drain()

    def _queue_scroll(self, s: ScrollMsg) -> None:
        if self._pending_scroll is not None:
            s = s.model_copy(
                update={
                    "dx": max(-5000, min(5000, s.dx + self._pending_scroll.dx)),
                    "dy": max(-5000, min(5000, s.dy + self._pending_scroll.dy)),
                }
            )
        self._pending_scroll = s
        self._ensure_drain()

    def _ensure_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.get_running_loop().create_task(self._drain())
            self._tasks.add(self._drain_task)
            self._drain_task.add_done_callback(self._tasks.discard)

    async def _drain(self) -> None:
        while not self._closed and await self._flush_pending():
            pass

    async def _flush_pending(self) -> bool:
        """Send the merged move and wheel events; True if there was anything."""
        move, self._pending_move = self._pending_move, None
        scroll, self._pending_scroll = self._pending_scroll, None
        try:
            if move is not None:
                await self._mouse(move)
            if scroll is not None:
                await self._scroll(scroll)
        except PlaywrightError:
            pass
        return move is not None or scroll is not None

    async def _abuse(self, viewer: Viewer) -> None:
        log.warning("screen %s: viewer closed for repeated protocol violations", self.id)
        self._viewer = None
        await _quietly(viewer.close(CLOSE_ABUSE, "too many violations"))
        await self._stop_screencast()

    async def _on_ack(self, seq: int) -> None:
        now = time.monotonic()
        sent = self._sent_at.get(seq)
        for key in [k for k in self._sent_at if k <= seq]:
            del self._sent_at[key]
        if sent is not None:
            before = self._quality.profile.quality
            change = self._quality.observe((now - sent) * 1000, now)
            if change is not None and change.quality != before and self._viewer is not None:
                await self._start_screencast()
        self._kick()

    async def _mouse(self, m: MouseMsg) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        bit = _BUTTONS[m.button]
        base = {"x": m.x, "y": m.y, "modifiers": self._modifiers}
        if m.kind == "move":
            await cdp.send(
                "Input.dispatchMouseEvent",
                {**base, "type": "mouseMoved", "buttons": self._buttons,
                 "button": "none" if not self._buttons else m.button},
            )  # fmt: skip
            return
        if m.kind in ("down", "click"):
            if m.kind == "click":
                await cdp.send(
                    "Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "button": "none"}
                )
            self._buttons |= bit
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    **base,
                    "type": "mousePressed",
                    "button": m.button,
                    "buttons": self._buttons,
                    "clickCount": 1,
                },
            )
        if m.kind in ("up", "click"):
            self._buttons &= ~bit
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    **base,
                    "type": "mouseReleased",
                    "button": m.button,
                    "buttons": self._buttons,
                    "clickCount": 1,
                },
            )

    async def _scroll(self, s: ScrollMsg) -> None:
        if self._cdp is not None:
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": s.x, "y": s.y, "deltaX": s.dx, "deltaY": s.dy,
                 "modifiers": self._modifiers},
            )  # fmt: skip

    async def _key(self, k: KeyMsg) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        if k.kind == "char":
            await self._type(k.text)
            return
        bit = _MODIFIER_BIT.get(k.code)
        if bit is not None:
            self._modifiers = (
                (self._modifiers | bit) if k.kind == "down" else (self._modifiers & ~bit)
            )
        printable = k.code.startswith(("Key", "Digit")) or k.code == "Space"
        shortcut = bool(self._modifiers & (1 | 2 | 4))
        if printable and not shortcut:
            return  # typing arrives as `char`; the key events would write the letter twice
        vk = virtual_key(k.code)
        event: dict[str, Any] = {
            "type": "keyUp" if k.kind == "up" else "rawKeyDown",
            "key": _key_name(k.code),
            "code": k.code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": self._modifiers,
        }
        if k.kind == "down" and k.code in ("Enter", "NumpadEnter"):
            event.update(type="keyDown", text="\r", unmodifiedText="\r")
        await cdp.send("Input.dispatchKeyEvent", event)

    async def _type(self, text: str) -> None:
        cdp = self._cdp
        if cdp is None or not text:
            return
        if len(text) == 1 and text.isascii() and text.isprintable():
            vk = ord(text.upper()) if text.isalnum() else 0
            common = {"key": text, "text": text, "unmodifiedText": text,
                      "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}  # fmt: skip
            await cdp.send("Input.dispatchKeyEvent", {**common, "type": "keyDown"})
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": text,
                                                       "windowsVirtualKeyCode": vk})  # fmt: skip
            return
        await cdp.send("Input.insertText", {"text": text})

    async def _resize(self, r: ResizeMsg) -> None:
        lo, hi = self._cfg.viewport_min, self._cfg.viewport_max
        w = max(lo[0], min(hi[0], r.w))
        h = max(lo[1], min(hi[1], r.h))
        if (w, h) == self._viewport:
            return
        self._viewport = (w, h)
        await self.handle.page.set_viewport_size({"width": w, "height": h})
        if self._viewer is not None:
            await self._start_screencast()


async def _quietly(awaitable: Awaitable[Any]) -> None:
    with contextlib.suppress(Exception):
        await awaitable


async def _detach_cdp(cdp: CDPSession) -> None:
    with contextlib.suppress(Exception):
        await cdp.send("Page.stopScreencast")
    with contextlib.suppress(Exception):
        await cdp.detach()


# --------------------------------------------------------------------------------------------
# Registry: implements browser.login.LoginScreens
# --------------------------------------------------------------------------------------------


class ScreenRegistry:
    """Open screens of this process, their tickets, and the Origin rule. Implements
    browser.login.LoginScreens."""

    def __init__(
        self,
        *,
        secret: bytes,
        public_base_url: str,
        config: ScreenConfig | None = None,
        clock: Clock | None = None,
        internal_bind: tuple[str, int] | None = None,
        remote_urls: Sequence[str] = (),
    ) -> None:
        self.secret = secret
        self.public_base_url = public_base_url
        self.config = config or ScreenConfig()
        self.clock = clock or SystemClock()
        self._sessions: dict[str, ScreenSession] = {}
        self._native: dict[tuple[str, str], Callable[[], Awaitable[bool]]] = {}
        self._internal_lock = asyncio.Lock()
        self._used: dict[str, float] = {}
        # api <-> worker link (screen_remote.py): this process serves its screens on
        # `internal_bind`, and looks for screens held by other processes at `remote_urls`
        link_key = derive_key(secret, "internal-link")
        self._internal_bind = internal_bind
        self._internal: ScreenInternalServer | None = None
        self.remote = RemoteScreens(remote_urls, link_key) if remote_urls else None
        self._link_key = link_key

    # -- sessions ----------------------------------------------------------------------------

    async def register_native(
        self, user_id: str, connection_id: str, request: Callable[[], Awaitable[bool]]
    ) -> Callable[[], None]:
        """Expose only an active native auth wait, never an arbitrary browser handle."""
        await self._serve_internal()
        key = (user_id, connection_id)
        self._native[key] = request

        def unregister() -> None:
            if self._native.get(key) is request:
                self._native.pop(key, None)

        return unregister

    async def request_native(self, user_id: str, connection_id: str) -> str | None:
        session = self.find(user_id, connection_id)
        if session is not None:
            session.human_control = True
            return session.id
        request = self._native.get((user_id, connection_id))
        if request is None or not await request():
            return None
        session = self.find(user_id, connection_id)
        if session is None:
            return None
        session.human_control = True
        return session.id

    async def open(
        self, handle: PlaywrightHandle, *, user_id: str, connection_id: str
    ) -> ScreenSession:
        """Start a screen on this browser handle (the login flow's LoginScreens.open)."""
        await self._serve_internal()
        existing = self.find(user_id, connection_id)
        if existing is not None:
            await existing.close("replaced by a newer screen")
        session = ScreenSession(
            session_id=new_id(),
            user_id=user_id,
            connection_id=connection_id,
            handle=handle,
            config=self.config,
            on_closed=self._forget,
        )
        self._sessions[session.id] = session
        try:
            await session.start()
        except BaseException:
            self._sessions.pop(session.id, None)
            raise
        offer = self.mint_ticket(session)
        session.ticket, session.ws_url, session.expires_at = (
            offer.ticket,
            offer.ws_url,
            offer.expires_at,
        )
        return session

    def _forget(self, session: ScreenSession) -> None:
        self._sessions.pop(session.id, None)

    async def _serve_internal(self) -> None:
        async with self._internal_lock:
            if self._internal is not None or self._internal_bind is None:
                return
            server = ScreenInternalServer(self, self._link_key, *self._internal_bind)
            try:
                await server.start()
            except OSError:
                log.warning("internal screen server could not bind %s:%d", *self._internal_bind)
                self._internal_bind = None
                return
            self._internal = server

    @property
    def internal_port(self) -> int | None:
        return self._internal.port if self._internal is not None else None

    async def lookup(
        self, user_id: str, connection_id: str, *, request_native: bool = False
    ) -> str | None:
        """The id of the screen this connection's login waits on: here, or in another process."""
        session = self.find(user_id, connection_id)
        if session is not None:
            if request_native:
                session.human_control = True
            return session.id
        if request_native:
            session_id = await self.request_native(user_id, connection_id)
            if session_id is not None:
                return session_id
        return (
            await self.remote.find(user_id, connection_id, request_native=request_native)
            if self.remote
            else None
        )

    async def connect_remote(self, session_id: str, user_id: str) -> ClientConnection:
        """A socket to the process that holds `session_id`, or ScreenError(no such session)."""
        upstream = await self.remote.connect(session_id, user_id) if self.remote else None
        if upstream is None:
            raise ScreenError(CLOSE_NO_SESSION, "no such session")
        return upstream

    def find(self, user_id: str, connection_id: str) -> ScreenSession | None:
        for session in self._sessions.values():
            if (
                session.user_id == user_id
                and session.connection_id == connection_id
                and not session.closed
            ):
                return session
        return None

    def get(self, session_id: str) -> ScreenSession | None:
        session = self._sessions.get(session_id)
        return session if session is not None and not session.closed else None

    # -- tickets -----------------------------------------------------------------------------

    def ws_base(self) -> str:
        parts = urlsplit(self.public_base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}"

    def mint_ticket(self, session: ScreenSession) -> Ticket:
        return self.mint_ticket_for(session.id, session.user_id)

    def mint_ticket_for(self, session_id: str, user_id: str) -> Ticket:
        now = self.clock.now()
        raw = mint_ticket(
            self.secret, session_id, user_id, ttl_s=self.config.ticket_ttl_s, now=now.timestamp()
        )
        url = f"{self.ws_base()}/api/v1/login-sessions/{session_id}/ws?ticket={raw}"
        return Ticket(raw, url, now + timedelta(seconds=self.config.ticket_ttl_s))

    def spend(self, ticket: str | None, session_id: str) -> TicketClaims:
        """Spend a ticket. Raises ScreenError for anything but a fresh, unused ticket that was
        issued for this very session."""
        now = self.clock.now().timestamp()
        for nonce, expires in list(self._used.items()):
            if expires < now:
                del self._used[nonce]
        claims = verify_ticket(self.secret, ticket or "", now=now)
        if claims is None or claims.session_id != session_id or claims.nonce in self._used:
            raise ScreenError(CLOSE_BAD_TICKET, "invalid ticket")
        self._used[claims.nonce] = claims.expires_at
        return claims

    def redeem(self, ticket: str | None, session_id: str) -> ScreenSession:
        """`spend` for a screen held by this process."""
        claims = self.spend(ticket, session_id)
        session = self.get(session_id)
        if session is None or session.user_id != claims.user_id:
            raise ScreenError(CLOSE_NO_SESSION, "no such session")
        return session

    def allowed_origin(self, origin: str | None) -> bool:
        if not origin:
            return False
        want = urlsplit(self.public_base_url)
        got = urlsplit(origin)
        return (got.scheme, got.netloc.lower()) == (want.scheme, want.netloc.lower())

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close("shutdown")
        if self._internal is not None:
            await self._internal.close()
            self._internal = None


# --------------------------------------------------------------------------------------------
# The process-wide registry
# --------------------------------------------------------------------------------------------

_registry: ScreenRegistry | None = None


def screen_secret(settings: Settings) -> bytes:
    """The key tickets are signed with. `BOTAI_SCREEN_SECRET`, else derived from the Vault key
    (the API and the worker share it), else, outside prod, a public development constant."""
    explicit = os.environ.get("BOTAI_SCREEN_SECRET", "")
    if len(explicit) >= 32:
        return derive_key(explicit.encode(), "screen-ticket")
    kek = settings.botai_kek.get_secret_value()
    if kek:
        return derive_key(kek.encode(), "screen-ticket")
    if settings.env == "prod":
        raise RuntimeError("BOTAI_SCREEN_SECRET (or BOTAI_KEK) is required in prod")
    return derive_key(b"botai-insecure-dev-screen-secret", "screen-ticket")


def _internal_bind() -> tuple[str, int] | None:
    """Where the worker serves its screens to the API: `BOTAI_SCREEN_BIND=host:port` or `off`.
    Inside a container the API reaches the worker over the Docker network, so all interfaces;
    elsewhere loopback."""
    raw = os.environ.get("BOTAI_SCREEN_BIND", "")
    if raw.lower() == "off":
        return None
    if raw:
        host, _, port = raw.rpartition(":")
        return host or "127.0.0.1", int(port)
    return ("0.0.0.0" if in_container() else "127.0.0.1"), 8765  # noqa: S104 - private network


def _remote_urls() -> list[str]:
    """Where the API looks for screens held by the worker: `BOTAI_SCREEN_INTERNAL_URL=url[,url]`
    or `off`. By default the same host (development) and the compose service `worker`."""
    raw = os.environ.get("BOTAI_SCREEN_INTERNAL_URL", "")
    if raw.lower() == "off":
        return []
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return ["http://127.0.0.1:8765", "http://worker:8765"]


def get_screens(settings: Settings) -> ScreenRegistry:
    global _registry
    if _registry is None:
        _registry = ScreenRegistry(
            secret=screen_secret(settings),
            public_base_url=settings.public_base_url,
            config=screen_config_from_env(),
            internal_bind=_internal_bind(),
            remote_urls=_remote_urls(),
        )
    return _registry


def reset_screens() -> None:
    """For tests: forget the process-wide registry."""
    global _registry
    _registry = None
