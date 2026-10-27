"""LISTEN/NOTIFY wake-ups on a dedicated connection.

Postgres tells waiting processes that something happened (a job became runnable, a block was
answered, an event was appended, a job was cancelled). Every waiter also polls with a timeout,
so a lost notification or a dropped LISTEN connection only costs latency, never correctness.
Payloads are job ids; no user data travels through NOTIFY.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import asyncpg
from sqlalchemy.engine import make_url

log = logging.getLogger(__name__)

CHANNEL_JOBS = "botai_jobs"  # a job became runnable
CHANNEL_EVENTS = "botai_events"  # a job event was appended
CHANNEL_ANSWERS = "botai_answers"  # a block was answered
CHANNEL_CANCEL = "botai_cancel"  # a job was cancelled
CHANNELS = (CHANNEL_JOBS, CHANNEL_EVENTS, CHANNEL_ANSWERS, CHANNEL_CANCEL)

_RECONNECT_MIN = 0.5
_RECONNECT_MAX = 15.0


def dsn_for_asyncpg(database_url: str) -> str:
    """`postgresql+asyncpg://…` (SQLAlchemy) to the plain DSN asyncpg connects with."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class Notifier:
    """Fans NOTIFY payloads out to asyncio events. With no DSN it is inert and waiters just poll."""

    def __init__(self, dsn: str | None) -> None:
        self._dsn = dsn
        self._waiters: dict[tuple[str, str | None], set[asyncio.Event]] = {}
        self._callbacks: dict[str, list[Callable[[str], None]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def start(self, *, wait: float = 3.0) -> None:
        if self._dsn is None or self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="botai-notifier")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), wait)

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._ready.clear()

    @contextmanager
    def watch(self, channel: str, key: str | None = None) -> Generator[asyncio.Event]:
        """An event set when `channel` carries `key` (any payload when key is None).

        Register first, then look at the database, then wait: a notification that lands in
        between is not lost.
        """
        event = asyncio.Event()
        slot = (channel, key)
        self._waiters.setdefault(slot, set()).add(event)
        try:
            yield event
        finally:
            group = self._waiters.get(slot)
            if group is not None:
                group.discard(event)
                if not group:
                    del self._waiters[slot]

    def on(self, channel: str, callback: Callable[[str], None]) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    def _deliver(self, channel: str, payload: str) -> None:
        for key in (payload, None):
            for event in tuple(self._waiters.get((channel, key), ())):
                event.set()
        for callback in self._callbacks.get(channel, ()):
            try:
                callback(payload)
            except Exception:
                log.exception("notify callback for %s failed", channel)

    async def _run(self) -> None:
        assert self._dsn is not None
        delay = _RECONNECT_MIN
        while not self._stopping:
            conn: Any = None
            closed = asyncio.Event()

            def on_terminated(_conn: Any, closed: asyncio.Event = closed) -> None:
                closed.set()

            def on_notify(_conn: Any, _pid: int, channel: str, payload: str) -> None:
                self._deliver(channel, payload)

            try:
                conn = await asyncpg.connect(self._dsn, timeout=5)
                conn.add_termination_listener(on_terminated)
                for channel in CHANNELS:
                    await conn.add_listener(channel, on_notify)
                self._ready.set()
                delay = _RECONNECT_MIN
                # Wake everyone once: whatever was missed while we were not listening is now due.
                for group in tuple(self._waiters.values()):
                    for event in tuple(group):
                        event.set()
                await closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("notify connection failed: %s", type(exc).__name__)
            finally:
                self._ready.clear()
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(conn.close(timeout=2))
            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)


_notifiers: dict[str, Notifier] = {}


async def shared_notifier(dsn: str | None) -> Notifier:
    """One listener per process and database (the API process shares it between SSE streams)."""
    key = dsn or ""
    notifier = _notifiers.get(key)
    if notifier is None:
        notifier = _notifiers[key] = Notifier(dsn)
    await notifier.start()
    return notifier


async def stop_shared_notifiers() -> None:
    notifiers = list(_notifiers.values())
    _notifiers.clear()
    for notifier in notifiers:
        await notifier.stop()
