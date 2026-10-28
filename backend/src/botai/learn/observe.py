"""Shared redacted observation shaping and the deterministic fallback crawl.

Starting from the home page, the observer follows a bounded number of same-origin menu links with
the user's session, collects page snapshots and the network log, and turns them into a redacted
bundle for the prompt. The adaptive reference-only controller lives in `learn.discovery`;
both paths use the same bounded, redacted bundle builder below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.browser.probe import requires_authentication
from botai.browser.session import ObservationSecretBrowser
from botai.contracts.specs import Kind
from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot
from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode
from botai.learn.addresses import navigation_url
from botai.learn.redact import Redactor, collect_values
from botai.sources.extract import node_text

log = logging.getLogger(__name__)

MAX_PAGES = 6
MAX_CALLS = 30
MAX_LINKS_PER_PAGE = 12
MAX_BODY_CHARS = 2500
SESSION_CAPTURE_SECONDS = 2.0
_STATIC = re.compile(
    r"(?i)\.(?:css|js|mjs|map|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|webp|pdf|zip)(?:\?|$)"
)
_SKIP_LINK = re.compile(
    r"(?i)logout|log-out|signout|sign-out|/exit|delete|remove|unsubscribe|выйти|выход|удалить|"
    r"отписаться|password|парол|(?:^|[/_.?=&-])(?:submit|save|create|edit|pay|purchase|"
    r"отправить|сохранить|создать|изменить|оплатить)(?:$|[/_.?=&-])"
)
_MENU = "nav a[href], header a[href], aside a[href], [role=navigation] a[href], .menu a[href], .sidebar a[href]"
_SKIP_TYPES = ("image/", "font/", "text/css", "javascript", "video/", "audio/")
_LOGIN_REQUIRED = re.compile(
    r"(?i)вы не вошли|для продолжения.{0,40}войдите|необходимо (?:войти|авторизоваться)|"
    r"you (?:are not|aren't) (?:logged|signed) in|please (?:log|sign) in|login required"
)


@dataclass(slots=True)
class PageView:
    url: str
    title: str
    links: list[tuple[str, str]]
    html: str
    reference: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class CallView:
    method: str
    url: str
    status: int | None
    content_type: str
    body: str
    reference: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class Observations:
    origin: str
    pages: list[PageView] = field(default_factory=list)
    calls: list[CallView] = field(default_factory=list)
    observed: set[str] = field(default_factory=set, repr=False)
    injections: int = 0
    plain_text: str = ""  # redacted visible text of the pages, for light keyword checks
    auth_required: bool = False
    bot_protection: bool = False
    discovered_kinds: tuple[Kind, ...] = ()
    planner_tokens: int = 0
    limited: bool = False
    stop_reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.pages and not self.calls

    def render(self, limit: int = 30_000) -> str:
        """The bundle as the model sees it, wrapped so that it reads as data."""
        parts = [f'<observation origin="{self.origin}">']
        headers: list[str] = []
        for page in self.pages:
            links = "; ".join(f"{text} -> {path}" for text, path in page.links)
            headers.append(
                f"[page] {page.url[:600]} | title: {page.title}\n"
                + (f"  links: {links[:400]}\n" if links else "")
            )
        for call in self.calls:
            headers.append(
                f"[request] {call.method} {call.url[:600]} -> {call.status} {call.content_type}\n"
            )
        # Every entry has a share after header costs. A large SPA cannot hide all later XHRs,
        # and the bundle never cuts a JSON document halfway through a field.
        entries = max(1, len(self.pages) + len(self.calls))
        share = max(
            0, (limit - len(parts[0]) - sum(map(len, headers)) - entries * 12 - 30) // entries
        )
        for i, page in enumerate(self.pages):
            parts.append(headers[i].rstrip())
            html = page.html if len(page.html) <= share else Redactor().html(page.html, share)
            parts.append(f"  html: {html}")
        for i, call in enumerate(self.calls):
            parts.append(headers[len(self.pages) + i].rstrip())
            if call.body and share >= 40:
                body = call.body
                if len(body) > share:
                    body = body_view(Redactor(), body, share)
                parts.append(f"  {body}")
        parts.append("</observation>")
        text = "\n".join(parts)
        diagnostic_event(
            "observe.render",
            page_count=len(self.pages),
            call_count=len(self.calls),
            input_chars=sum(len(p.html) for p in self.pages) + sum(len(c.body) for c in self.calls),
            output_chars=len(text),
            limit=limit,
            share_chars=share,
            truncated=any(len(p.html) > share for p in self.pages)
            or any(len(c.body) > share for c in self.calls),
            outcome="ok" if len(text) <= limit else "budget_exceeded",
        )
        # Very small caller budgets may not fit even the headers. Do not expose broken data.
        return (
            text
            if len(text) <= limit
            else parts[0] + "\n[observation budget exceeded]\n</observation>"
        )


# --- links ----------------------------------------------------------------------------------


def menu_links(
    html: str, base_url: str, origin: str, *, limit: int = MAX_LINKS_PER_PAGE
) -> list[tuple[str, str]]:
    """Same-origin links worth visiting: the menu first, then the rest, without destructive ones."""
    tree = LexborHTMLParser(html)
    ordered: list[LexborNode] = list(tree.css(_MENU)) + list(tree.css("a[href]"))
    seen = {_url_key(base_url)}
    out: list[tuple[str, str]] = []
    for node in ordered:
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if href.startswith("#") and not href.startswith(("#/", "#!/")):
            continue
        # Hash routes are SPA navigation, not an authentication fragment. The validator
        # rejects credential fragments while preserving an observed #/courses route.
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
            continue
        if navigation_url(absolute) is None:
            continue
        label = node_text(node)
        if _SKIP_LINK.search(absolute) or _SKIP_LINK.search(label):
            continue
        key = _url_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        out.append((label[:60], absolute))
        if len(out) >= limit:
            break
    return out


def _url_key(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    # Different selected weeks/pages are distinct; ordering of the same query is not.
    path = (parts.path or "/") + (f"#{parts.fragment}" if parts.fragment else "")
    return path, tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))


# --- the observer ---------------------------------------------------------------------------


async def _visit(handle: BrowserHandle, url: str) -> PageSnapshot | None:
    try:
        return await handle.goto(url)
    except Exception as exc:
        log.info("observation skipped a page: %s", type(exc).__name__)
        diagnostic_event("observe.visit_failed", reason="navigation_failed", **url_facts(url))
        return None


async def protect_observation(handle: BrowserHandle, redactor: Redactor) -> None:
    """Refresh the in-memory secret deny-set before any DOM/response is shaped for a prompt."""
    try:
        async with asyncio.timeout(SESSION_CAPTURE_SECONDS):
            if isinstance(handle, ObservationSecretBrowser):
                values = await handle.observation_secrets()
                redactor.protect_values(value.get_secret_value() for value in values)
            else:
                state = await handle.export_session()
                redactor.protect_session(state)
    except Exception:
        diagnostic_event("observe.network_failed", reason="network_log_failed")
        raise BotaiError(
            ErrorCode.SOURCE_UNAVAILABLE, "Не удалось безопасно прочитать состояние браузера"
        ) from None


def body_view(redactor: Redactor, sample: str | None, limit: int = MAX_BODY_CHARS) -> str:
    if not sample:
        return ""
    text = sample.strip()
    if text[:1] in "{[":
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if doc is not None:
            return redactor.json_text(doc, limit)
    if "<" in text[:200] and ">" in text[:400]:
        return redactor.html(text, min(1500, limit))
    return redactor.text(text, min(600, limit)) or ""


def interesting_call(call: ObservedRequest, origin: str) -> bool:
    parts = urlsplit(call.url)
    if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
        return False
    ctype = (call.response_content_type or "").lower()
    return not any(skip in ctype for skip in _SKIP_TYPES)


def _call_priority(call: ObservedRequest) -> int:
    """Useful successful record responses outrank envelopes and repeated background noise."""
    score = int(call.resource_type in {"xhr", "fetch"})
    if call.status is None or not 200 <= call.status < 300:
        return score - 3
    sample = (call.response_sample or "").strip()
    if sample[:1] not in "{[":
        return score
    try:
        doc = json.loads(sample)
    except (ValueError, RecursionError):
        return score

    def has_records(value: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, list):
            return any(isinstance(item, dict) and bool(item) for item in value[:3])
        if isinstance(value, dict):
            return any(has_records(item, depth + 1) for item in list(value.values())[:40])
        return False

    return score + (4 if has_records(doc) else 0)


async def observe(
    handle: BrowserHandle,
    origin: str,
    redactor: Redactor,
    *,
    start_urls: list[str],
    max_pages: int = MAX_PAGES,
    max_requests: int | None = None,
) -> Observations:
    """Visit the start pages and the menu links they lead to, then read the network log."""
    started = time.monotonic()
    diagnostic_event("observe.start", entry_count=len(start_urls), limit=max_pages)
    snapshots: list[tuple[str, PageSnapshot]] = []
    visited: set[str] = set()
    queue = list(start_urls)
    failed_count = 0
    stop_reason = ""

    async def network() -> list[ObservedRequest] | None:
        nonlocal stop_reason
        try:
            return await handle.network_log()
        except Exception:
            stop_reason = "network_log_failed"
            diagnostic_event("observe.network_failed", reason=stop_reason)
            return None

    while queue and len(snapshots) < max_pages:
        if max_requests is not None:
            current_log = await network()
            if current_log is None:
                break
            if len(current_log) >= max_requests:
                stop_reason = "request_budget"
                break
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page_index = len(snapshots)
        diagnostic_event("observe.page", phase="requested", page_index=page_index, **url_facts(url))
        snap = await _visit(handle, url)
        if snap is None:
            failed_count += 1
            continue
        await protect_observation(handle, redactor)
        diagnostic_event(
            "observe.page",
            phase="final",
            page_index=page_index,
            html_chars=len(snap.html),
            text_chars=len(snap.text),
            form_count=len(snap.forms),
            captcha_suspected=snap.captcha_suspected,
            login_required=bool(_LOGIN_REQUIRED.search(snap.text or _plain(snap.html))),
            redirected=snap.url != url,
            **url_facts(snap.url),
        )
        snapshots.append((url, snap))
        if requires_authentication(snap) or snap.captcha_suspected:
            break
        if max_requests is not None:
            current_log = await network()
            if current_log is None:
                break
            if len(current_log) >= max_requests:
                stop_reason = "request_budget"
                break
        if len(snapshots) == 1 or len(queue) < max_pages:
            for _, link in menu_links(snap.html, snap.url or url, origin):
                if link not in visited and link not in queue:
                    queue.append(link)
    log_entries = await network()
    await protect_observation(handle, redactor)
    obs = observations_of(
        origin,
        redactor,
        snapshots,
        log_entries or [],
        visited_count=len(visited),
        queued_count=len(queue),
        started=started,
    )
    if not obs.auth_required and not obs.bot_protection:
        if not stop_reason and failed_count:
            stop_reason = "navigation_failed"
        if not stop_reason and queue and len(snapshots) >= max_pages:
            stop_reason = "page_budget"
        obs.limited = bool(stop_reason)
        obs.stop_reason = stop_reason
    return obs


def observations_of(
    origin: str,
    redactor: Redactor,
    snapshots: list[tuple[str, PageSnapshot]],
    log_entries: list[ObservedRequest],
    *,
    visited_count: int = 0,
    queued_count: int = 0,
    started: float | None = None,
    page_refs: tuple[str, ...] = (),
    call_refs: Mapping[tuple[str, str], str] | None = None,
) -> Observations:
    """Build the same redacted bundle from legacy crawling or adaptive browser actions."""
    started = time.monotonic() if started is None else started
    obs = Observations(
        origin=origin,
        auth_required=any(requires_authentication(snap) for _, snap in snapshots),
        bot_protection=any(snap.captcha_suspected for _, snap in snapshots),
    )
    # URLs first: that is where identifiers are met for the first time and get their pseudonyms
    for _, snap in snapshots:
        redactor.url(snap.url, origin)
    seen_calls: set[tuple[str, tuple[str, tuple[tuple[str, str], ...]]]] = set()
    kept: list[ObservedRequest] = []
    filtered_count = duplicate_count = processed_count = 0
    for call in sorted(log_entries, key=_call_priority, reverse=True):
        processed_count += 1
        if not interesting_call(call, origin):
            filtered_count += 1
            continue
        key = (call.method, _url_key(call.url))
        if key in seen_calls:
            duplicate_count += 1
            continue
        seen_calls.add(key)
        redactor.url(call.url, origin)
        redactor.observed.update(raw_json_values(call.response_sample))
        kept.append(call)
        if len(kept) >= MAX_CALLS:
            break

    texts: list[str] = []
    for index, (_, snap) in enumerate(snapshots):
        links = [
            (redactor.text(label, 60) or "", redactor.url(link, origin))
            for label, link in menu_links(snap.html, snap.url, origin)
        ]
        title = redactor.text(snap.title, 100) or ""
        obs.pages.append(
            PageView(
                url=redactor.url(snap.url, origin),
                title=title,
                links=links,
                html=redactor.html(snap.html),
                reference=page_refs[index] if index < len(page_refs) else None,
            )
        )
        texts.append(redactor.text(snap.text or _plain(snap.html), 1500) or "")
    for call in kept:
        body = body_view(redactor, call.response_sample)
        diagnostic_event(
            "observe.call",
            method=call.method if call.method in {"GET", "HEAD", "OPTIONS", "POST"} else "OTHER",
            resource_type=call.resource_type
            if call.resource_type in {"document", "xhr", "fetch"}
            else "other",
            status=call.status,
            has_sample=call.response_sample is not None,
            sample_chars=len(call.response_sample or ""),
            body_chars=len(body),
            body_reduced=len(body) < len(call.response_sample or ""),
            **url_facts(call.url),
        )
        obs.calls.append(
            CallView(
                method=call.method,
                url=redactor.url(call.url, origin),
                status=call.status,
                content_type=(call.response_content_type or "").split(";")[0],
                body=body,
                reference=call_refs.get((call.method, call.url)) if call_refs is not None else None,
            )
        )
    obs.observed = set(redactor.observed)
    obs.injections = redactor.injections
    obs.plain_text = "\n".join(t for t in texts if t)
    diagnostic_event(
        "observe.complete",
        page_count=len(obs.pages),
        call_count=len(obs.calls),
        considered_count=len(log_entries),
        filtered_count=filtered_count,
        duplicate_count=duplicate_count,
        unexamined_count=len(log_entries) - processed_count,
        failed_count=max(0, visited_count - len(snapshots)),
        queued_count=queued_count,
        sample_missing_count=sum(c.response_sample is None for c in kept),
        injection_count=obs.injections,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        outcome="empty" if obs.empty else "ok",
    )
    return obs


def _plain(html: str) -> str:
    tree = LexborHTMLParser(html)
    for bad in tree.css("script,style"):
        bad.decompose()
    body = tree.body
    return body.text(separator=" ", strip=True) if body is not None else ""


def raw_json_values(sample: str | None) -> list[str]:
    """Scalars of a response sample, for the lint's observed set (kept apart from the prompt)."""
    if not sample or sample.strip()[:1] not in "{[":
        return []
    try:
        return list(collect_values(json.loads(sample)))
    except (ValueError, RecursionError):
        return []
