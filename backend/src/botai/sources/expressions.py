"""Bounded recipe-v2 data expressions. No expression evaluates source code or makes requests.

Collections form one nested chain, not a join. Scalar sources retain parent bindings while the
leaf changes; nothing is looked up outside the response. Dates require a full observed year and
an explicit IANA zone, and recurrence is expanded only inside a bounded, moving horizon.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.contracts.specs import (
    HtmlCollection,
    HtmlComposedField,
    HtmlDateTime,
    HtmlField,
    HtmlResponse,
    HtmlScalar,
    HtmlWeekly,
    JsonComposedField,
    JsonDateTime,
    JsonField,
    JsonResponse,
    JsonScalar,
    JsonWeekly,
)
from botai.sources.extract import (
    MISSING,
    apply_transform,
    html_value,
    parse_datetime,
    resolve,
    to_utc_iso,
)
from botai.sources.templates import render

MAX_COLLECTION_NODES = 10_000
MAX_LEAVES = 5_000
MAX_INSTANCES = 5_000

Scalar = JsonScalar | HtmlScalar
FieldExpression = JsonField | HtmlField | JsonComposedField | HtmlComposedField
Weekly = JsonWeekly | HtmlWeekly


class ExpressionError(Exception):
    """Static failure classification only: response values never enter error strings."""

    def __init__(self, problem: Literal["format", "fields", "limit"]) -> None:
        super().__init__(problem)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class CollectionPage:
    root_count: int
    nodes_seen: int
    items: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Occurrence:
    # Nominal local time identifies the instance even if a later recipe changes its duration.
    local_date: date
    local_time: time
    starts_at: str
    ends_at: str


def _children(parent: Any, source: str, *, html: bool) -> list[Any]:
    if html:
        if not isinstance(parent, (LexborNode, LexborHTMLParser)):
            raise ExpressionError("format")
        try:
            return list(parent.css(source))
        except Exception:
            raise ExpressionError("format") from None
    value = resolve(parent, source)
    if not isinstance(value, list):
        raise ExpressionError("format")
    return value


def collect(
    document: Any,
    response: JsonResponse | HtmlResponse,
    *,
    remaining_nodes: int = MAX_COLLECTION_NODES,
    remaining_leaves: int = MAX_LEAVES,
) -> CollectionPage:
    """Return fresh parent bindings and the first collection's pagination cardinality."""
    html = isinstance(response, HtmlResponse)
    collections = response.collections
    if collections is None:
        source = (
            response.items_selector if isinstance(response, HtmlResponse) else response.items_path
        )
        assert source is not None
        children = _children(document, source, html=html)
        if len(children) > min(remaining_nodes, remaining_leaves):
            raise ExpressionError("limit")
        return CollectionPage(
            len(children),
            len(children),
            [{"root": document, "item": child} for child in children],
        )
    contexts: list[dict[str, Any]] = [{"root": document, "item": document}]
    visited = 0
    root_count = 0
    for index, collection in enumerate(collections):
        next_contexts: list[dict[str, Any]] = []
        source = collection.selector if isinstance(collection, HtmlCollection) else collection.path
        for context in contexts:
            children = _children(context["item"], source, html=html)
            visited += len(children)
            if visited > remaining_nodes:
                raise ExpressionError("limit")
            if index == 0:
                root_count = len(children)
            for child in children:
                next_contexts.append({**context, collection.as_: child, "item": child})
            if len(next_contexts) > MAX_COLLECTION_NODES:
                raise ExpressionError("limit")
        contexts = next_contexts
    if len(contexts) > remaining_leaves:
        raise ExpressionError("limit")
    return CollectionPage(root_count, visited, contexts)


def scalar_value(source: Scalar, bindings: Mapping[str, Any], tz: ZoneInfo) -> Any:
    """Read an untransformed scalar (or recurrence array) from a declared binding."""
    parent = bindings.get(source.from_, MISSING)
    if parent is MISSING:
        raise ExpressionError("fields")
    if isinstance(source, JsonScalar):
        value = resolve(parent, source.path)
        if (value is MISSING or value is None) and source.fallback_path is not None:
            value = resolve(parent, source.fallback_path)
        return value
    if isinstance(parent, LexborHTMLParser):
        # The root document is a parser, whereas html_value consumes a node.
        parent = parent.root
    if not isinstance(parent, LexborNode):
        raise ExpressionError("fields")
    return html_value(
        parent,
        selector=source.selector,
        attr=source.attr,
        pattern=source.pattern,
        transform=None,
        tz=tz,
    )


def field_value(expression: FieldExpression, bindings: Mapping[str, Any], tz: ZoneInfo) -> Any:
    if isinstance(expression, (JsonComposedField, HtmlComposedField)):
        # Date ambiguity is a structural failure even if the author omitted required=true.
        return compose_datetime(expression.compose, bindings, tz)
    if isinstance(expression, HtmlField):
        parent = bindings.get(expression.from_, MISSING)
        if isinstance(parent, LexborHTMLParser):
            parent = parent.root
        if not isinstance(parent, LexborNode):
            raise ExpressionError("fields")
        value = html_value(
            parent,
            selector=expression.selector,
            attr=expression.attr,
            pattern=expression.pattern,
            transform=expression.transform,
            tz=tz,
        )
    else:
        value = apply_transform(expression.transform, scalar_value(expression, bindings, tz), tz)
    if value is None and expression.required:
        raise ExpressionError("fields")
    return value


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_DATE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{4}$")
_TEXT_DATE = re.compile(r"^\d{1,2}\s+[а-яё]+\s+\d{4}(?:\s*(?:г\.?|года))?$", re.IGNORECASE)
_CLOCK = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def calendar_date(value: Any) -> date:
    """Date-only with an observed four-digit year. Never infer a year or discard a time."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ExpressionError("fields")
    text = value.strip()
    if not any(pattern.fullmatch(text) for pattern in (_ISO_DATE, _NUMERIC_DATE, _TEXT_DATE)):
        raise ExpressionError("fields")
    parsed = parse_datetime(text, ZoneInfo("UTC"))
    if parsed is None:
        raise ExpressionError("fields")
    return parsed.date()


def clock_time(value: Any) -> time:
    if isinstance(value, time) and value.tzinfo is None:
        return value
    if not isinstance(value, str) or not _CLOCK.fullmatch(value.strip()):
        raise ExpressionError("fields")
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ExpressionError("fields") from None


def local_datetime(
    day: date,
    clock: time,
    zone: ZoneInfo,
    fold: Literal["reject", "earlier", "later"],
) -> datetime:
    naive = datetime.combine(day, clock)
    # A round trip catches spring-forward gaps; two distinct instants catch autumn folds.
    instants: set[datetime] = set()
    for choice in (0, 1):
        try:
            instant = naive.replace(tzinfo=zone, fold=choice).astimezone(UTC)
            if instant.astimezone(zone).replace(tzinfo=None) == naive:
                instants.add(instant)
        except (ValueError, OverflowError):
            raise ExpressionError("fields") from None
    if not instants or (len(instants) > 1 and fold == "reject"):
        raise ExpressionError("fields")
    return max(instants) if fold == "later" else min(instants)


def compose_datetime(
    expression: JsonDateTime | HtmlDateTime,
    bindings: Mapping[str, Any],
    tz: ZoneInfo,
) -> str:
    day = calendar_date(scalar_value(expression.date, bindings, tz))
    clock = clock_time(scalar_value(expression.time, bindings, tz))
    try:
        day += timedelta(days=expression.day_offset)
    except OverflowError:
        raise ExpressionError("fields") from None
    return to_utc_iso(local_datetime(day, clock, ZoneInfo(expression.time_zone), expression.fold))


def _dates(source: Scalar | None, bindings: Mapping[str, Any], zone: ZoneInfo) -> set[date]:
    if source is None:
        return set()
    values = scalar_value(source, bindings, zone)
    if values is MISSING or values is None:
        raise ExpressionError("fields")
    values = values if isinstance(values, list) else [values]
    if len(values) > MAX_COLLECTION_NODES:
        raise ExpressionError("limit")
    return {calendar_date(value) for value in values}


def _weekdays(value: Any) -> set[int]:
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > 7:
        raise ExpressionError("fields")
    # HTML source text may contain a single ISO weekday; no locale-name guessing or separators.
    days: set[int] = set()
    for value in values:
        if isinstance(value, str) and len(value) == 1 and value in "1234567":
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 7:
            raise ExpressionError("fields")
        days.add(value)
    return days


def expand_weekly(
    expression: Weekly,
    bindings: Mapping[str, Any],
    *,
    now: datetime,
    remaining_instances: int = MAX_INSTANCES,
) -> list[Occurrence]:
    zone = ZoneInfo(expression.time_zone)
    first = calendar_date(scalar_value(expression.start_date, bindings, zone))
    last = calendar_date(scalar_value(expression.until_date, bindings, zone))
    if last < first:
        raise ExpressionError("fields")
    horizon = calendar_date(render(expression.horizon.from_, now=now, tz=zone, params={}))
    try:
        horizon_end = horizon + timedelta(days=expression.horizon.days - 1)
    except OverflowError:
        raise ExpressionError("fields") from None
    weekdays = _weekdays(scalar_value(expression.weekdays, bindings, zone))
    clock = clock_time(scalar_value(expression.start_time, bindings, zone))
    end_clock = (
        clock_time(scalar_value(expression.end_time, bindings, zone))
        if expression.end_time is not None
        else None
    )
    duration_source = expression.duration_minutes
    duration = (
        scalar_value(duration_source, bindings, zone)
        if isinstance(duration_source, (JsonScalar, HtmlScalar))
        else duration_source
    )
    if duration is not None:
        if isinstance(duration, str) and duration.isascii() and duration.isdecimal():
            duration = int(duration)
        if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 1440:
            raise ExpressionError("fields")
    anchor = (
        calendar_date(scalar_value(expression.anchor_date, bindings, zone))
        if expression.anchor_date is not None
        else first
    )
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    exclude = _dates(expression.exclude_dates, bindings, zone)
    include = _dates(expression.include_dates, bindings, zone)
    begin, end = max(first, horizon), min(last, horizon_end)
    result: list[Occurrence] = []
    if begin > end:
        return result
    for offset in range((end - begin).days + 1):
        day = begin + timedelta(days=offset)
        weeks = (day - anchor_monday).days // 7
        scheduled = day.isoweekday() in weekdays and weeks % expression.interval_weeks == 0
        if (not scheduled and day not in include) or day in exclude:
            continue
        starts = local_datetime(day, clock, zone, expression.fold)
        if end_clock is not None:
            try:
                end_day = day + timedelta(days=expression.end_day_offset)
            except OverflowError:
                raise ExpressionError("fields") from None
            ends = local_datetime(end_day, end_clock, zone, expression.fold)
        else:
            assert isinstance(duration, int)
            ends = starts + timedelta(minutes=duration)
        if not timedelta(0) < ends - starts <= timedelta(days=1):
            raise ExpressionError("fields")
        result.append(Occurrence(day, clock, to_utc_iso(starts), to_utc_iso(ends)))
        if len(result) > min(expression.max_occurrences, remaining_instances):
            raise ExpressionError("limit")
    return result
