"""Adaptive, reference-only observation in one read-only browser context.

The model chooses among links and responses the executor already observed. It never authors
an address, selector, request body or script. Recipes still go through the separate author,
schema/lint, canary and human-confirmation pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from botai.browser.probe import requires_authentication
from botai.browser.read_views import ReadView, ReadViewBrowser
from botai.contracts.specs import Kind
from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmFixtureMissing, LlmProvider, LlmRequest, LlmResponse
from botai.learn.addresses import navigation_url, url_origin
from botai.learn.author import parse_json
from botai.learn.intent import LearningIntent
from botai.learn.observe import (
    MAX_CALLS,
    Observations,
    body_view,
    interesting_call,
    menu_links,
    observations_of,
    observe,
)
from botai.learn.prompting import discovery_request
from botai.learn.redact import Redactor
from botai.sources.vocab import KIND_LABELS


@runtime_checkable
class ObservationProvider(Protocol):
    """Optional test/provider dispatch: planner calls cannot consume author fixtures."""

    async def complete_observation(self, request: LlmRequest) -> LlmResponse: ...


@dataclass(frozen=True, slots=True)
class DiscoveryBudget:
    steps: int = 10
    pages: int = 12
    requests: int = 200
    seconds: float = 90.0
    tokens: int = 150_000
    links_per_page: int = 80


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    ref: str | None
    discoveries: tuple[Kind, ...]
    evidence: tuple[str, ...]


@dataclass(slots=True)
class ObservationSession:
    handle: BrowserHandle
    origin: str
    redactor: Redactor
    budget: DiscoveryBudget
    pages: dict[str, PageSnapshot] = field(default_factory=dict, repr=False)
    links: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    calls: dict[str, ObservedRequest] = field(default_factory=dict, repr=False)
    visited: set[str] = field(default_factory=set, repr=False)
    inspected: set[str] = field(default_factory=set)
    details: dict[str, str] = field(default_factory=dict, repr=False)
    revision: int = 0
    network_count: int = 0
    shapes: set[str] = field(default_factory=set, repr=False)
    delta: list[str] = field(default_factory=list)
    views: dict[str, ReadView] = field(default_factory=dict, repr=False)
    current_links: list[str] = field(default_factory=list)
    failures: set[str] = field(default_factory=set)

    def offer_entries(self, urls: list[str]) -> None:
        """Keep explicit entry pages and the source home available without inventing routes."""
        known = {target for _, target in self.links.values()}
        for url in urls:
            if (
                navigation_url(url) is not None
                and url_origin(url) == self.origin
                and url not in self.visited
                and url not in known
            ):
                self.links[f"l{len(self.links)}"] = ("Стартовая страница", url)
                known.add(url)

    async def visit(self, url: str) -> bool:
        # Revalidate raw executor-owned addresses, even though they were checked when captured.
        if navigation_url(url) is None or url_origin(url) != self.origin or url in self.visited:
            return False
        self.visited.add(url)
        try:
            snap = await self.handle.goto(url)
        except BotaiError as exc:
            if exc.code in {ErrorCode.SOURCE_AUTH_REQUIRED, ErrorCode.SOURCE_BOT_PROTECTION}:
                raise
            self.failures.add("navigation_failed")
            diagnostic_event("observe.visit_failed", reason="navigation_failed")
            return False
        except (KeyError, TimeoutError):
            self.failures.add("navigation_failed")
            diagnostic_event("observe.visit_failed", reason="navigation_failed")
            return False
        return await self.capture(snap)

    async def capture(self, snap: PageSnapshot) -> bool:
        """Refresh DOM, UI capabilities and the response delta after one executor action."""
        ref = f"p{len(self.pages)}"
        self.pages[ref] = snap
        self.revision += 1
        self.delta = [ref]
        shape = hashlib.sha256(self.redactor.html(snap.html).encode()).hexdigest()
        new_shape = shape not in self.shapes
        self.shapes.add(shape)
        self.views.clear()
        self.current_links.clear()
        if requires_authentication(snap) or snap.captcha_suspected:
            return True  # Let Explorer reauthenticate before another model call.
        known = {target for _, target in self.links.values()}
        previous_links = len(self.links)
        for label, target in menu_links(
            snap.html, snap.url or self.origin + "/", self.origin, limit=self.budget.links_per_page
        ):
            if target not in known and target not in self.visited:
                link_ref = f"l{len(self.links)}"
                self.links[link_ref] = (label, target)
                self.current_links.append(link_ref)
                known.add(target)
        if isinstance(self.handle, ReadViewBrowser):
            for i, view in enumerate(await self.handle.read_views()):
                if i >= 24:
                    break
                self.views[f"t{self.revision}-{i}"] = view
        try:
            log = await self.handle.network_log()
        except Exception:
            self.failures.add("network_log_failed")
            diagnostic_event("observe.network_failed", reason="network_log_failed")
            log = []
        self.network_count = len(log)
        by_request = {(call.method, call.url): ref for ref, call in self.calls.items()}
        changed_response = False
        # network_log is cumulative. Compare the newest response of each request with the
        # previous capture once; replaying its older history is not new evidence.
        latest_calls: dict[tuple[str, str], ObservedRequest] = {}
        for call in log[: self.budget.requests]:
            if call.status is None or not interesting_call(call, self.origin):
                continue
            latest_calls[(call.method, call.url)] = call
        for key, call in latest_calls.items():
            call_ref = by_request.get(key)
            previous = self.calls.get(call_ref) if call_ref is not None else None
            if call_ref is None:
                call_ref = f"r{len(self.calls)}"
                by_request[key] = call_ref
            self.calls[call_ref] = call.model_copy()
            if (
                previous is None
                or previous.response_sample != call.response_sample
                or previous.status != call.status
            ):
                changed_response = changed_response or bool(call.response_sample)
                self.inspected.discard(call_ref)
                self.details.pop(call_ref, None)
                self.delta.append(call_ref)
        return new_shape or len(self.links) > previous_links or changed_response

    @property
    def auth_required(self) -> bool:
        return any(requires_authentication(page) for page in self.pages.values())

    @property
    def bot_protection(self) -> bool:
        return any(page.captcha_suspected for page in self.pages.values())

    def summary(self) -> dict[str, Any]:
        r = self.redactor
        link_refs = list(dict.fromkeys([*self.current_links, *self.links]))
        response_refs = list(
            dict.fromkeys(
                [*(ref for ref in self.delta if ref in self.calls), *reversed(self.calls)]
            )
        )[:MAX_CALLS]
        return {
            "stateRef": self.revision,
            "newEvidenceRefs": self.delta,
            "pages": [
                {
                    "ref": ref,
                    "url": r.url(page.url, self.origin),
                    "title": r.text(page.title, 120),
                    "html": r.html(page.html, 4000),
                }
                for ref, page in list(self.pages.items())[-4:]
            ],
            "links": [
                {
                    "ref": ref,
                    "label": r.text(self.links[ref][0], 100),
                    "url": r.url(self.links[ref][1], self.origin),
                }
                for ref in link_refs
                if self.links[ref][1] not in self.visited
            ][:160],
            "views": [
                {"ref": ref, "label": r.text(view.label, 100)} for ref, view in self.views.items()
            ],
            "responses": [
                {
                    "ref": ref,
                    "method": self.calls[ref].method,
                    "url": r.url(self.calls[ref].url, self.origin),
                    "status": self.calls[ref].status,
                    "body": body_view(r, self.calls[ref].response_sample, 1600),
                }
                for ref in response_refs
            ],
            "details": self.details,
        }

    def decision(self, data: object) -> Decision:
        if not isinstance(data, dict):
            raise ValueError("invalid observation plan")
        data = cast(dict[str, Any], data)
        if set(data) - {"stateRef", "action", "ref", "discoveries", "evidenceRefs"}:
            raise ValueError("invalid observation plan fields")
        if type(data.get("stateRef")) is not int or data["stateRef"] != self.revision:
            raise ValueError("stale observation plan")
        action, ref = data.get("action"), data.get("ref")
        if not isinstance(action, str) or action not in {
            "navigate",
            "inspect",
            "activate",
            "finish",
        }:
            raise ValueError("invalid observation action")
        if action == "finish":
            if ref is not None:
                raise ValueError("finish cannot name a destination")
        elif not isinstance(ref, str):
            raise ValueError("observation reference required")
        elif action == "navigate" and ref not in self.links:
            raise ValueError("unknown link reference")
        elif action == "inspect" and ref not in self.pages and ref not in self.calls:
            raise ValueError("unknown evidence reference")
        elif action == "activate" and ref not in self.views:
            raise ValueError("unknown view reference")
        evidence = data.get("evidenceRefs", [])
        if (
            not isinstance(evidence, list)
            or len(evidence) > 8
            or any(
                not isinstance(ref_, str) or ref_ not in {*self.pages, *self.calls}
                for ref_ in evidence
            )
        ):
            raise ValueError("invalid evidence references")
        kinds = data.get("discoveries", [])
        if (
            not isinstance(kinds, list)
            or any(not isinstance(kind, str) or kind not in KIND_LABELS for kind in kinds)
            or len(kinds) > len(KIND_LABELS)
            or (kinds and not evidence)
        ):
            raise ValueError("invalid discovered kinds")
        return Decision(action, ref, tuple(cast(list[Kind], kinds)), tuple(evidence))

    async def apply(self, decision: Decision) -> bool:
        ref = decision.ref
        if ref is None:
            return False
        if decision.action == "navigate":
            return await self.visit(self.links[ref][1])
        if decision.action == "activate":
            if not isinstance(self.handle, ReadViewBrowser):
                return False
            try:
                snap = await self.handle.activate_read_view(self.views[ref].ref)
            except BotaiError:
                self.failures.add("navigation_failed")
                diagnostic_event("observe.plan", outcome="rejected", reason="navigation_failed")
                return False
            return await self.capture(snap)
        if ref in self.inspected:
            return False
        self.inspected.add(ref)
        if ref in self.pages:
            detail = self.redactor.html(self.pages[ref].html, 12_000)
        else:
            detail = body_view(self.redactor, self.calls[ref].response_sample, 8000)
        self.details = {ref: detail}  # only the newest expansion; no growing duplicate context
        self.delta = [ref]
        self.revision += 1
        return bool(detail)

    def bundle(self, evidence: tuple[str, ...] = ()) -> Observations:
        page_refs = [ref for ref in evidence if ref in self.pages]
        call_refs = [ref for ref in evidence if ref in self.calls]
        # Distil the chosen evidence ahead of navigation shells, instead of cutting at BFS6.
        ordered_pages = list(dict.fromkeys([*page_refs, *reversed(self.pages)]))[:6]
        ordered_calls = list(dict.fromkeys([*call_refs, *self.calls]))[:MAX_CALLS]
        obs = observations_of(
            self.origin,
            self.redactor,
            [(self.pages[ref].url, self.pages[ref]) for ref in ordered_pages],
            [self.calls[ref] for ref in ordered_calls],
            visited_count=len(self.visited),
            page_refs=tuple(ordered_pages),
            call_refs={(self.calls[ref].method, self.calls[ref].url): ref for ref in ordered_calls},
        )
        # Auth/refusal evidence is a stop condition, never an optional planner-selected page.
        obs.auth_required = self.auth_required
        obs.bot_protection = self.bot_protection
        for ref, detail in self.details.items():
            if ref in self.pages:
                for page in obs.pages:
                    if page.reference == ref:
                        page.html = detail
            elif ref in self.calls:
                for call in obs.calls:
                    if call.reference == ref:
                        call.body = detail
        return obs


async def adaptive_observe(
    handle: BrowserHandle,
    origin: str,
    redactor: Redactor,
    *,
    start_urls: list[str],
    intent: LearningIntent,
    llm: LlmProvider,
    budget: DiscoveryBudget | None = None,
) -> Observations:
    """Model-guided exploration, with legacy crawl fallback for unavailable/malformed planners."""
    budget = budget or DiscoveryBudget()
    state = ObservationSession(handle, origin, redactor, budget)
    tokens = 0
    discovered: dict[Kind, None] = {}
    evidence: tuple[str, ...] = ()
    no_progress = 0
    fallback = False
    stopped = "step_budget"
    started = time.monotonic()
    try:
        async with asyncio.timeout(budget.seconds):
            for url in start_urls:
                if await state.visit(url):
                    break
            # A deep user URL need not link back to the home page or to the other explicit
            # entries. These are known destinations, not model-authored endpoint guesses.
            state.offer_entries(start_urls)
            for step in range(budget.steps):
                if state.auth_required or state.bot_protection:
                    stopped = "authentication"
                    break
                if tokens >= budget.tokens:
                    stopped = "token_budget"
                    break
                if len(state.pages) >= budget.pages:
                    stopped = "page_budget"
                    break
                if state.network_count >= budget.requests:
                    stopped = "request_budget"
                    break
                if not state.pages:
                    stopped = "empty"
                    break
                request = discovery_request(state.summary(), intent)
                diagnostic_event(
                    "observe.plan",
                    phase="request",
                    step=step,
                    input_chars=sum(len(m.content) for m in request.messages),
                )
                try:
                    if isinstance(llm, ObservationProvider):
                        response = await llm.complete_observation(request)
                    else:
                        raise LlmFixtureMissing("provider has no observation planner")
                    usage = response.usage
                    tokens += usage.input_cache_hit + usage.input_cache_miss + usage.output
                    decision = state.decision(parse_json(response.content))
                except LlmFixtureMissing:
                    fallback = True
                    stopped = "planner_unavailable"
                    diagnostic_event(
                        "observe.plan", outcome="skipped", reason="planner_unavailable"
                    )
                    break
                except ValueError:
                    fallback = True
                    stopped = "invalid_plan"
                    diagnostic_event("observe.plan", outcome="rejected", reason="invalid_json")
                    break
                except BotaiError as exc:
                    if exc.code == ErrorCode.QUOTA_EXCEEDED:
                        raise
                    fallback = True
                    stopped = "provider_failed"
                    diagnostic_event("observe.plan", outcome="failed", reason="provider_failed")
                    break
                diagnostic_event("observe.plan", phase="response", step=step, outcome="accepted")
                if intent.discovery_mode == "auto":
                    discovered.update(
                        dict.fromkeys(
                            k for k in decision.discoveries if k not in intent.absent_kinds
                        )
                    )
                evidence = decision.evidence or evidence
                if decision.action == "finish":
                    stopped = "finished"
                    break
                changed = await state.apply(decision)
                no_progress = 0 if changed else no_progress + 1
                if no_progress >= 2:
                    stopped = "no_progress"
                    break
    except TimeoutError:
        stopped = "timeout"
        diagnostic_event("observe.plan", outcome="failed", reason="timeout")
    if fallback and not state.auth_required and not state.bot_protection:
        remaining = max(0.0, budget.seconds - (time.monotonic() - started))
        remaining_pages = max(0, budget.pages - len(state.pages))
        if remaining > 0 and remaining_pages > 0 and state.network_count < budget.requests:
            try:
                async with asyncio.timeout(remaining):
                    fallback_limit = min(6, remaining_pages)
                    obs = await observe(
                        handle,
                        origin,
                        redactor,
                        start_urls=start_urls,
                        max_pages=fallback_limit,
                        max_requests=budget.requests,
                    )
                    added_pages = len(obs.pages)
                    fallback_limited = added_pages >= fallback_limit
                    retained = state.bundle(evidence)
                    # Retain chosen adaptive evidence. Revisited pages/responses replace stale
                    # content, but a malformed plan cannot erase the useful earlier branch.
                    pages = {page.url: page for page in retained.pages}
                    pages.update({page.url: page for page in obs.pages})
                    calls = {(call.method, call.url): call for call in retained.calls}
                    calls.update({(call.method, call.url): call for call in obs.calls})
                    obs.pages = list(pages.values())[:6]
                    obs.calls = list(calls.values())[:MAX_CALLS]
                    obs.observed.update(retained.observed)
                    obs.plain_text = (retained.plain_text + "\n" + obs.plain_text)[:8000]
                    obs.auth_required = obs.auth_required or retained.auth_required
                    obs.bot_protection = obs.bot_protection or retained.bot_protection
                    return finish_observation(
                        obs,
                        state,
                        tokens,
                        tuple(discovered),
                        stopped,
                        started,
                        limited=stopped != "planner_unavailable" or fallback_limited or obs.limited,
                        additional_pages=added_pages,
                    )
            except TimeoutError:
                stopped = "timeout"
                diagnostic_event("observe.plan", outcome="failed", reason="timeout")
    obs = state.bundle(evidence)
    return finish_observation(
        obs,
        state,
        tokens,
        tuple(discovered),
        stopped,
        started,
        limited=stopped not in {"finished", "authentication", "empty"},
    )


def finish_observation(
    obs: Observations,
    state: ObservationSession,
    tokens: int,
    discovered: tuple[Kind, ...],
    stopped: str,
    started: float,
    *,
    limited: bool,
    additional_pages: int = 0,
) -> Observations:
    obs.discovered_kinds = discovered
    obs.planner_tokens = tokens
    obs.limited = limited or bool(state.failures)
    obs.stop_reason = (
        sorted(state.failures)[0]
        if state.failures and stopped in {"finished", "empty", "planner_unavailable"}
        else obs.stop_reason
        if obs.stop_reason and stopped == "planner_unavailable"
        else stopped
    )
    diagnostic_event(
        "observe.discovery",
        page_count=len(state.pages) + additional_pages,
        call_count=len(obs.calls),
        token_count=tokens,
        limited=obs.limited,
        stop_reason=obs.stop_reason,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    return obs
