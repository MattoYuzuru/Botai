"""Adaptive, reference-only observation in one read-only browser context.

The model chooses among links and responses the executor already observed. It never authors
an address, selector, request body or script. Recipes still go through the separate author,
schema/lint, canary and human-confirmation pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar, cast, runtime_checkable
from zoneinfo import ZoneInfo

from botai.browser.probe import requires_authentication
from botai.browser.read_capabilities import ReadEffectError
from botai.browser.read_views import ReadView, ReadViewBrowser
from botai.contracts.browser_plan import BrowserPlan
from botai.contracts.specs import Kind
from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmFixtureMissing, LlmProvider, LlmRequest, LlmResponse
from botai.core.read_capabilities import ReadCapability, ReadCapabilityBrowser
from botai.learn.addresses import navigation_url, url_origin
from botai.learn.author import parse_json
from botai.learn.intent import LearningIntent
from botai.learn.memory import (
    DiscoveryMemory,
    MemoryLimits,
    NegativeReason,
    PortableBrowserPlan,
    StopReason,
)
from botai.learn.observe import (
    MAX_CALLS,
    Observations,
    RequestKey,
    body_view,
    data_response_health,
    interesting_call,
    menu_links,
    observations_of,
    observe,
    protect_observation,
    request_key,
)
from botai.learn.probe import (
    Prober,
    ProbeRefused,
    ProbeShape,
    observed_literals,
    probe_url,
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
    capabilities: bool = True
    probes: int = 6  # repeated reads with other dates or pages (FR-039) per observation


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    ref: str | None
    discoveries: tuple[Kind, ...]
    evidence: tuple[str, ...]
    value_ref: str | None = None
    overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeKit:
    """What a probe needs besides the observation: the reader and the source's clock."""

    prober: Prober
    now: datetime
    tz: ZoneInfo


@dataclass(frozen=True, slots=True)
class ProbeNote:
    source: str
    overrides: tuple[tuple[str, str], ...]
    status: int
    shape: ProbeShape


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
    capabilities: dict[str, ReadCapability] = field(default_factory=dict, repr=False)
    portable_plans: dict[str, PortableBrowserPlan] = field(default_factory=dict, repr=False)
    current_links: list[str] = field(default_factory=list)
    failures: set[str] = field(default_factory=set)
    network_auth_required: bool = False
    current_requested_url: str | None = field(default=None, repr=False)
    auth_target_url: str | None = field(default=None, repr=False)
    probe_kit: ProbeKit | None = field(default=None, repr=False)
    probe_count: int = 0
    probes: dict[str, ProbeNote] = field(default_factory=dict, repr=False)

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
        self.current_requested_url = url
        return await self.capture(snap)

    async def capture(self, snap: PageSnapshot) -> bool:
        """Refresh DOM, UI capabilities and the response delta after one executor action."""
        await protect_observation(self.handle, self.redactor)
        ref = f"p{len(self.pages)}"
        self.pages[ref] = snap
        self.revision += 1
        self.delta = [ref]
        shape = hashlib.sha256(self.redactor.html(snap.html).encode()).hexdigest()
        new_shape = shape not in self.shapes
        self.shapes.add(shape)
        self.views.clear()
        self.capabilities.clear()
        self.current_links.clear()
        if requires_authentication(snap) or snap.captcha_suspected:
            self.auth_target_url = self.current_requested_url
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
        if self.budget.capabilities and isinstance(self.handle, ReadCapabilityBrowser):
            for i, capability in enumerate(await self.handle.read_capabilities()):
                if i >= 24:
                    break
                self.capabilities[f"c{self.revision}-{i}"] = capability
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
        # Sampling can await a response while the SPA refreshes its credentials. Protect
        # that new state before any response or DOM detail becomes a planner/author input.
        await protect_observation(self.handle, self.redactor)
        self.network_count = len(log)
        self.network_auth_required, network_failed = data_response_health(log, self.origin)
        if self.network_auth_required:
            self.auth_target_url = self.current_requested_url
        if network_failed:
            self.failures.add("data_request_failed")
        else:
            self.failures.discard("data_request_failed")
        by_request = {request_key(call): ref for ref, call in self.calls.items()}
        changed_response = False
        # network_log is cumulative. Compare the newest response of each request with the
        # previous capture once; replaying its older history is not new evidence.
        latest_calls: dict[RequestKey, ObservedRequest] = {}
        for call in log[: self.budget.requests]:
            if call.status is None or not interesting_call(call, self.origin):
                continue
            latest_calls[request_key(call)] = call
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
                successful = call.status is not None and 200 <= call.status < 300
                changed_response = changed_response or (successful and bool(call.response_sample))
                self.inspected.discard(call_ref)
                self.details.pop(call_ref, None)
                if successful:
                    self.delta.append(call_ref)
        return new_shape or len(self.links) > previous_links or changed_response

    @property
    def auth_required(self) -> bool:
        return self.network_auth_required or any(
            requires_authentication(page) for page in self.pages.values()
        )

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
            "capabilities": [
                {
                    "ref": ref,
                    "kind": capability.kind,
                    "label": r.text(capability.label, 120),
                    "values": [
                        {"ref": value.ref, "label": r.text(value.label, 120)}
                        for value in capability.values
                    ],
                }
                for ref, capability in self.capabilities.items()
            ],
            "browserPlans": [
                {
                    "ref": ref,
                    "operations": [step.op for step in portable.plan.steps],
                    "frameDepth": len(portable.plan.frame_path),
                }
                for ref, portable in self.portable_plans.items()
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
            "probes": [
                {
                    "ref": ref,
                    "from": note.source,
                    "overrides": dict(note.overrides),
                    "status": note.status,
                    "objectsPerLevel": list(note.shape.levels),
                }
                for ref, note in self.probes.items()
            ],
            "details": self.details,
        }

    def decision(self, data: object) -> Decision:
        if not isinstance(data, dict):
            raise ValueError("invalid observation plan")
        data = cast(dict[str, Any], data)
        if set(data) - {
            "stateRef",
            "action",
            "ref",
            "discoveries",
            "evidenceRefs",
            "valueRef",
            "overrides",
        }:
            raise ValueError("invalid observation plan fields")
        if type(data.get("stateRef")) is not int or data["stateRef"] != self.revision:
            raise ValueError("stale observation plan")
        action, ref = data.get("action"), data.get("ref")
        if not isinstance(action, str) or action not in {
            "navigate",
            "inspect",
            "activate",
            "use",
            "probe",
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
        elif action == "use" and ref not in self.capabilities:
            raise ValueError("unknown capability reference")
        elif action == "probe" and ref not in self.calls:
            raise ValueError("unknown response reference")
        raw_overrides = data.get("overrides")
        if action == "probe":
            if (
                not isinstance(raw_overrides, dict)
                or not 1 <= len(raw_overrides) <= 4
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in cast(dict[object, object], raw_overrides).items()
                )
            ):
                raise ValueError("invalid probe overrides")
        elif raw_overrides:
            raise ValueError("this action does not accept overrides")
        value_ref = data.get("valueRef")
        if value_ref is not None and not isinstance(value_ref, str):
            raise ValueError("invalid capability value reference")
        if action == "use":
            capability = self.capabilities[cast(str, ref)]
            options = {option.ref for option in capability.values}
            if (options and value_ref not in options) or (not options and value_ref is not None):
                raise ValueError("unknown capability value reference")
        elif value_ref is not None:
            raise ValueError("this action does not accept a value reference")
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
        overrides = tuple(cast(dict[str, str], raw_overrides).items()) if action == "probe" else ()
        return Decision(
            action,
            ref,
            tuple(cast(list[Kind], kinds)),
            tuple(evidence),
            value_ref,
            overrides,
        )

    async def apply(self, decision: Decision) -> bool:
        ref = decision.ref
        if ref is None:
            return False
        if decision.action == "navigate":
            return await self.visit(self.links[ref][1])
        if decision.action == "probe":
            return await self.probe(ref, dict(decision.overrides))
        if decision.action == "use":
            if not isinstance(self.handle, ReadCapabilityBrowser):
                return False
            capability = self.capabilities.get(ref)
            if capability is None:
                return False
            try:
                result = await self.handle.apply_read_capability(
                    capability.ref, value_ref=decision.value_ref
                )
            except ReadEffectError:
                self.failures.add("no_progress")
                return False
            changed = await self.capture(result.snapshot)
            portable = PortableBrowserPlan(
                start_url=result.start_url, result_url=result.snapshot.url, plan=result.plan
            )
            if (
                navigation_url(portable.start_url) == portable.start_url
                and url_origin(portable.start_url) == self.origin
                and navigation_url(portable.result_url) == portable.result_url
                and url_origin(portable.result_url) == self.origin
                and not _browser_plan_contains_secret(portable.plan, self.redactor)
                and not any(
                    self.redactor.contains_secret_address(url)
                    for url in (portable.start_url, portable.result_url)
                )
            ):
                key = next(
                    (f"bp{i}" for i in range(8) if f"bp{i}" not in self.portable_plans), None
                )
                if key is not None:
                    self.portable_plans[key] = portable
            return changed
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

    async def probe(self, ref: str, overrides: dict[str, str]) -> bool:
        """Repeat one observed read with other query values and keep the answer as evidence."""
        kit = self.probe_kit
        if kit is None or self.probe_count >= self.budget.probes:
            self.failures.add("no_progress")
            return False
        call = self.calls[ref]
        try:
            url = probe_url(
                call,
                overrides,
                origin=self.origin,
                literals=observed_literals(self.calls.values()),
                now=kit.now,
                tz=kit.tz,
            )
        except ProbeRefused as refused:
            diagnostic_event("observe.probe", outcome="refused", reason=refused.reason)
            self.details = {ref: f"probe refused: {refused.reason}"}
            self.delta = [ref]
            self.revision += 1
            return False
        if self.redactor.contains_secret_address(url):
            diagnostic_event("observe.probe", outcome="refused", reason="secret_address")
            return False
        self.probe_count += 1
        reply = await kit.prober(call, url)
        if reply is None:
            self.failures.add("data_request_failed")
            diagnostic_event("observe.probe", outcome="failed", reason="request_failed")
            return False
        new_ref = f"r{len(self.calls)}"
        self.calls[new_ref] = call.model_copy(
            update={
                "url": url,
                "status": reply.status,
                "response_sample": reply.text,
                "response_content_type": reply.content_type,
                "evidence_id": f"probe-{self.probe_count}",
            }
        )
        self.probes[new_ref] = ProbeNote(ref, tuple(overrides.items()), reply.status, reply.shape)
        self.delta = [new_ref]
        self.revision += 1
        diagnostic_event(
            "observe.probe",
            outcome="ok",
            status=reply.status,
            top_level=reply.shape.top_level,
            records=reply.shape.records,
        )
        return True

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
            call_refs={request_key(self.calls[ref]): ref for ref in ordered_calls},
            known_links=list(self.links.values()),
        )
        # Auth/refusal evidence is a stop condition, never an optional planner-selected page.
        obs.auth_required = self.auth_required
        obs.bot_protection = self.bot_protection
        obs.auth_target_url = (
            self.auth_target_url if obs.auth_required or obs.bot_protection else None
        )
        obs.browser_plans = {
            ref: portable.plan.model_copy(deep=True)
            for ref, portable in self.portable_plans.items()
        }
        obs.browser_plan_urls = {
            ref: portable.start_url for ref, portable in self.portable_plans.items()
        }
        obs.browser_plan_results = {
            ref: portable.result_url for ref, portable in self.portable_plans.items()
        }
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
    memory: DiscoveryMemory | None = None,
    checkpoint: Callable[[DiscoveryMemory], Awaitable[None]] | None = None,
    run_limits: MemoryLimits | None = None,
    probe_kit: ProbeKit | None = None,
) -> Observations:
    """Model-guided exploration, with legacy crawl fallback for unavailable/malformed planners."""
    if memory is not None:
        return await _resumable_observe(
            handle,
            origin,
            redactor,
            start_urls=start_urls,
            intent=intent,
            llm=llm,
            budget=budget or DiscoveryBudget(),
            memory=memory,
            checkpoint=checkpoint,
            limits=run_limits or MemoryLimits(),
            probe_kit=probe_kit,
        )
    budget = budget or DiscoveryBudget()
    state = ObservationSession(handle, origin, redactor, budget, probe_kit=probe_kit)
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
                if state.network_count + state.probe_count >= budget.requests:
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
                    obs.browser_plans = retained.browser_plans
                    obs.browser_plan_urls = retained.browser_plan_urls
                    obs.browser_plan_results = retained.browser_plan_results
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


_Result = TypeVar("_Result")


class _SliceExpired(Exception):
    pass


_CSS_ESCAPE = re.compile(r"[0-9a-fA-F]{1,6}(?:\s)?")
_GENERATED_POSITION = re.compile(r":nth-of-type\([1-9][0-9]*\)")
_DATE_BINDING = re.compile(r"^\{(?:today|weekStart|(?:plusDays|minusDays):[0-9]{1,3})\}$")


def _selector_secret_text(selector: str) -> str:
    """Ignore generated positions, preserving IDs/attribute values and decoding CSS escapes.

    A regex over the whole selector would also erase a literal ':nth-of-type(1)' inside an
    attribute or an escaped ID. Those are source values and must retain their secret check.
    """
    parts: list[str] = []
    index, brackets = 0, 0
    quote: str | None = None
    while index < len(selector):
        char = selector[index]
        if char == "\\" and index + 1 < len(selector):
            escaped = _CSS_ESCAPE.match(selector, index + 1)
            if escaped is not None:
                point = int(escaped[0].strip(), 16)
                parts.append(chr(point) if 0 < point <= 0x10FFFF else "\ufffd")
                index = escaped.end()
            else:
                parts.append(selector[index + 1])
                index += 2
            continue
        if quote is not None:
            parts.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            parts.append(char)
        elif char == "[":
            brackets += 1
            parts.append(char)
        elif char == "]":
            brackets = max(0, brackets - 1)
            parts.append(char)
        elif brackets == 0 and (position := _GENERATED_POSITION.match(selector, index)):
            index = position.end()
            continue
        else:
            parts.append(char)
        index += 1
    return "".join(parts)


def _browser_plan_contains_secret(plan: BrowserPlan, redactor: Redactor) -> bool:
    """Check only source-derived descriptor values, never trusted JSON keys or constants."""
    steps = [*plan.steps, *([plan.pagination.next] if plan.pagination else [])]
    targets = [plan.empty_target]
    frames = list(plan.frame_path)
    for step in steps:
        targets.extend([step.target, step.region])
        frames.extend(step.frame_path)
        if step.value is not None:
            # ISO date templates are executable bindings generated by the trusted compiler;
            # their numeric offsets are not option values or observed credential fragments.
            binding = step.op == "set_date" and _DATE_BINDING.fullmatch(step.value)
            if not binding and redactor.contains_secret(step.value):
                return True
    for target in targets:
        if target is not None and any(
            redactor.contains_secret(value)
            for value in (_selector_secret_text(target.selector), target.label, target.controls)
        ):
            return True
    return any(
        redactor.contains_secret(value)
        for frame in frames
        for value in (_selector_secret_text(frame.selector), frame.name, frame.title)
    )


def _sanitize_memory_addresses(memory: DiscoveryMemory, redactor: Redactor) -> bool:
    """Remove protected values before any checkpoint or replay, never persist deny-set values."""
    omitted = False
    for field_name in ("frontier", "visited", "evidence_urls"):
        before = getattr(memory, field_name)
        after = [url for url in before if not redactor.contains_secret_address(url)]
        omitted = omitted or len(before) != len(after)
        setattr(memory, field_name, after)
    for field_name in ("last_url", "pending_url"):
        value = getattr(memory, field_name)
        if value is not None and redactor.contains_secret_address(value):
            setattr(memory, field_name, None)
            omitted = True
    negatives = []
    for outcome in memory.negative_outcomes:
        if outcome.url is not None and redactor.contains_secret_address(outcome.url):
            negatives.append(outcome.model_copy(update={"url": None}))
            omitted = True
        else:
            negatives.append(outcome)
    memory.negative_outcomes = negatives
    plans = {
        ref: portable
        for ref, portable in memory.browser_plans.items()
        if not _browser_plan_contains_secret(portable.plan, redactor)
        and not any(
            redactor.contains_secret_address(url)
            for url in (portable.start_url, portable.result_url)
        )
    }
    omitted = omitted or len(plans) != len(memory.browser_plans)
    memory.browser_plans = plans
    return omitted


@dataclass
class _MemorySlice:
    state: ObservationSession
    memory: DiscoveryMemory
    limits: MemoryLimits
    checkpoint: Callable[[DiscoveryMemory], Awaitable[None]] | None
    started: float = field(default_factory=time.monotonic)
    _tick: float = field(default_factory=time.monotonic)
    _network: int = 0
    _probed: int = 0
    slice_tokens: int = 0
    _sensitive_omitted: bool = False

    def reject_address(self) -> None:
        self.state.failures.add("navigation_failed")
        self.memory.remember_negative("navigation_failed")
        if not self._sensitive_omitted:
            diagnostic_event(
                "observe.plan", outcome="rejected", reason="invalid_address", omitted_sensitive=True
            )
            self._sensitive_omitted = True

    def allowed_address(self, url: str) -> bool:
        if self.state.redactor.contains_secret_address(url):
            self.reject_address()
            return False
        return True

    @property
    def remaining(self) -> float:
        return max(0.0, self.state.budget.seconds - (time.monotonic() - self.started))

    async def bounded(self, operation: Awaitable[_Result]) -> _Result:
        # Checkpoint callbacks are deliberately outside this timeout: a storage/fencing
        # exception must propagate instead of becoming a source timeout or fallback crawl.
        try:
            async with asyncio.timeout(self.remaining):
                return await operation
        except TimeoutError:
            raise _SliceExpired from None

    def account(self) -> None:
        now = time.monotonic()
        self.memory.active_ms += max(0, int((now - self._tick) * 1000))
        self._tick = now
        self.memory.requests += max(0, self.state.network_count - self._network)
        self._network = self.state.network_count
        self.memory.requests += max(0, self.state.probe_count - self._probed)
        self._probed = self.state.probe_count

    async def commit(self) -> None:
        self.account()
        state, memory = self.state, self.memory
        memory.browser_plans = dict(state.portable_plans)
        if _sanitize_memory_addresses(memory, state.redactor):
            self.reject_address()
        state.portable_plans = dict(memory.browser_plans)
        memory.merge_urls(
            "visited", [url for url in sorted(state.visited) if self.allowed_address(url)]
        )
        state.links = {
            ref: value for ref, value in state.links.items() if self.allowed_address(value[1])
        }
        state.current_links = [ref for ref in state.current_links if ref in state.links]
        targets = [target for _, target in state.links.values() if target not in state.visited]
        memory.merge_urls("frontier", targets)
        memory.frontier = [url for url in memory.frontier if url not in state.visited]
        if state.current_requested_url is not None and self.allowed_address(
            state.current_requested_url
        ):
            memory.last_url = state.current_requested_url
        pages = [
            page.url
            for page in state.pages.values()
            if navigation_url(page.url) == page.url
            and url_origin(page.url) == state.origin
            and self.allowed_address(page.url)
        ]
        memory.evidence_urls = list(dict.fromkeys([*memory.evidence_urls, *pages]))[-6:]
        for failure in sorted(state.failures):
            memory.remember_negative(cast(NegativeReason, failure), memory.last_url)
        memory.validate_for(state.origin)
        if self.checkpoint is not None:
            await self.checkpoint(memory.model_copy(deep=True))

    def stop(self) -> StopReason:
        self.account()
        reason = self.memory.exhausted_reason(self.limits)
        if reason:
            self.memory.exhausted = True
            return reason
        if self.memory.overflowed:
            return "page_budget"
        if self.remaining <= 0:
            return "timeout"
        if len(self.state.pages) >= self.state.budget.pages:
            return "page_budget"
        if self.state.network_count + self.state.probe_count >= self.state.budget.requests:
            return "request_budget"
        if self.slice_tokens >= self.state.budget.tokens:
            return "token_budget"
        return ""

    def next_url(self) -> str | None:
        return next((url for url in self.memory.frontier if url not in self.state.visited), None)

    async def visit(self, url: str) -> bool:
        if not self.allowed_address(url):
            if self.memory.pending_url == url:
                self.memory.pending_url = None
            await self.commit()
            return False
        changed = await self.bounded(self.state.visit(url))
        if self.state.current_requested_url != url:
            self.memory.remember_negative("navigation_failed", url)
        if self.memory.pending_url == url:
            self.memory.pending_url = None
        await self.commit()
        return changed


def _planner_allocation(request: LlmRequest) -> int:
    # UTF-8 bytes plus schema/role overhead conservatively bound text tokenization without
    # making a remote tokenizer call. Unknown in-flight allocations are never refunded.
    message_bytes = sum(len(message.content.encode("utf-8")) for message in request.messages)
    schema_bytes = len(json.dumps(request.json_schema, ensure_ascii=False).encode("utf-8"))
    return message_bytes + schema_bytes + request.max_tokens + 1024


async def _resumable_observe(
    handle: BrowserHandle,
    origin: str,
    redactor: Redactor,
    *,
    start_urls: list[str],
    intent: LearningIntent,
    llm: LlmProvider,
    budget: DiscoveryBudget,
    memory: DiscoveryMemory,
    checkpoint: Callable[[DiscoveryMemory], Awaitable[None]] | None,
    limits: MemoryLimits,
    probe_kit: ProbeKit | None = None,
) -> Observations:
    memory.validate_for(origin)
    sensitive_omitted = _sanitize_memory_addresses(memory, redactor)
    memory.abandon_planner()  # a previous process may have died after the remote call
    generation = memory.generations
    memory.generations += 1
    memory.resume_needed = False
    memory.exhausted = False
    state = ObservationSession(
        handle,
        origin,
        redactor,
        budget,
        portable_plans=dict(memory.browser_plans),
        probe_kit=probe_kit,
    )
    # Runtime references are rebuilt. In particular an old stateRef cannot accidentally match
    # the first page of a new handle, even when both happen to contain a link named l0.
    state.revision = generation * 1000
    state.visited.update(memory.visited)
    run = _MemorySlice(state, memory, limits, checkpoint)
    if sensitive_omitted:
        run.reject_address()
    evidence: tuple[str, ...] = ()
    stopped: StopReason = "step_budget"
    fallback = False
    no_progress = 0
    initial = [
        url
        for url in start_urls
        if navigation_url(url) == url and url_origin(url) == origin and run.allowed_address(url)
    ]
    memory.merge_urls("frontier", [url for url in initial if url not in state.visited])
    await run.commit()
    try:
        stopped = run.stop()
        if not stopped:
            restore = list(
                dict.fromkeys(
                    url
                    for url in [
                        memory.pending_url,
                        memory.last_url,
                        *reversed(memory.evidence_urls),
                    ]
                    if url is not None
                )
            )
            hydrate_limit = min(limits.max_rehydrate_pages, max(1, budget.pages // 2))
            if budget.pages <= 1 and memory.frontier:
                restore = [memory.pending_url or memory.frontier[0]]
            for url in restore[:hydrate_limit]:
                state.visited.discard(url)
                await run.visit(url)
                if state.auth_required or state.bot_protection or run.stop():
                    break
            if not state.pages and not run.stop():
                for url in memory.frontier:
                    if await run.visit(url):
                        break
                    if run.stop():
                        break
            state.offer_entries([*memory.frontier, *initial])
            await run.commit()
            if memory.finished and not state.auth_required and not state.bot_protection:
                stopped = "finished"
            else:
                memory.finished = False
                for step in range(budget.steps):
                    if state.auth_required or state.bot_protection:
                        stopped = "authentication"
                        break
                    stopped = run.stop()
                    if stopped:
                        break
                    if not state.pages:
                        stopped = "navigation_failed" if memory.negative_outcomes else "empty"
                        break
                    request = discovery_request(state.summary(), intent)
                    allocation = _planner_allocation(request)
                    if memory.tokens + memory.reserved_tokens + allocation > limits.max_tokens:
                        stopped = "token_budget"
                        memory.exhausted = True
                        break
                    memory.reserve_planner(allocation)
                    await run.commit()
                    diagnostic_event("observe.plan", phase="request", step=step)
                    response: LlmResponse | None = None
                    try:
                        if not isinstance(llm, ObservationProvider):
                            raise LlmFixtureMissing("provider has no observation planner")
                        response = await run.bounded(llm.complete_observation(request))
                    except LlmFixtureMissing:
                        memory.complete_planner(0)  # provider was not called
                        fallback, stopped = True, "planner_unavailable"
                    except BotaiError as exc:
                        if exc.code == ErrorCode.QUOTA_EXCEEDED:
                            memory.complete_planner(0)
                            await run.commit()
                            raise
                        memory.abandon_planner()
                        memory.remember_negative("provider_failed", memory.last_url)
                        fallback, stopped = True, "provider_failed"
                    if response is not None:
                        usage = response.usage
                        spent = usage.input_cache_hit + usage.input_cache_miss + usage.output
                        memory.complete_planner(spent)
                        run.slice_tokens += spent
                        try:
                            decision = state.decision(parse_json(response.content))
                        except ValueError:
                            memory.remember_negative("invalid_plan", memory.last_url)
                            fallback, stopped = True, "invalid_plan"
                        else:
                            if intent.discovery_mode == "auto":
                                memory.discovered_kinds = list(
                                    dict.fromkeys(
                                        [
                                            *memory.discovered_kinds,
                                            *(
                                                k
                                                for k in decision.discoveries
                                                if k not in intent.absent_kinds
                                            ),
                                        ]
                                    )
                                )
                            evidence = decision.evidence or evidence
                            if decision.action == "navigate" and decision.ref is not None:
                                target = state.links[decision.ref][1]
                                if not run.allowed_address(target):
                                    await run.commit()
                                    continue
                                memory.pending_url = target
                            await run.commit()  # validated selection precedes its read effect
                            if decision.action == "navigate":
                                # A callback/capture may discover a rotated credential between
                                # validation and this effect. Check the target again after await.
                                target = state.links.get(decision.ref or "")
                                if target is None or not run.allowed_address(target[1]):
                                    memory.pending_url = None
                                    await run.commit()
                                    continue
                            if decision.action == "finish":
                                if state.failures and run.next_url() is not None:
                                    target = run.next_url()
                                    assert target is not None
                                    await run.visit(target)
                                    continue
                                stopped, memory.finished = "finished", True
                                break
                            changed = await run.bounded(state.apply(decision))
                            if memory.pending_url is not None:
                                if state.current_requested_url != memory.pending_url:
                                    memory.remember_negative(
                                        "navigation_failed", memory.pending_url
                                    )
                                memory.pending_url = None
                            await run.commit()
                            no_progress = 0 if changed else no_progress + 1
                            if no_progress >= 2:
                                memory.remember_negative("no_progress", memory.last_url)
                                target = run.next_url()
                                if target is None:
                                    stopped = "no_progress"
                                    break
                                if run.stop():
                                    stopped = run.stop()
                                    break
                                memory.steps += 1
                                memory.pending_url = target
                                await run.commit()
                                await run.visit(target)
                                no_progress = 0
                            continue
                    await run.commit()
                    if fallback:
                        break
                else:
                    stopped = "step_budget"
            if fallback and not state.auth_required and not state.bot_protection:
                # Legacy providers terminate with the same bounded crawl semantics; they must
                # not turn a missing planner fixture/capability into endless queued slices.
                for _ in range(min(6, budget.pages)):
                    if run.stop() or (target := run.next_url()) is None:
                        break
                    memory.steps += 1
                    memory.pending_url = target
                    await run.commit()
                    await run.visit(target)
                    if state.auth_required or state.bot_protection:
                        break
                memory.finished = not state.auth_required and not state.bot_protection
    except _SliceExpired:
        stopped = "timeout"
        memory.abandon_planner()
    if state.auth_required or state.bot_protection:
        stopped = "authentication"
        memory.finished = False
    final_limit = run.stop()
    if memory.exhausted or memory.overflowed:
        stopped = final_limit or stopped
    memory.stop_reason = stopped
    memory.resume_needed = (
        not memory.finished
        and not memory.exhausted
        and not memory.overflowed
        and stopped in {"step_budget", "token_budget", "page_budget", "request_budget", "timeout"}
    )
    await run.commit()
    obs = state.bundle(evidence)
    return finish_observation(
        obs,
        state,
        run.slice_tokens,
        tuple(memory.discovered_kinds),
        stopped,
        run.started,
        limited=memory.exhausted
        or memory.overflowed
        or memory.resume_needed
        or stopped not in {"finished", "authentication", "empty", "planner_unavailable"}
        or (fallback and run.next_url() is not None),
    )
