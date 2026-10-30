"""Read-only browser execution of an explicitly rendered HTML recipe (ADR-0009) and of a page that
a single-page application draws with its scripts (the agent's `browse_source`).

The gateway owns egress, session isolation and the human-browser lease. This reader owns only its
short-lived background handle. Raw DOM is returned in memory for the existing CSS interpreter;
it is never logged, stored or sent to a model during sync.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from botai.browser.forms import CaptchaPage, ErrorPage, HardBlock, detect, has_credential_fields
from botai.browser.probe import requires_authentication
from botai.browser.read_capabilities import ReadEffectError
from botai.browser.session import bearer_expired
from botai.contracts.browser_plan import BrowserPlan
from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot, auth_origin
from botai.core.errors import BotaiError, ErrorCode
from botai.core.read_capabilities import ReadCapabilityBrowser
from botai.sources.adapter import Problem, SessionExpired, SyncContext
from botai.sources.extract import collapse, parse_html, select_items
from botai.sources.session import known_expired
from botai.sources.templates import render

MAX_RENDERED_PAGES = 3
MAX_RENDERED_SECONDS = 30.0
SELECTOR_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.2
MAX_RENDERED_HTML = 2 * 1024 * 1024
# A page drawn by scripts (`read_page`): how long an app that has drawn nothing is waited for after
# the load, how long its text must stand still, and what counts as a page with next to no text.
PAGE_WAIT_SECONDS = 10.0
PAGE_QUIET_SECONDS = 0.5
PAGE_IDLE_SECONDS = 2.0  # a thin page that asked its site for nothing is final after this long
SHELL_TEXT_CHARS = 160
MAX_PAGE_DATA = 8
_DATA_TYPES = frozenset({"xhr", "fetch"})


class RenderedPageError(Exception):
    problem: Problem

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """A page after its scripts ran: the DOM and the requests it made to draw itself."""

    snapshot: PageSnapshot
    requests: tuple[ObservedRequest, ...]
    # Successful GET JSON answers of the page's own origin (a sample each, credentials already
    # dropped by the gateway): what an app fetched to fill itself. Oldest first.
    data: tuple[ObservedRequest, ...]
    drawn: bool  # the app showed text or fetched data before the time ran out


def visible_text(markup: str | bytes) -> str:
    """The text of a page without scripts and styles, as one line."""
    return _text_of(LexborHTMLParser(markup))


def _text_of(tree: LexborHTMLParser) -> str:
    for bad in tree.css("script,style,noscript,template"):
        bad.decompose()
    return collapse(tree.body.text(separator=" ", strip=True)) if tree.body is not None else ""


def is_app_shell(markup: str | bytes) -> bool:
    """Markup that draws nothing by itself: next to no text, and scripts (or the notice that they
    are needed) that draw it in a browser. A single-page application before it runs looks so."""
    tree = LexborHTMLParser(markup)
    scripted = tree.css_first("script,noscript") is not None
    return scripted and len(_text_of(tree)) < SHELL_TEXT_CHARS


class RenderedPageReader:
    """One fetch-wide budget, shared by all rendered endpoints and their pagination."""

    def __init__(self, ctx: SyncContext, *, seconds: float | None = None) -> None:
        self.ctx = ctx
        self._pages = 0
        self._deadline = asyncio.get_running_loop().time() + (seconds or MAX_RENDERED_SECONDS)

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
        async with self._reading(url) as handle:
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
            status = _document_status(snapshot, await handle.network_log())
            if status in (401, 403):
                raise SessionExpired("rendered page refused the session")
            if status in (404, 410):
                raise RenderedPageError("not_found")
            if status is not None and status >= 400:
                raise RenderedPageError("status")
            return snapshot

    async def read_page(self, url: str) -> RenderedPage:
        """A page the way a student sees it: open it in the read-only background browser with the
        student's session, wait until the app has drawn itself (its text stands still, the data it
        asked for has arrived) or the time is up, and give back the DOM with what it fetched. A
        page that sends the student back to the login, or whose own requests are all refused, is
        an expired session."""
        async with self._reading(url) as handle:
            snapshot = await handle.goto(url)
            snapshot, requests = await self._until_drawn(handle, snapshot)
            status = _document_status(snapshot, requests)
            if status in (401, 403):
                raise SessionExpired("rendered page refused the session")
            if status is not None and status >= 400:
                raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, f"Источник ответил HTTP {status}")
            origin = auth_origin(self.ctx.origin)
            own = [r for r in requests if r.resource_type in _DATA_TYPES and _is_own(r, origin)]
            data = _page_data(own)
            if not data and any(r.status == 401 for r in own):
                raise SessionExpired("the requests of the page were refused", status=401)
            drawn = bool(data) or len(visible_text(snapshot.html)) >= SHELL_TEXT_CHARS
            return RenderedPage(snapshot, tuple(requests), tuple(data), drawn)

    @contextlib.asynccontextmanager
    async def _reading(self, url: str) -> AsyncGenerator[BrowserHandle]:
        """A short-lived read-only handle in the background (it never pushes out the user's login
        or research browser), closed within the fetch-wide budget."""
        loop = asyncio.get_running_loop()
        if self._pages >= MAX_RENDERED_PAGES or loop.time() >= self._deadline:
            raise BotaiError(
                ErrorCode.SOURCE_RATE_LIMITED, "Исчерпан лимит чтения страниц источника"
            )
        self._pages += 1
        if _origin(url) != self.ctx.origin:
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
                yield handle
        except TimeoutError:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Страница не успела загрузиться"
            ) from None
        finally:
            if handle is not None:
                # Cleanup failure must not replace the original source/egress error.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.close(), timeout=1.0)

    async def _until_drawn(
        self, handle: BrowserHandle, snapshot: PageSnapshot
    ) -> tuple[PageSnapshot, list[ObservedRequest]]:
        """Poll until the text of the page has stood still for a moment, nothing the app asked of
        its own site is still on the way, and there is something to read (text, or data it
        fetched). A thin page that asked for nothing is final after `PAGE_IDLE_SECONDS`; an app
        that is still on the way, or a request that never comes back, is waited for
        `PAGE_WAIT_SECONDS`, not for ever."""
        loop = asyncio.get_running_loop()
        give_up = min(loop.time() + PAGE_WAIT_SECONDS, self._deadline - 1.0)
        origin = auth_origin(self.ctx.origin)
        previous, still_since = "", loop.time()
        while True:
            self._check_page(snapshot)
            requests = await handle.network_log()
            text = visible_text(snapshot.html)
            now = loop.time()
            if text != previous:
                previous, still_since = text, now
            # A POST of the page never completes (the read-only guard refuses it): only its GETs
            # are waited for.
            reads = [
                r
                for r in requests
                if r.method == "GET" and r.resource_type in _DATA_TYPES and _is_own(r, origin)
            ]
            still = now - still_since
            if (
                still >= PAGE_QUIET_SECONDS
                and all(r.status is not None for r in reads)
                and (
                    len(text) >= SHELL_TEXT_CHARS
                    or _page_data(reads)
                    or (not reads and still >= PAGE_IDLE_SECONDS)
                )
            ) or now >= give_up:
                return snapshot, requests
            await asyncio.sleep(min(POLL_SECONDS, max(0.0, give_up - now)))
            snapshot = await handle.snapshot()

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


def _is_own(request: ObservedRequest, origin: str | None) -> bool:
    return origin is not None and auth_origin(request.url) == origin


def _document_status(snapshot: PageSnapshot, requests: Sequence[ObservedRequest]) -> int | None:
    """The status the server gave to the page itself (the newest document request for it)."""
    for request in reversed(requests):
        if request.resource_type == "document" and request.url == snapshot.url:
            return request.status
    return None


def _page_data(own: Sequence[ObservedRequest]) -> list[ObservedRequest]:
    """Successful GET JSON answers among the requests an app made to its own site, one per
    address. POST and other methods never get here: the read-only browser does not send them."""
    found: dict[str, ObservedRequest] = {}
    for request in own:
        if (
            request.method == "GET"
            and request.status is not None
            and 200 <= request.status < 300
            and request.response_sample
            and "json" in (request.response_content_type or "").lower()
        ):
            found.setdefault(request.url, request)
    return list(found.values())[:MAX_PAGE_DATA]
