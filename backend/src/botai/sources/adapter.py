"""Adapter protocol and the data that crosses it (docs/v4/06-decisions.md).

An adapter turns one connection's spec plus a session into `AdapterResult`: records by kind with
provenance, extracted documents and per-endpoint outcomes. It never touches the database and never
calls a model. Persisting is the Normalizer's job (`sources/normalize.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx

from botai.contracts.specs import Kind
from botai.core.browser import BrowserGateway, SessionState
from botai.core.http import RetryPolicy
from botai.core.vault import Vault

AdapterKind = Literal["recipe", "moodle_ws", "ical", "document"]

# External ids of the occurrences a repeating rule is unrolled into start with this. Such a row is
# not something the source listed: it exists while the rule says so (see `normalize._mark_missing`).
OCCURRENCE_PREFIX = "occ1:"

# Why an endpoint produced nothing usable. Structural problems mean the source no longer matches
# the spec (drift); the others are about data or transport.
Problem = Literal[
    "not_found",  # 404/410: the path is gone
    "status",  # unexpected 4xx
    "format",  # body is not what the spec describes (not JSON, itemsPath missing, wrong markup)
    "selector",  # HTML parsed but the item selector matched nothing while the page has content
    "bootstrap",  # a bootstrap step could not produce its parameters
    "fields",  # items found but none yields the required fields
    "empty",  # structure is fine, zero items
    "profile_missing",  # a table filter needs a profile value the user has not given
    "limit",  # bounded traversal stopped before completeness was established
    "no_progress",  # pagination repeated a previously seen result
    "unsupported_capability",  # a verified reader cannot execute the requested operation
]
STRUCTURAL_PROBLEMS: frozenset[str] = frozenset(
    {
        "not_found",
        "status",
        "format",
        "selector",
        "bootstrap",
        "fields",
        "limit",
        "no_progress",
        "unsupported_capability",
    }
)


class SessionExpired(Exception):
    """The source asked us to log in again (401/403, redirect to the login page, bad token).

    `reauth` says who can fix it: a browser login (`login`) or the user giving a new token or
    link (`token`).
    """

    def __init__(
        self,
        reason: str,
        *,
        reauth: Literal["login", "token"] = "login",
        code: Literal[
            "auth_missing", "auth_rejected", "auth_scope_mismatch", "login_redirect"
        ] = "auth_rejected",
        status: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reauth = reauth
        self.code: Literal[
            "auth_missing", "auth_rejected", "auth_scope_mismatch", "login_redirect"
        ] = code
        self.status = status


class NeedsUserInput(Exception):
    """The adapter cannot continue without a value only the user has (e.g. their full name)."""

    def __init__(self, param: str) -> None:
        super().__init__(param)
        self.param = param


@dataclass(frozen=True, slots=True)
class ExpiredRules:
    """Per-origin signs of an expired session, from the login preset (`expired`)."""

    redirect_path_prefixes: tuple[str, ...] = ()
    probe401_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class SyncContext:
    user_id: str
    connection_id: str
    origin: str
    allow_hosts: tuple[str, ...]
    now: datetime
    vault: Vault
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Moscow"))
    session: SessionState | None = None
    profile: Mapping[str, str] = field(default_factory=dict)  # fullName, email, group, studentId
    proxy_url: str | None = None
    transport: httpx.AsyncBaseTransport | None = None  # tests only
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    expired: ExpiredRules = field(default_factory=ExpiredRules)
    spec_revision: int | None = None
    content: bytes | None = field(default=None, repr=False)  # uploaded document bytes
    filename: str | None = None
    known_hash: str | None = None  # hash of the previously ingested document
    browser: BrowserGateway | None = field(default=None, repr=False)
    before_request: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Record:
    """One domain entity as the source gave it, in the vocabulary of `sources/vocab.py`."""

    kind: Kind
    external_id: str
    fields: Mapping[str, Any]
    source_url: str
    open_url: str | None = None  # the page of the source that shows this record to the student


@dataclass(slots=True)
class ExtractedDocument:
    title: str
    kind: Literal["file", "sheet", "page"]
    hash: str
    chunks: list[str]
    mime: str | None = None
    url: str | None = None


@dataclass(slots=True)
class EndpointOutcome:
    endpoint_id: str
    kind: Kind | None
    ok: bool
    items_seen: int = 0
    items_valid: int = 0
    problem: Problem | None = None
    source_url: str = ""
    transport: Literal["http", "browser"] = "http"
    status: int | None = None
    binding_id: str | None = None
    windows: int = 0  # date windows answered by a dateWindow endpoint
    # `[start, end)` of the days a weekly expansion looked at in this read. What the rules do not
    # produce inside it is gone; outside it they said nothing.
    expanded: tuple[datetime, datetime] | None = None
    window_items: list[int] = field(default_factory=list[int])  # records of each window

    @property
    def structural(self) -> bool:
        return self.problem in STRUCTURAL_PROBLEMS


@dataclass(slots=True)
class AdapterResult:
    records: list[Record]
    fetched_at: datetime
    outcomes: list[EndpointOutcome] = field(default_factory=list)
    documents: list[ExtractedDocument] = field(default_factory=list)
    spec_revision: int | None = None
    unchanged: bool = False  # document with the same hash as before: nothing to ingest

    def kinds_with_problem(self) -> set[Kind]:
        return {o.kind for o in self.outcomes if o.kind and not o.ok}

    def complete_kinds(self) -> set[Kind]:
        """Kinds for which every endpoint finished: only these may mark absent rows as missing."""
        bad = self.kinds_with_problem()
        return {o.kind for o in self.outcomes if o.kind and o.kind not in bad}

    def structural_problems(self) -> list[EndpointOutcome]:
        return [o for o in self.outcomes if o.structural]

    def count(self, kind: Kind) -> int:
        return sum(1 for r in self.records if r.kind == kind)


class SourceAdapter(Protocol):
    kind: ClassVar[AdapterKind]

    async def fetch(self, ctx: SyncContext) -> AdapterResult: ...
