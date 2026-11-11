"""The run's browser, launched on first use (D-044 §5.4, docs/v4/15-runtime-wiring.md).

A chat turn that answers from the student's synced data never touches a page, yet launching a
Chromium context (and restoring the saved agent session into it) cost every turn seconds. The
wiring therefore hands the tools a `LazyBrowser`: nothing is launched until the first tool really
needs the browser.

* `browser_*` tools are wrapped in `LaunchFirst`: the browser is up before the inner tool runs, so
  the tools keep using the control's synchronous members (`url`, `network_log()`, HAR) unchanged.
* `secret_use` (a browser field) and `action_execute` (browser steps) receive `handle()`: a stand-in
  that launches the browser on the first awaited call and then is the control itself.
* the saved agent session is loaded from Vault inside the launch, so it is still injected into the
  very first context; a run that never launched has nothing to save and nothing to close.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from botai.agent.v4.protocol import ToolArguments, ToolContext, ToolHandler, ToolResult
from botai.browser.control import BrowserControl

log = logging.getLogger(__name__)

Launch = Callable[[], Awaitable[BrowserControl]]


class LazyBrowser:
    """One browser control per run, created by `launch` on the first `get()` (concurrency-safe)."""

    def __init__(self, launch: Launch | None = None, *, control: BrowserControl | None = None):
        if launch is None and control is None:
            raise ValueError("LazyBrowser needs a launch function or a ready control")
        self._launch = launch
        self._control = control
        self._lock = asyncio.Lock()
        self.launches = 0

    @property
    def control(self) -> BrowserControl | None:
        """The control when it exists (launched or injected); never launches."""
        return self._control

    async def get(self) -> BrowserControl:
        if self._control is not None:
            return self._control
        async with self._lock:
            if self._control is None:
                assert self._launch is not None
                self._control = await self._launch()
                self.launches += 1
        return self._control

    def handle(self) -> BrowserControl:
        """A stand-in for the control that launches it on the first awaited call."""
        return cast(BrowserControl, _BrowserHandle(self))


class _BrowserHandle:
    """Delegates to the live control; before the launch every member is an async launcher.

    Only the async members of `BrowserControl` may be the first thing a holder touches: the holders
    (`secret_use`, the action executor) call `type`/`navigate`/`click`/… first, and the `browser_*`
    tools are launched by `LaunchFirst` before they run.
    """

    def __init__(self, lazy: LazyBrowser) -> None:
        self._lazy = lazy

    def __getattr__(self, name: str) -> Any:
        live = self._lazy.control
        if live is not None:
            return getattr(live, name)

        async def deferred(*args: Any, **kwargs: Any) -> Any:
            control = await self._lazy.get()
            return await getattr(control, name)(*args, **kwargs)

        return deferred


class LaunchFirst:
    """A `browser_*` tool whose browser is launched right before its first call."""

    def __init__(self, inner: ToolHandler, browser: LazyBrowser) -> None:
        self._inner = inner
        self._browser = browser
        self.spec = inner.spec

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        try:
            await self._browser.get()
        except Exception as exc:  # the exception text may hold a path or a URL: the type only
            log.warning("browser launch failed: %s", type(exc).__name__)
            return ToolResult.failure(
                "browser_unavailable",
                f"Браузер не запустился ({type(exc).__name__}). Попробуй ещё раз или обойдись "
                "без браузера (http_request, source_read_live).",
                retryable=True,
            )
        return await self._inner.run(arguments, context)


__all__ = ["LaunchFirst", "LazyBrowser"]
