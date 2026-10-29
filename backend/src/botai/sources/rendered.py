"""Read-only browser execution of an explicitly rendered HTML recipe (ADR-0009).

The gateway owns egress, session isolation and the human-browser lease. This reader owns only its
short-lived background handle. Raw DOM is returned in memory for the existing CSS interpreter;
it is never logged, stored or sent to a model during sync.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit

from botai.browser.forms import CaptchaPage, ErrorPage, HardBlock, detect, has_credential_fields
from botai.browser.probe import requires_authentication
from botai.browser.read_capabilities import ReadEffectError
from botai.browser.session import bearer_expired
from botai.contracts.browser_plan import BrowserPlan
from botai.core.browser import BrowserHandle, PageSnapshot, auth_origin
from botai.core.errors import BotaiError, ErrorCode
from botai.core.read_capabilities import ReadCapabilityBrowser
from botai.sources.adapter import Problem, SessionExpired, SyncContext
from botai.sources.extract import parse_html, select_items
from botai.sources.session import known_expired
from botai.sources.templates import render

MAX_RENDERED_PAGES = 3
MAX_RENDERED_SECONDS = 30.0
SELECTOR_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.2
MAX_RENDERED_HTML = 2 * 1024 * 1024


class RenderedPageError(Exception):
    problem: Problem

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem)
        self.problem = problem


class RenderedPageReader:
    """One fetch-wide budget, shared by all rendered endpoints and their pagination."""

    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx
        self._pages = 0
        self._deadline = asyncio.get_running_loop().time() + MAX_RENDERED_SECONDS

    async def read_many(
        self,
        url: str,
        items_selector: str,
        *,
        plan: BrowserPlan | None = None,
        values: Mapping[str, str],
    ) -> list[PageSnapshot]:
        if plan is None:
            return [await self.read(url, items_selector)]
        remaining = MAX_RENDERED_PAGES - self._pages
        if remaining <= 0 or asyncio.get_running_loop().time() >= self._deadline:
            raise RenderedPageError(cast(Problem, "limit"))
        if _origin(url) != self.ctx.origin:
            raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Страница не принадлежит источнику")
        if self.ctx.browser is None:
            raise BotaiError(ErrorCode.RECIPE_REJECTED, "Чтение страницы в браузере недоступно")
        plan = BrowserPlan.model_validate(plan.model_dump(by_alias=True))
        if plan.pagination is not None:
            plan.pagination.max_pages = min(plan.pagination.max_pages, remaining)
        reserved_pages = plan.pagination.max_pages if plan.pagination else 1
        self._pages += reserved_pages
        resolved = dict(values)
        for step in plan.steps:
            if step.op == "set_date" and step.value is not None:
                resolved[step.value] = render(
                    step.value, now=self.ctx.now, tz=self.ctx.tz, params=values
                )
        handle: BrowserHandle | None = None
        self._check_session()
        if self.ctx.before_request is not None:
            await self.ctx.before_request()
        try:
            async with asyncio.timeout_at(self._deadline - 1.0):
                handle = await self.ctx.browser.open(
                    user_id=self.ctx.user_id,
                    connection_id=self.ctx.connection_id,
                    allow_hosts=self.ctx.allow_hosts,
                    session=self.ctx.session,
                    read_only=True,
                    background=True,
                )
                snapshot = await handle.goto(url)
                self._check_page(snapshot)
                if not isinstance(handle, ReadCapabilityBrowser):
                    raise BotaiError(
                        ErrorCode.RECIPE_REJECTED,
                        "Источник требует неподдерживаемых действий браузера",
                    )
                snapshots = await handle.replay_browser_plan(
                    plan, values=resolved, items_selector=items_selector
                )
                if not snapshots or len(snapshots) > reserved_pages:
                    raise RenderedPageError(cast(Problem, "limit"))
                for snapshot in snapshots:
                    self._check_page(snapshot)
                self._pages -= reserved_pages - len(snapshots)
                return snapshots
        except ReadEffectError as error:
            reason = error.reason if error.reason in {"limit", "no_progress"} else "selector"
            raise RenderedPageError(cast(Problem, reason)) from None
        except TimeoutError:
            raise RenderedPageError(cast(Problem, "limit")) from None
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.close(), timeout=1.0)

    async def read(self, url: str, items_selector: str) -> PageSnapshot:
        loop = asyncio.get_running_loop()
        if self._pages >= MAX_RENDERED_PAGES or loop.time() >= self._deadline:
            raise BotaiError(
                ErrorCode.SOURCE_RATE_LIMITED, "Исчерпан лимит чтения страниц источника"
            )
        self._pages += 1
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self.ctx.origin:
            raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Страница не принадлежит источнику")
        if self.ctx.browser is None:
            raise BotaiError(ErrorCode.RECIPE_REJECTED, "Чтение страницы в браузере недоступно")
        self._check_session()
        if self.ctx.before_request is not None:
            await self.ctx.before_request()
        handle: BrowserHandle | None = None
        try:
            # Leave cleanup inside the fetch-wide time budget as well.
            async with asyncio.timeout_at(self._deadline - 1.0):
                handle = await self.ctx.browser.open(
                    user_id=self.ctx.user_id,
                    connection_id=self.ctx.connection_id,
                    allow_hosts=self.ctx.allow_hosts,
                    session=self.ctx.session,
                    read_only=True,
                    background=True,
                )
                snapshot = await handle.goto(url)
                ready_at = min(loop.time() + SELECTOR_WAIT_SECONDS, self._deadline - 1.0)
                while True:
                    self._check_page(snapshot)
                    if (
                        select_items(parse_html(snapshot.html), items_selector)
                        or loop.time() >= ready_at
                    ):
                        break
                    await asyncio.sleep(min(POLL_SECONDS, max(0.0, ready_at - loop.time())))
                    snapshot = await handle.snapshot()
                for request in reversed(await handle.network_log()):
                    if request.resource_type == "document" and request.url == snapshot.url:
                        if request.status in (401, 403):
                            raise SessionExpired("rendered page refused the session")
                        if request.status in (404, 410):
                            raise RenderedPageError("not_found")
                        if request.status is not None and request.status >= 400:
                            raise RenderedPageError("status")
                        break
                return snapshot
        except TimeoutError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Страница не успела загрузиться"
            ) from None
        finally:
            if handle is not None:
                # Cleanup failure must not replace the original source/egress error.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.close(), timeout=1.0)

    def _check_session(self) -> None:
        state = self.ctx.session
        if state is None:
            return
        if (
            known_expired(state, self.ctx.now)
            or any(
                scope.origin == auth_origin(self.ctx.origin)
                and bearer_expired(scope.token, now=self.ctx.now, expires_hint=scope.expires_hint)
                for scope in state.bearer_scopes
            )
            or (
                state.bearer is not None
                and auth_origin(state.origin) == auth_origin(self.ctx.origin)
                and bearer_expired(state.bearer, now=self.ctx.now)
            )
        ):
            raise SessionExpired("rendered session is past its expiry hint", code="auth_missing")

    def _check_page(self, snapshot: PageSnapshot) -> None:
        if snapshot.url.startswith("chrome-error:"):
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Страница источника не загрузилась")
        parts = urlsplit(snapshot.url)
        if f"{parts.scheme}://{parts.netloc}" != self.ctx.origin or any(
            parts.path.startswith(prefix) for prefix in self.ctx.expired.redirect_path_prefixes
        ):
            raise SessionExpired("rendered page redirected to login")
        if len(snapshot.html) > MAX_RENDERED_HTML:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Страница источника слишком большая")
        found = detect(snapshot)
        if isinstance(found, ErrorPage):
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Страница источника не загрузилась")
        if isinstance(found, (CaptchaPage, HardBlock)):
            raise BotaiError(
                ErrorCode.SOURCE_BOT_PROTECTION, "Источник просит пройти проверку в обычном входе"
            )
        tree = parse_html(snapshot.html)
        if (
            requires_authentication(snapshot)
            or has_credential_fields(snapshot)
            or tree.css_first("input[type=password]") is not None
        ):
            raise SessionExpired("rendered page requires login")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
