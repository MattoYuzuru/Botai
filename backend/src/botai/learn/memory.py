"""Private, bounded research knowledge; no browser references, DOM or response bodies.

This value is an input to the encrypted job checkpoint, never a public API or a prompt.
Persisted URLs remain executor-owned addresses and are checked again before navigation.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from botai.contracts.browser_plan import BrowserPlan
from botai.contracts.specs import Kind
from botai.learn.addresses import navigation_url, url_origin

MAX_KNOWN_URLS = 128
MAX_EVIDENCE_URLS = 6
MAX_NEGATIVE_OUTCOMES = 128

StopReason = Literal[
    "",
    "data_request_failed",
    "navigation_failed",
    "network_log_failed",
    "finished",
    "authentication",
    "empty",
    "step_budget",
    "token_budget",
    "page_budget",
    "request_budget",
    "no_progress",
    "planner_unavailable",
    "invalid_plan",
    "provider_failed",
    "timeout",
]
NegativeReason = Literal[
    "navigation_failed",
    "network_log_failed",
    "data_request_failed",
    "invalid_plan",
    "provider_failed",
    "no_progress",
]


class MemoryLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_steps: int = Field(default=100, ge=1, le=10_000)
    max_requests: int = Field(default=2000, ge=1, le=1_000_000)
    max_tokens: int = Field(default=1_500_000, ge=1, le=100_000_000)
    max_active_ms: int = Field(default=1_800_000, ge=1, le=86_400_000)
    max_rehydrate_pages: int = Field(default=3, ge=1, le=MAX_EVIDENCE_URLS)


class NegativeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    url: str | None = Field(default=None, max_length=1000, repr=False)
    reason: NegativeReason
    intent_key: str = Field(default="", max_length=64, repr=False)


class PortableBrowserPlan(BaseModel):
    """Executor-compiled descriptors, never current DOM/element/frame references."""

    model_config = ConfigDict(extra="forbid", strict=True)
    start_url: str = Field(max_length=1000, repr=False)
    result_url: str = Field(max_length=1000, repr=False)
    plan: BrowserPlan = Field(repr=False)


class DiscoveryMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    schema_version: Literal[1] = 1
    origin: str
    intent_key: str = Field(default="", max_length=64, repr=False)
    frontier: list[str] = Field(default_factory=list, max_length=MAX_KNOWN_URLS, repr=False)
    visited: list[str] = Field(default_factory=list, max_length=MAX_KNOWN_URLS, repr=False)
    evidence_urls: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_URLS, repr=False)
    browser_plans: dict[str, PortableBrowserPlan] = Field(
        default_factory=dict, max_length=8, repr=False
    )
    negative_outcomes: list[NegativeOutcome] = Field(
        default_factory=list, max_length=MAX_NEGATIVE_OUTCOMES, repr=False
    )
    last_url: str | None = Field(default=None, max_length=1000, repr=False)
    pending_url: str | None = Field(default=None, max_length=1000, repr=False)
    steps: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    probes: int = Field(default=0, ge=0)  # repeated reads with other dates, over all slices
    active_ms: int = Field(default=0, ge=0)
    reserved_tokens: int = Field(default=0, ge=0)
    pending_planner_tokens: int = Field(default=0, ge=0)
    uncertain_calls: int = Field(default=0, ge=0)
    generations: int = Field(default=0, ge=0)
    discovered_kinds: list[Kind] = Field(default_factory=list, max_length=8)
    finished: bool = False
    resume_needed: bool = False
    exhausted: bool = False
    overflowed: bool = False
    stop_reason: StopReason = ""

    @field_validator("origin")
    @classmethod
    def plain_origin(cls, value: str) -> str:
        valid = navigation_url(value)
        if valid is None or value.rstrip("/") != url_origin(valid):
            raise ValueError("research origin must be a plain HTTPS origin")
        return url_origin(valid)

    @model_validator(mode="after")
    def private_addresses(self) -> Self:
        addresses = [*self.frontier, *self.visited, *self.evidence_urls]
        for key, portable in self.browser_plans.items():
            if key not in {f"bp{i}" for i in range(8)}:
                raise ValueError("invalid portable browser plan reference")
            addresses.extend([portable.start_url, portable.result_url])
        addresses.extend(url for url in (self.last_url, self.pending_url) if url is not None)
        addresses.extend(row.url for row in self.negative_outcomes if row.url is not None)
        for value in addresses:
            valid = navigation_url(value)
            if valid is None or valid != value or url_origin(valid) != self.origin:
                raise ValueError("research address is invalid or belongs to another source")
        if self.pending_planner_tokens > self.reserved_tokens:
            raise ValueError("pending planner allocation exceeds reserved tokens")
        return self

    def validate_for(self, origin: str, intent_key: str | None = None) -> None:
        # Assignment validation cannot see mutations of a list performed by a caller.
        checked = type(self).model_validate(self.model_dump())
        if checked.origin != origin or (
            intent_key is not None and checked.intent_key != intent_key
        ):
            raise ValueError("research checkpoint does not match the current source and intent")

    def remember_negative(self, reason: NegativeReason, url: str | None = None) -> None:
        row = NegativeOutcome(url=url, reason=reason, intent_key=self.intent_key)
        if row in self.negative_outcomes:
            return
        if len(self.negative_outcomes) >= MAX_NEGATIVE_OUTCOMES:
            self.overflowed = True
            return
        self.negative_outcomes = [*self.negative_outcomes, row]

    def merge_urls(self, name: Literal["frontier", "visited"], urls: list[str]) -> None:
        merged = list(dict.fromkeys([*getattr(self, name), *urls]))
        if len(merged) > MAX_KNOWN_URLS:
            self.overflowed = True
        setattr(self, name, merged[:MAX_KNOWN_URLS])

    def reserve_planner(self, tokens: int) -> None:
        if self.pending_planner_tokens:
            raise ValueError("previous planner allocation has not been reconciled")
        self.reserved_tokens += tokens
        self.pending_planner_tokens = tokens
        self.steps += 1

    def complete_planner(self, tokens: int) -> None:
        allocated = self.pending_planner_tokens
        # Clear the pending value first to preserve the validated reservation invariant.
        self.pending_planner_tokens = 0
        self.reserved_tokens -= allocated
        self.tokens += tokens

    def abandon_planner(self) -> None:
        if self.pending_planner_tokens:
            self.pending_planner_tokens = 0
            self.uncertain_calls += 1
        # The allocation remains charged: a remote response may have completed before crash.

    def exhausted_reason(self, limits: MemoryLimits) -> StopReason:
        if self.steps >= limits.max_steps:
            return "step_budget"
        if self.tokens + self.reserved_tokens >= limits.max_tokens:
            return "token_budget"
        if self.requests >= limits.max_requests:
            return "request_budget"
        if self.active_ms >= limits.max_active_ms:
            return "timeout"
        return ""
