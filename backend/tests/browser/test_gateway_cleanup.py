"""Cancellation of a caller must not abandon the browser context or its pool admission."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import BrowserContext, Page

from botai.browser.egress import Egress
from botai.browser.gateway import CaptureConfig, PlaywrightGateway, PlaywrightHandle
from botai.core.clock import SystemClock


@pytest.mark.parametrize("from_reaper", [False, True])
async def test_cancelled_close_caller_keeps_one_cleanup_and_releases_one_slot(
    from_reaper: bool,
) -> None:
    gateway = PlaywrightGateway(cast(Egress, Mock()), max_contexts=1, sandbox=False)
    started, finish = asyncio.Event(), asyncio.Event()
    cancelled: list[bool] = []

    async def close_context() -> None:
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    context = Mock(spec=BrowserContext)
    context.close = AsyncMock(side_effect=close_context)
    handle = PlaywrightHandle(
        gateway=gateway,
        context=cast(BrowserContext, context),
        page=cast(Page, Mock()),
        user_id="student",
        connection_id="source",
        allow_hosts=("fakeuni.test",),
        origin="https://fakeuni.test",
        capture=CaptureConfig(),
        clock=SystemClock(),
    )
    await gateway._slots.acquire()
    gateway._handles.add(handle)
    gateway._by_user[handle.user_id] = handle
    callbacks: list[bool] = []
    handle.on_close(lambda: callbacks.append(True))
    try:
        if from_reaper:
            handle.start_reaper(0)
            await asyncio.wait_for(started.wait(), timeout=1)
        caller = asyncio.create_task(handle.close())
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(caller, timeout=0.02)
        assert started.is_set()
        assert not cancelled
        assert handle in gateway._handles
        assert callbacks == []
        finish.set()
        await asyncio.wait_for(handle.close(), timeout=1)
        if handle._reaper is not None:
            await handle._reaper
        await handle.close()
        context.close.assert_awaited_once()
        assert callbacks == [True]
        assert not gateway._handles
        assert gateway.active_for(handle.user_id) is None
        await asyncio.wait_for(gateway._slots.acquire(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(gateway._slots.acquire(), timeout=0.02)
    finally:
        finish.set()
        await handle.close()
