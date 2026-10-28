"""Optional, server-owned navigation between observed read-only tab panels."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from playwright.async_api import ElementHandle, Page
from playwright.async_api import Error as PlaywrightError

from botai.core.browser import PageSnapshot
from botai.core.errors import BotaiError, ErrorCode

MAX_READ_VIEWS = 12
_DESTRUCTIVE = re.compile(
    r"logout|sign[ -]?out|log[ -]?in|sign[ -]?in|password|delete|remove|unsubscribe|"
    r"submit|send|save|create|edit|pay|purchase|выйти|выход|войти|вход|парол|удал|"
    r"отпис|отправ|сохран|созда|измен|оплат|купить",
    re.IGNORECASE,
)
_TAB_FACTS = r"""el => {
  if (!el.isConnected || el.ownerDocument !== document || el.getAttribute('role') !== 'tab') return null;
  if (el.closest('form') || el.getAttribute('aria-disabled') === 'true' || el.disabled) return null;
  if (el.matches('input,textarea,select,[contenteditable=true],button[type=submit]')) return null;
  if (el.querySelector('input,textarea,select,button,a[href],[contenteditable=true]')) return null;
  const href = el.getAttribute('href');
  if (href && !href.startsWith('#')) return null;
  const style = getComputedStyle(el), box = el.getBoundingClientRect();
  if (style.display === 'none' || style.visibility !== 'visible' || box.width <= 0 || box.height <= 0) return null;
  const controls = (el.getAttribute('aria-controls') || '').trim();
  if (!controls || /\s/.test(controls)) return null;
  const panel = document.getElementById(controls);
  if (!panel || panel.getAttribute('role') !== 'tabpanel') return null;
  const label = (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim();
  if (!label || label.length > 120) return null;
  const path = [];
  let node = el;
  while (node && node.nodeType === 1 && path.length < 50) {
    if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
      path.unshift('#' + CSS.escape(node.id)); break;
    }
    const tag = node.tagName.toLowerCase();
    const peers = node.parentElement ? Array.from(node.parentElement.children).filter(n => n.tagName === node.tagName) : [node];
    path.unshift(tag + ':nth-of-type(' + (peers.indexOf(node) + 1) + ')');
    node = node.parentElement;
  }
  const selector = path.join(' > ');
  if (!selector || selector.length > 1500 || document.querySelectorAll(selector).length !== 1) return null;
  return {label, controls, selector};
}"""
_SCAN_TABS = (
    "() => Array.from(document.querySelectorAll('[role=tab][aria-controls]')).slice(0,256)"
    ".map(" + _TAB_FACTS + ").filter(Boolean).slice(0,12)"
)


@dataclass(frozen=True, slots=True)
class ReadView:
    ref: str
    label: str


@runtime_checkable
class ReadViewBrowser(Protocol):
    async def read_views(self) -> list[ReadView]: ...
    async def activate_read_view(self, ref: str) -> PageSnapshot: ...


@dataclass(slots=True)
class _Registered:
    label: str
    controls: str
    selector: str
    element: ElementHandle


class ReadViewRegistry:
    def __init__(self) -> None:
        self._generation = 0
        self._valid = False
        self._page: Page | None = None
        self._url = ""
        self._entries: dict[str, _Registered] = {}

    def invalidate(self) -> None:
        self._valid = False
        self._generation += 1

    async def discover(self, page: Page) -> list[ReadView]:
        self.invalidate()
        for entry in self._entries.values():
            with contextlib.suppress(PlaywrightError):
                await entry.element.dispose()
        self._entries.clear()
        self._page, self._url = page, page.url
        try:
            raw: Any = await page.evaluate(_SCAN_TABS)
            if not isinstance(raw, list):
                return []
            for data in raw[:MAX_READ_VIEWS]:
                if not isinstance(data, dict):
                    continue
                label, controls, selector = (
                    data.get("label"),
                    data.get("controls"),
                    data.get("selector"),
                )
                if (
                    not isinstance(label, str)
                    or not isinstance(controls, str)
                    or not isinstance(selector, str)
                    or _DESTRUCTIVE.search(label)
                    or _DESTRUCTIVE.search(controls)
                ):
                    continue
                element = await page.query_selector(selector)
                if element is None:
                    continue
                facts: Any = await element.evaluate(_TAB_FACTS)
                if facts != data:
                    await element.dispose()
                    continue
                ref = f"tab-{self._generation}-{len(self._entries)}"
                self._entries[ref] = _Registered(label, controls, selector, element)
            if page.url != self._url:
                return []
            self._valid = True
            return [ReadView(ref, entry.label) for ref, entry in self._entries.items()]
        except PlaywrightError:
            return []

    async def activate(self, page: Page, ref: str) -> None:
        entry = self._entries.get(ref)
        if not self._valid or page is not self._page or page.url != self._url or entry is None:
            raise BotaiError(
                ErrorCode.CONFLICT, "Представление устарело. Сначала осмотрите страницу"
            )
        try:
            facts: Any = await entry.element.evaluate(_TAB_FACTS)
            if facts != {
                "label": entry.label,
                "controls": entry.controls,
                "selector": entry.selector,
            }:
                raise BotaiError(ErrorCode.CONFLICT, "Представление страницы изменилось")
            self.invalidate()
            await entry.element.click(timeout=10_000)
        except PlaywrightError:
            # An exception can quote a DOM label or selector; it is never an operator log.
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Представление страницы недоступно"
            ) from None
