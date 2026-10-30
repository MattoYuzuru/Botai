"""Probe: repeat an observed successful GET with other query values (FR-039).

The model may ask how the source answers for another week or page, but it never writes a request.
The executor repeats the *observed* request of the same origin and changes only values of query
keys that request already had: a date template (`{plusDays:7}`, `{weekStart}`...) or a literal the
observation has already shown. Method, host, path and body stay as they were, the student's own
session is used exactly as the browser used it, and the answer becomes new evidence with the number
of records and its shape. Nothing here writes to the source or reaches a second host.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser

from botai.browser.gateway import scrub_sample
from botai.core.browser import ObservedRequest
from botai.core.errors import BotaiError
from botai.learn.lint import SECRET_NAME
from botai.sources import templates
from botai.sources.adapter import SessionExpired, SyncContext
from botai.sources.fetch import SourceClient

MAX_OVERRIDES = 4
_VALUE = re.compile(r"^[A-Za-z0-9_.:+/-]{1,60}$")
_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLACEHOLDER = re.compile(r"\{(?:today|weekStart|(?:plusDays|minusDays):(?P<days>\d{1,4}))\}")
MAX_DAYS = 62


class ProbeRefused(Exception):
    """The requested change is not a probe. Only a static reason, never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProbeShape:
    """How much the answer holds: objects per nesting level of lists, outermost first.

    `[{date, lessons: [...]}, ...]` is `(7, 9)`: seven days with nine classes in all. `records` is
    the deepest level, the one that usually holds the entries themselves.
    """

    levels: tuple[int, ...] = ()

    @property
    def top_level(self) -> int:
        return self.levels[0] if self.levels else 0

    @property
    def records(self) -> int:
        return self.levels[-1] if self.levels else 0


@dataclass(frozen=True, slots=True)
class ProbeReply:
    status: int
    text: str  # the sample kept as evidence (scrubbed, bounded)
    content_type: str
    shape: ProbeShape


Prober = Callable[[ObservedRequest, str], Awaitable[ProbeReply | None]]


def observed_literals(calls: Collection[ObservedRequest]) -> dict[str, frozenset[str]]:
    """Query values the observation has already shown, per key: the only literals a probe may
    reuse. A value seen for one key is not a value for another."""
    found: dict[str, set[str]] = {}
    for call in calls:
        for key, value in parse_qsl(urlsplit(call.url).query, keep_blank_values=True):
            if value:
                found.setdefault(key, set()).add(value)
    return {key: frozenset(values) for key, values in found.items()}


def probe_url(
    call: ObservedRequest,
    overrides: Mapping[str, str],
    *,
    origin: str,
    literals: Mapping[str, Collection[str]],
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """The observed URL with changed values of its own query keys, or a static refusal.

    A value is exactly one date placeholder (`{today}`, `{weekStart}`, `{plusDays:N}`,
    `{minusDays:N}`, N up to 62) for a key whose observed value is an ISO date, or a literal the
    observation already showed for that same key. The text of the model is never spliced."""
    if call.method != "GET" or not (call.status and 200 <= call.status < 300):
        raise ProbeRefused("not_a_successful_get")
    if call.resource_type not in {"xhr", "fetch"} or not call.auth_proven:
        raise ProbeRefused("not_an_observed_data_request")
    parts = urlsplit(call.url)
    if f"{parts.scheme}://{parts.netloc}" != origin or parts.fragment:
        raise ProbeRefused("other_origin")
    if not 1 <= len(overrides) <= MAX_OVERRIDES:
        raise ProbeRefused("override_count")
    pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32)
    current = dict(pairs)
    changed: dict[str, str] = {}
    for key, raw in overrides.items():
        if key not in current or not _KEY.match(key) or SECRET_NAME.search(key):
            raise ProbeRefused("unknown_query_key")
        placeholder = _PLACEHOLDER.fullmatch(raw)
        if placeholder is not None:
            if _ISO_DATE.fullmatch(current[key]) is None:
                raise ProbeRefused("not_a_date_key")
            if placeholder["days"] is not None and int(placeholder["days"]) > MAX_DAYS:
                raise ProbeRefused("date_out_of_range")
            value = templates.render(raw, now=now, tz=tz, params={})
        elif raw in literals.get(key, ()):
            value = raw
        else:
            raise ProbeRefused("value_not_observed")
        if _VALUE.fullmatch(value) is None:
            raise ProbeRefused("value_not_observed")
        changed[key] = value
    query = urlencode([(key, changed.get(key, value)) for key, value in pairs])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def shape_of(text: str) -> ProbeShape:
    """Counts only; the body itself is shaped for the model by the usual redacting views."""
    text = text.strip()
    if text[:1] in "{[":
        try:
            return _json_shape(json.loads(text))
        except (ValueError, RecursionError):
            return ProbeShape()
    if "<" in text[:400]:
        rows = len(LexborHTMLParser(text).css("tr, li, article"))
        return ProbeShape((rows,))
    return ProbeShape()


def _json_shape(document: Any) -> ProbeShape:
    levels: dict[int, int] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > 5:
            return
        if isinstance(node, list):
            objects = [item for item in node if isinstance(item, dict)]
            if objects or not node:
                levels[depth] = levels.get(depth, 0) + len(objects)
            for item in objects:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            for item in list(node.values())[:40]:
                walk(item, depth)

    walk(document, 0)
    return ProbeShape(tuple(levels[depth] for depth in sorted(levels)))


def http_prober(context: Callable[[], Awaitable[SyncContext]]) -> Prober:
    """Repeat a request with the student's credentials as the browser sent them (read-only GET)."""

    async def run(call: ObservedRequest, url: str) -> ProbeReply | None:
        sync_context = await context()
        binding = {
            "cookies": call.has_cookie,
            "bearer": "observedAuthorization" if call.has_authorization else "none",
        }
        try:
            async with SourceClient(sync_context, auth_type="session") as client:
                response = await client.request("GET", url, auth_binding=binding)
        except (SessionExpired, BotaiError):
            return None
        sample = scrub_sample(response.text)
        if sample is None:
            return None  # an answer that looks like a credential is never kept as evidence
        return ProbeReply(
            response.status_code,
            sample,
            response.headers.get("content-type", "").split(";")[0],
            shape_of(response.text),  # counted on the whole body, not on the kept sample
        )

    return run
