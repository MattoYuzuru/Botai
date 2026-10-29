"""Optional read capabilities; the planner selects current refs, never selectors or scripts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from botai.contracts.browser_plan import BrowserPlan
from botai.core.browser import PageSnapshot

ReadCapabilityKind = Literal[
    "activate_tab", "set_date", "select_option", "next_link", "wait_ready", "frame"
]


@dataclass(frozen=True, slots=True)
class ReadValue:
    ref: str
    label: str


@dataclass(frozen=True, slots=True)
class ReadCapability:
    ref: str
    kind: ReadCapabilityKind
    label: str
    values: tuple[ReadValue, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadActionResult:
    snapshot: PageSnapshot
    plan: BrowserPlan = field(repr=False)
    start_url: str = field(default="", repr=False)


@runtime_checkable
class ReadCapabilityBrowser(Protocol):
    async def read_capabilities(self) -> list[ReadCapability]: ...

    async def apply_read_capability(
        self, ref: str, *, value_ref: str | None = None
    ) -> ReadActionResult: ...

    async def replay_browser_plan(
        self, plan: BrowserPlan, *, values: Mapping[str, str], items_selector: str
    ) -> list[PageSnapshot]: ...
