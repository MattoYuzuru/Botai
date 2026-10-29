"""Verified native read controls and portable replay; no model-authored DOM interaction.

The gateway must retain its existing read-only request/WS guards. This registry never grants
network permissions. Every operation rechecks native semantics, ownership and a current handle.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard, cast
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, JSHandle, Page, Request, Response
from playwright.async_api import Error as PlaywrightError

from botai.browser.forms import CaptchaPage, HardBlock, detect, has_credential_fields
from botai.browser.pagescan import MAX_HTML as MAX_SNAPSHOT_HTML
from botai.browser.pagescan import SCAN_JS, build_snapshot
from botai.browser.probe import requires_authentication
from botai.browser.read_views import UNSAFE_CONTROL_LABEL
from botai.contracts.browser_plan import (
    BrowserPagination,
    BrowserPlan,
    BrowserStep,
    ReadFrame,
    ReadTarget,
)
from botai.core.browser import PageSnapshot
from botai.core.errors import BotaiError, ErrorCode
from botai.core.read_capabilities import (
    ReadActionResult,
    ReadCapability,
    ReadCapabilityKind,
    ReadValue,
)

WAIT_SECONDS = 5.0
QUIET_SECONDS = 0.3
MAX_CONTROLS = 24
MAX_HTML = 2 * 1024 * 1024
DATE_VALUES = {
    "today": "{today}",
    "weekStart": "{weekStart}",
    "previousWeek": "{minusDays:7}",
    "nextWeek": "{plusDays:7}",
    "twoWeeks": "{plusDays:14}",
}
_DATE_TEMPLATE = re.compile(r"^\{(today|weekStart|(?:plusDays|minusDays):\d{1,3})\}$")
_DATE_LITERAL = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_HELPERS = r"""
const selector = el => {
  if (el.id && document.querySelectorAll('#'+CSS.escape(el.id)).length === 1)
    return '#'+CSS.escape(el.id);
  const path = []; let node = el;
  while (node && node.nodeType === 1 && path.length < 24) {
    const tag = node.tagName.toLowerCase();
    const peers = node.parentElement ? [...node.parentElement.children].filter(p=>p.tagName===node.tagName) : [node];
    path.unshift(tag+':nth-of-type('+(peers.indexOf(node)+1)+')'); node=node.parentElement;
  }
  const value=path.join(' > ');
  return value.length<=300 && document.querySelectorAll(value).length===1 ? value : null;
};
const visible = el => {
  const style=getComputedStyle(el), box=el.getBoundingClientRect();
  return el.isConnected && el.ownerDocument===document && style.display!=='none' &&
    style.visibility==='visible' && box.width>0 && box.height>0;
};
const label = el => (el.getAttribute('aria-label') ||
  (el.labels && [...el.labels].map(l=>l.innerText).join(' ')) || el.innerText ||
  el.getAttribute('title') || '').replace(/\s+/g,' ').trim();
const target = el => {
  const path=selector(el), isControl=el.matches('input,select,a,[role=tab]');
  const name=isControl || el.getAttribute('role')==='status' ? label(el) : (el.getAttribute('aria-label')||el.getAttribute('title')||'');
  if (!path || name.length>120) return null;
  return {selector:path, tag:el.tagName.toLowerCase(), role:el.getAttribute('role')||'',
    label:name, controls:(el.getAttribute('aria-controls')||'').trim()};
};
const region = el => {
  const id=(el.getAttribute('aria-controls')||'').trim();
  if (!id || /\s/.test(id) || id.length>120) return null;
  const found=document.getElementById(id);
  const tabPanel=el.getAttribute('role')==='tab' && found && found.getAttribute('role')==='tabpanel';
  return found && (visible(found)||tabPanel) && !found.closest('form') ? target(found) : null;
};
const facts = el => {
  if (!visible(el) || el.closest('form') || el.disabled || el.readOnly ||
      el.getAttribute('aria-disabled')==='true' || el.isContentEditable) return null;
  const t=target(el); if (!t) return null;
  const tag=t.tag, role=t.role, owner=region(el);
  if (role==='tab' && owner && owner.role==='tabpanel' &&
      !el.matches('input,textarea,select,button[type=submit]') &&
      !el.querySelector('input,textarea,select,button,a[href],[contenteditable=true]') &&
      (!el.getAttribute('href') || el.getAttribute('href').startsWith('#')))
    return {kind:'activate_tab',target:t,region:owner};
  if (tag==='input' && el.type==='date' && owner && t.label)
    return {kind:'set_date',target:t,region:owner,type:'date',min:el.min,max:el.max};
  if (tag==='select' && !el.multiple && owner && t.label) {
    const options=[...el.options].filter(o=>!o.disabled && !o.closest('optgroup[disabled]') && o.value && o.label.length<=120 && o.value.length<=120);
    if (!options.length || options.length>40) return null;
    return {kind:'select_option',target:t,region:owner,options:options.map(o=>({value:o.value,label:o.label}))};
  }
  if (tag==='a' && (el.getAttribute('rel')||'').split(/\s+/).includes('next') &&
      !el.hasAttribute('download') && el.href)
    return {kind:'next_link',target:t,region:owner,href:el.href};
  if (el.id && (['region','tabpanel','grid','table'].includes(role) || ['main','section'].includes(tag)))
    return {kind:'wait_ready',target:t,region:t};
  return null;
};
"""
_SCAN = (
    "() => {"
    + _HELPERS
    + "return [...document.querySelectorAll('input[type=date],select,[role=tab],a[rel~=next],[role=region],[role=tabpanel],[role=grid],main[id],section[id]')].slice(0,256).map(facts).filter(Boolean).slice(0,24); }"
)
_FACTS = "el => {" + _HELPERS + "return facts(el); }"
_TARGET = "el => {" + _HELPERS + "return visible(el) && !el.closest('form') ? target(el) : null; }"
_HIDDEN_REGION_TARGET = (
    "el => {"
    + _HELPERS
    + "return el.isConnected && el.ownerDocument===document && !el.closest('form') ? target(el) : null; }"
)
_FRAME = (
    "el => {"
    + _HELPERS
    + "if(el.tagName!=='IFRAME'||!visible(el)||el.closest('form'))return null; const path=selector(el);if(!path)return null;return {selector:path,title:el.title||'',name:el.name||''};}"
)
_MONITOR = """() => { const state={revision:0}; state.observer=new MutationObserver(()=>state.revision++); state.observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true}); return state; }"""
_EMPTY = (
    "scope => {"
    + _HELPERS
    + "const root=scope?document.querySelector(scope):(document.querySelector('main')||document.body);if(!root)return [];return [...root.querySelectorAll('[role=status]')].filter(visible).filter(el=>/^(нет занятий|нет данных|ничего не найдено|no classes|no data|no results|nothing found)[.!]?$/i.test(label(el))).map(target).filter(Boolean); }"
)


class ReadEffectError(Exception):
    """Static execution verdict; no page values, selectors or URLs in exception text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _date_value(template: str, values: Mapping[str, str], now: datetime) -> str:
    match = _DATE_TEMPLATE.fullmatch(template)
    if match is None:
        raise ReadEffectError("unsupported_value")
    name = match[1]
    supplied = values.get(template, values.get(name))
    if supplied is not None:
        result = supplied
    else:
        day = now.date()
        if name == "weekStart":
            day -= timedelta(days=day.weekday())
        elif ":" in name:
            mode, raw = name.split(":")
            days = int(raw)
            if not 1 <= days <= 366:
                raise ReadEffectError("unsupported_value")
            day += timedelta(days=days if mode == "plusDays" else -days)
        result = day.isoformat()
    if not _DATE_LITERAL.fullmatch(result):
        raise ReadEffectError("unsupported_value")
    try:
        datetime.fromisoformat(result)
    except ValueError:
        raise ReadEffectError("unsupported_value") from None
    return result


@dataclass
class _Entry:
    capability: ReadCapability
    element: ElementHandle
    frame: Frame
    path: list[ReadFrame]
    facts: dict[str, Any]
    dom_revision: int
    values: dict[str, str] = field(default_factory=dict)
    child: Frame | None = None


class ReadCapabilityRegistry:
    def __init__(self, page: Page, *, now: Callable[[], datetime] | None = None) -> None:
        self.page = page
        self._now = now or (lambda: datetime.now(UTC))
        self._origin = ""
        self._start_url = ""
        self._path: list[ReadFrame] = []
        self._steps: list[BrowserStep] = []
        self._pagination: BrowserPagination | None = None
        self._entries: dict[str, _Entry] = {}
        self._monitor: JSHandle | None = None
        self._generation = 0
        self._valid = False
        self._completed: dict[Frame, int] = {}
        self._completed_started: dict[Frame, float] = {}
        self._started: dict[Request, float] = {}
        self._refused: set[Frame] = set()
        self._inflight: dict[Frame, set[Request]] = {}
        self._responses: dict[Request, tuple[int, str]] = {}
        page.on("response", self._response)
        page.on("request", self._request)
        page.on("requestfinished", self._request_done)
        page.on("requestfailed", self._request_done)
        page.on("framenavigated", self._navigated)

    @property
    def start_url(self) -> str:
        return self._start_url

    def invalidate(self) -> None:
        self._valid = False
        self._generation += 1

    def reset(self) -> None:
        """Called by explicit gateway.goto; snapshots invalidate refs but preserve the plan."""
        self.invalidate()
        self._path, self._steps = [], []
        self._pagination = None
        self._origin = self._start_url = ""
        self._refused.clear()

    def _navigated(self, frame: Frame) -> None:
        self.invalidate()

    def _request(self, request: Request) -> None:
        with contextlib.suppress(PlaywrightError):
            if request.method == "GET" and request.resource_type in {"document", "xhr", "fetch"}:
                self._inflight.setdefault(request.frame, set()).add(request)
                self._started[request] = time.monotonic()

    def _request_done(self, request: Request) -> None:
        started = self._started.pop(request, 0.0)
        response = self._responses.pop(request, None)
        with contextlib.suppress(PlaywrightError):
            frame = request.frame
            self._inflight.get(frame, set()).discard(request)
            if (
                request.failure is None
                and response is not None
                and 200 <= response[0] < 300
                and response[1] == _origin(frame.url)
            ):
                # This public event means the response body has completed. In pinned
                # Playwright it also resolves Response._finished_future, without creating
                # the orphan close-waiter left behind by Response.finished().
                self._completed[frame] = self._completed.get(frame, 0) + 1
                self._completed_started[frame] = max(
                    started, self._completed_started.get(frame, 0.0)
                )

    def _response(self, response: Response) -> None:
        with contextlib.suppress(PlaywrightError):
            request = response.request
            if request.method != "GET" or request.resource_type not in {"document", "xhr", "fetch"}:
                return
            frame = request.frame
            if response.status in {401, 403}:
                self._refused.add(frame)
            self._responses[request] = (response.status, _origin(response.url))

    async def aclose(self) -> None:
        self.invalidate()
        self.page.remove_listener("response", self._response)
        self.page.remove_listener("request", self._request)
        self.page.remove_listener("requestfinished", self._request_done)
        self.page.remove_listener("requestfailed", self._request_done)
        self.page.remove_listener("framenavigated", self._navigated)
        self._responses.clear()
        self._started.clear()
        self._inflight.clear()
        await self._dispose()

    async def _dispose(self) -> None:
        for entry in self._entries.values():
            with contextlib.suppress(PlaywrightError):
                await entry.element.dispose()
        self._entries.clear()
        if self._monitor is not None:
            with contextlib.suppress(PlaywrightError):
                await self._monitor.evaluate("s=>s.observer.disconnect()")
                await self._monitor.dispose()
            self._monitor = None

    async def _frame_at(self, path: list[ReadFrame]) -> Frame:
        if len(path) > 3 or _origin(self.page.url) != self._origin:
            raise ReadEffectError("frame_changed")
        frame = self.page.main_frame
        for descriptor in path:
            elements = await frame.query_selector_all(descriptor.selector)
            if len(elements) != 1:
                for element in elements:
                    await element.dispose()
                raise ReadEffectError("frame_changed")
            element = elements[0]
            try:
                facts: Any = await element.evaluate(_FRAME)
                if facts != descriptor.model_dump():
                    raise ReadEffectError("frame_changed")
                child = await element.content_frame()
                if child is None or _origin(child.url) != self._origin:
                    raise ReadEffectError("frame_changed")
                frame = child
            finally:
                await element.dispose()
        return frame

    async def snapshot(self, path: list[ReadFrame] | None = None) -> PageSnapshot:
        frame = await self._frame_at(self._path if path is None else path)
        raw: Any = await frame.evaluate(SCAN_JS)
        if not isinstance(raw, dict) or len(str(raw.get("html", ""))) >= MAX_SNAPSHOT_HTML:
            raise ReadEffectError("limit")
        snapshot = build_snapshot(frame.url, await frame.title(), raw)
        if snapshot.captcha_suspected or isinstance(detect(snapshot), (CaptchaPage, HardBlock)):
            raise BotaiError(ErrorCode.SOURCE_BOT_PROTECTION, "Источник требует проверки человеком")
        if (
            requires_authentication(snapshot)
            or has_credential_fields(snapshot)
            or frame in self._refused
        ):
            raise BotaiError(ErrorCode.SOURCE_AUTH_REQUIRED, "Нужно снова войти в источник")
        return snapshot

    async def discover(self) -> list[ReadCapability]:
        self.invalidate()
        await self._dispose()
        if not self._start_url:
            self._start_url, self._origin = self.page.url, _origin(self.page.url)
        try:
            frame = await self._frame_at(self._path)
            self._monitor = await frame.evaluate_handle(_MONITOR)
            raw: Any = await frame.evaluate(_SCAN)
            if not isinstance(raw, list):
                return []
            for data in raw[:MAX_CONTROLS]:
                if not isinstance(data, dict):
                    continue
                target = ReadTarget.model_validate(data["target"])
                if UNSAFE_CONTROL_LABEL.search(target.label) or UNSAFE_CONTROL_LABEL.search(
                    target.controls
                ):
                    continue
                kind = data.get("kind")
                if not isinstance(kind, str) or kind not in {
                    "activate_tab",
                    "set_date",
                    "select_option",
                    "next_link",
                    "wait_ready",
                }:
                    continue
                if kind == "next_link" and not self._safe_href(data.get("href")):
                    continue
                element = await frame.query_selector(target.selector)
                if element is None:
                    continue
                if await element.evaluate(_FACTS) != data:
                    await element.dispose()
                    continue
                values: dict[str, str] = {}
                labels: list[ReadValue] = []
                if kind == "set_date":
                    values = dict(DATE_VALUES)
                    labels = [ReadValue(key, value) for key, value in values.items()]
                elif kind == "select_option":
                    for index, option in enumerate(data.get("options", [])):
                        if UNSAFE_CONTROL_LABEL.search(
                            option["label"]
                        ) or UNSAFE_CONTROL_LABEL.search(option["value"]):
                            continue
                        key = f"option-{self._generation}-{index}"
                        values[key] = option["value"]
                        labels.append(ReadValue(key, option["label"]))
                    if not values:
                        await element.dispose()
                        continue
                ref = f"read-{self._generation}-{len(self._entries)}"
                capability = ReadCapability(
                    ref,
                    cast(ReadCapabilityKind, kind),
                    target.label or target.role or target.tag,
                    tuple(labels),
                )
                revision = await self._monitor.evaluate("s=>s.revision")
                self._entries[ref] = _Entry(
                    capability, element, frame, list(self._path), data, revision, values
                )
            if len(self._path) < 3:
                for element in (await frame.query_selector_all("iframe"))[:16]:
                    if len(self._entries) >= MAX_CONTROLS:
                        break
                    facts = await element.evaluate(_FRAME)
                    child = await element.content_frame()
                    if (
                        not isinstance(facts, dict)
                        or child is None
                        or _origin(child.url) != self._origin
                    ):
                        await element.dispose()
                        continue
                    descriptor = ReadFrame.model_validate(facts)
                    ref = f"read-{self._generation}-{len(self._entries)}"
                    capability = ReadCapability(
                        ref, "frame", descriptor.title or descriptor.name or "Встроенный раздел"
                    )
                    revision = await self._monitor.evaluate("s=>s.revision")
                    self._entries[ref] = _Entry(
                        capability,
                        element,
                        frame,
                        [*self._path, descriptor],
                        facts,
                        revision,
                        child=child,
                    )
            self._valid = True
            return [entry.capability for entry in self._entries.values()]
        except (PlaywrightError, ValueError):
            self.invalidate()
            return []

    def _safe_href(self, value: object) -> TypeGuard[str]:
        if not isinstance(value, str) or len(value) > 1000:
            return False
        parts = urlsplit(value)
        return (
            parts.scheme == "https"
            and _origin(value) == self._origin
            and not parts.username
            and not parts.password
            and not parts.fragment
        )

    async def apply(self, ref: str, *, value_ref: str | None = None) -> ReadActionResult:
        try:
            return await self._apply(ref, value_ref=value_ref)
        except PlaywrightError:
            raise ReadEffectError("stale_reference") from None

    async def _apply(self, ref: str, *, value_ref: str | None = None) -> ReadActionResult:
        entry = self._entries.get(ref)
        if not self._valid or entry is None or self._monitor is None:
            raise ReadEffectError("stale_reference")
        if await self._monitor.evaluate("s=>s.revision") != entry.dom_revision:
            raise ReadEffectError("stale_reference")
        current: Any = await entry.element.evaluate(_FRAME if entry.child else _FACTS)
        if current != entry.facts or entry.frame.is_detached():
            raise ReadEffectError("stale_reference")
        if value_ref is not None and value_ref not in entry.values:
            raise ReadEffectError("unknown_value")
        if entry.values and value_ref is None:
            raise ReadEffectError("unknown_value")
        self.invalidate()  # refs are single-use even if the effect times out
        if entry.child is not None:
            if _origin(entry.child.url) != self._origin:
                raise ReadEffectError("frame_changed")
            self._path = list(entry.path)
        else:
            if len(self._steps) >= 8 and entry.capability.kind != "next_link":
                raise ReadEffectError("limit")
            op = entry.capability.kind
            if op == "frame":
                raise ReadEffectError("frame_changed")
            step = BrowserStep(
                op=op,
                target=ReadTarget.model_validate(entry.facts["target"]),
                framePath=entry.path,
                region=ReadTarget.model_validate(entry.facts["region"])
                if entry.facts.get("region")
                else None,
                value=entry.values[value_ref] if value_ref is not None else None,
            )
            await self._perform(step, {}, entry.element)
            if op == "next_link":
                self._pagination = BrowserPagination(next=step)
            else:
                self._steps.append(step)
        frame = await self._frame_at(self._path)
        owner = next(
            (
                step.region
                for step in reversed(self._steps)
                if step.frame_path == self._path and step.region is not None
            ),
            None,
        )
        empty: Any = await frame.evaluate(_EMPTY, owner.selector if owner else None)
        empty_target = (
            ReadTarget.model_validate(empty[0])
            if isinstance(empty, list) and len(empty) == 1
            else None
        )
        return ReadActionResult(
            await self.snapshot(),
            BrowserPlan(
                steps=list(self._steps),
                framePath=list(self._path),
                emptyTarget=empty_target,
                pagination=self._pagination,
            ),
            self._start_url,
        )

    async def _target(
        self, frame: Frame, descriptor: ReadTarget, *, actionable: bool, allow_hidden: bool = False
    ) -> ElementHandle:
        elements = await frame.query_selector_all(descriptor.selector)
        if len(elements) != 1:
            for element in elements:
                await element.dispose()
            raise ReadEffectError("target_changed")
        element = elements[0]
        script = _FACTS if actionable else _HIDDEN_REGION_TARGET if allow_hidden else _TARGET
        facts: Any = await element.evaluate(script)
        target = facts.get("target") if actionable and isinstance(facts, dict) else facts
        if target != descriptor.model_dump():
            await element.dispose()
            raise ReadEffectError("target_changed")
        return element

    async def _content(
        self, frame: Frame, region: ReadTarget | None, *, allow_hidden: bool = False
    ) -> str:
        if region is None:
            html = await frame.content()
        else:
            element = await self._target(frame, region, actionable=False, allow_hidden=allow_hidden)
            try:
                html = await element.inner_html()
            finally:
                await element.dispose()
        if len(html) > MAX_HTML:
            raise ReadEffectError("limit")
        return hashlib.sha256(html.encode()).hexdigest()

    async def _settled(
        self,
        frame: Frame,
        region: ReadTarget | None,
        before: str,
        sequence: int,
        *,
        network: bool,
        changed: bool,
        started_after: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        previous, quiet_since = "", asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() < deadline:
            if _origin(frame.url) != self._origin or _origin(self.page.url) != self._origin:
                raise ReadEffectError("frame_changed")
            if frame in self._refused:
                raise BotaiError(
                    ErrorCode.SOURCE_AUTH_REQUIRED, "Источник требует повторного входа"
                )
            signature = await self._content(frame, region)
            now = asyncio.get_running_loop().time()
            if signature != previous:
                previous, quiet_since = signature, now
            busy = await frame.locator(region.selector if region else "body").get_attribute(
                "aria-busy"
            )
            if (
                (not changed or signature != before)
                and (
                    not network
                    or (
                        self._completed.get(frame, 0) > sequence
                        and self._completed_started.get(frame, 0) >= started_after
                    )
                )
                and not self._inflight.get(frame)
                and busy != "true"
                and now - quiet_since >= QUIET_SECONDS
            ):
                return
            await asyncio.sleep(0.05)
        raise ReadEffectError("no_progress")

    async def _perform(
        self, step: BrowserStep, values: Mapping[str, str], registered: ElementHandle | None = None
    ) -> None:
        frame = await self._frame_at(step.frame_path)
        element = registered or await self._target(frame, step.target, actionable=True)
        try:
            facts: Any = await element.evaluate(_FACTS)
            if (
                not isinstance(facts, dict)
                or facts.get("kind") != step.op
                or facts.get("target") != step.target.model_dump()
            ):
                raise ReadEffectError("target_changed")
            if facts.get("region") != (step.region.model_dump() if step.region else None):
                raise ReadEffectError("target_changed")
            before, sequence = (
                await self._content(frame, step.region, allow_hidden=step.op == "activate_tab"),
                self._completed.get(frame, 0),
            )
            started_after = time.monotonic()
            changed, network = True, step.op != "activate_tab"
            if step.op == "set_date":
                assert step.value is not None
                value = _date_value(step.value, values, self._now())
                lower, upper = facts.get("min"), facts.get("max")
                if (
                    isinstance(lower, str) and _DATE_LITERAL.fullmatch(lower) and value < lower
                ) or (isinstance(upper, str) and _DATE_LITERAL.fullmatch(upper) and value > upper):
                    raise ReadEffectError("invalid_value")
                if await element.input_value() == value:
                    changed, sequence, started_after = False, 0, 0.0
                else:
                    await element.fill(value, timeout=1000)
                    if await element.input_value() != value or not await element.evaluate(
                        "el=>el.validity.valid"
                    ):
                        raise ReadEffectError("invalid_value")
            elif step.op == "select_option":
                assert step.value is not None
                value = step.value  # an observed native option is a literal, never a binding
                options = facts.get("options", [])
                if not any(option.get("value") == value for option in options):
                    raise ReadEffectError("unknown_value")
                if await element.input_value() == value:
                    changed, sequence, started_after = False, 0, 0.0
                else:
                    await element.select_option(value=value, timeout=1000)
                    if await element.input_value() != value:
                        raise ReadEffectError("invalid_value")
            elif step.op == "activate_tab":
                if step.region is not None:
                    panel = await self._target(
                        frame, step.region, actionable=False, allow_hidden=True
                    )
                    try:
                        if not await panel.is_visible():
                            changed = False  # becoming visible is the verified panel effect
                    finally:
                        await panel.dispose()
                if await element.get_attribute("aria-selected") == "true":
                    changed = False
                else:
                    await element.click(timeout=1000)
            elif step.op == "next_link":
                href = facts.get("href")
                if not self._safe_href(href):
                    raise ReadEffectError("unsafe_navigation")
                await frame.goto(
                    href, wait_until="domcontentloaded", timeout=int(WAIT_SECONDS * 1000)
                )
            else:
                pending = [
                    self._started.get(request, started_after)
                    for request in self._inflight.get(frame, set())
                ]
                started_after = min(pending, default=started_after)
            await self._settled(
                frame,
                step.region,
                before,
                sequence,
                network=network,
                changed=changed,
                started_after=started_after,
            )
        except PlaywrightError:
            raise ReadEffectError("target_changed") from None
        finally:
            if registered is None:
                await element.dispose()

    async def replay(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]:
        try:
            return await self._replay(plan, values=values, items_selector=items_selector)
        except PlaywrightError:
            raise ReadEffectError("target_changed") from None

    async def _replay(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]:
        if not self._origin:
            self._start_url, self._origin = self.page.url, _origin(self.page.url)
        # Revalidate even a caller-mutated model, then compile current handles afresh per step.
        plan = BrowserPlan.model_validate(plan.model_dump(by_alias=True))
        for step in plan.steps:
            await self._perform(step, values)
        self._path = list(plan.frame_path)
        pages: list[PageSnapshot] = []
        seen: set[str] = set()
        maximum = plan.pagination.max_pages if plan.pagination else 1
        for index in range(maximum):
            snapshot, signature = await self._ready_snapshot(plan, items_selector)
            if signature in seen:
                raise ReadEffectError("no_progress")
            seen.add(signature)
            pages.append(snapshot)
            if plan.pagination is None:
                break
            frame = await self._frame_at(plan.pagination.next.frame_path)
            candidates = await frame.query_selector_all(plan.pagination.next.target.selector)
            if not candidates:
                break
            if len(candidates) != 1:
                for candidate in candidates:
                    await candidate.dispose()
                raise ReadEffectError("target_changed")
            next_element = candidates[0]
            next_facts: Any = await next_element.evaluate(_TARGET)
            if next_facts != plan.pagination.next.target.model_dump():
                await next_element.dispose()
                raise ReadEffectError("target_changed")
            if await next_element.get_attribute(
                "aria-disabled"
            ) == "true" or not await next_element.get_attribute("href"):
                await next_element.dispose()
                break
            for candidate in candidates:
                await candidate.dispose()
            if index + 1 >= maximum:
                raise ReadEffectError("limit")
            await self._perform(plan.pagination.next, values)
        return pages

    async def _ready_snapshot(
        self, plan: BrowserPlan, items_selector: str
    ) -> tuple[PageSnapshot, str]:
        from botai.sources.extract import parse_html, select_items

        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        previous, quiet_since = "", asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() < deadline:
            frame = await self._frame_at(plan.frame_path)
            snapshot = await self.snapshot()
            owner = next(
                (
                    step.region
                    for step in reversed(plan.steps)
                    if step.frame_path == plan.frame_path and step.region is not None
                ),
                None,
            )
            busy = await frame.locator(owner.selector if owner else "body").get_attribute(
                "aria-busy"
            )
            nodes = select_items(parse_html(snapshot.html), items_selector)
            explicit_empty = False
            if not nodes and plan.empty_target is not None:
                # Reapply the compiler's scope and explicit-empty semantics. A status with the
                # same descriptor in a sidebar cannot certify this owning region as empty.
                empty: Any = await frame.evaluate(_EMPTY, owner.selector if owner else None)
                explicit_empty = (
                    isinstance(empty, list)
                    and len(empty) == 1
                    and empty[0] == plan.empty_target.model_dump()
                )
            signature = hashlib.sha256(
                "\n".join(node.html or "" for node in nodes).encode()
            ).hexdigest()
            now = asyncio.get_running_loop().time()
            if signature != previous:
                previous, quiet_since = signature, now
            if (
                (nodes or explicit_empty)
                and self._completed.get(frame, 0) > 0
                and not self._inflight.get(frame)
                and busy != "true"
                and now - quiet_since >= QUIET_SECONDS
            ):
                return snapshot, signature
            await asyncio.sleep(0.05)
        raise ReadEffectError("no_progress")
