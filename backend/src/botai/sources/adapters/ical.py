"""`ical` adapter: an iCal export link (Vault `kind=ical_url`) -> classes and deadlines.

The link carries the user's secret, so it lives in the Vault and never in the spec. A VEVENT is a
deadline when `classify.deadlineIf` says so (zero duration by default, a category, a summary
pattern); otherwise it is a class. Recurring events are expanded inside the sync horizon.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar

from botai.contracts.specs import IcalSpec
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import host_allowed
from botai.core.vault import SecretKind
from botai.sources import rxsafe
from botai.sources.adapter import (
    AdapterKind,
    AdapterResult,
    EndpointOutcome,
    Record,
    SessionExpired,
    SyncContext,
)
from botai.sources.extract import clean_text, collapse, to_utc_iso
from botai.sources.fetch import SourceClient, origin_host

HORIZON_DAYS = 90
LOOKBACK_DAYS = 1
MAX_OCCURRENCES = 400
MAX_EVENTS = 5000


def _as_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=tz)
    return None


def _prop(comp: Any, name: str) -> Any:
    got = comp.get(name)
    return got.dt if got is not None and hasattr(got, "dt") else None


def _text(comp: Any, name: str) -> str:
    got = comp.get(name)
    return clean_text(collapse(str(got))) if got is not None else ""


def _categories(comp: Any) -> list[str]:
    raw = comp.get("CATEGORIES")
    items = raw if isinstance(raw, list) else [raw] if raw is not None else []
    out: list[str] = []
    for item in items:
        cats = getattr(item, "cats", None)
        if cats:
            out.extend(str(c) for c in cats)
    return out


def _external_id(uid: str, start: datetime | None = None) -> str:
    base = uid if len(uid) <= 120 else hashlib.sha256(uid.encode()).hexdigest()
    if start is None:
        return base
    return f"{base}#{start.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _outcomes() -> list[EndpointOutcome]:
    return [
        EndpointOutcome("events", "scheduleEvents", ok=True),
        EndpointOutcome("deadlines", "deadlines", ok=True),
    ]


@dataclass(frozen=True, slots=True)
class Reading:
    """What a calendar link answered. `problem` is `gone` when the service no longer knows the link
    (revoked, regenerated, mistyped), `status` for any other HTTP failure, `format` when the
    answer is not an iCalendar file."""

    calendar: Calendar | None
    status: int
    problem: Literal["gone", "status", "format"] | None


async def read_link(ctx: SyncContext, url: str) -> Reading:
    """Fetch a calendar link through the EgressProxy, without cookies, and parse it.

    The caller's `allow_hosts` hold the link's host only: a redirect to any other host is refused
    by the client (`egress_blocked`), a slow or oversized answer by its time and size caps.
    """
    if not host_allowed(url, ctx.allow_hosts):
        raise BotaiError(
            ErrorCode.EGRESS_BLOCKED,
            "Ссылка на календарь ведёт на хост вне разрешённых",
            extra={"host": origin_host(url)},
        )
    async with SourceClient(ctx, auth_type="none", use_session=False) as client:
        response = await client.request("GET", url, check_session=False)
    if response.status_code in (401, 403, 404, 410):
        return Reading(None, response.status_code, "gone")
    if response.status_code >= 400:
        return Reading(None, response.status_code, "status")
    try:
        calendar = Calendar.from_ical(response.content)
    except Exception:
        return Reading(None, response.status_code, "format")
    if calendar.name != "VCALENDAR":  # a contact card or any other iCalendar-family file
        return Reading(None, response.status_code, "format")
    return Reading(calendar, response.status_code, None)


class IcalAdapter:
    kind: ClassVar[AdapterKind] = "ical"

    def __init__(self, spec: IcalSpec) -> None:
        self.spec = spec

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        host = origin_host(self.spec.origin)
        if host not in {h.lower() for h in ctx.allow_hosts}:
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                "Адрес календаря не входит в разрешённые для этого подключения",
                extra={"host": host},
            )
        secret = await ctx.vault.get(ctx.user_id, ctx.connection_id, SecretKind.ICAL_URL)
        if secret is None:
            raise SessionExpired("no calendar link stored", reauth="token")
        reading = await read_link(ctx, secret.get_secret_value())
        if reading.problem == "gone":
            raise SessionExpired("the calendar link no longer works", reauth="token")
        if reading.calendar is None:
            outcomes = _outcomes()
            for o in outcomes:
                o.ok, o.problem = False, reading.problem
            return AdapterResult([], ctx.now, outcomes, spec_revision=ctx.spec_revision)
        return self.result_for(reading.calendar, ctx)

    def result_for(self, calendar: Calendar, ctx: SyncContext) -> AdapterResult:
        """The records of a calendar that was read already (the link check reads it once)."""
        records = self._records(calendar, ctx)
        outcomes = _outcomes()
        for o in outcomes:
            o.items_seen = o.items_valid = sum(1 for r in records if r.kind == o.kind)
            if o.items_valid == 0:
                o.problem = "empty"
        return AdapterResult(records, ctx.now, outcomes, spec_revision=ctx.spec_revision)

    # --- classification ----------------------------------------------------------------------

    def _is_deadline(
        self, comp: Any, start: datetime | None, end: datetime | None, title: str
    ) -> bool:
        classify = self.spec.classify or {}
        cond: dict[str, Any] = classify.get("deadlineIf") or {"zeroDuration": True}
        if cond.get("zeroDuration", True) and start is not None:
            raw_start = _prop(comp, "DTSTART")
            if isinstance(raw_start, datetime) and (end is None or end == start):
                return True
        cats = {c.casefold() for c in _categories(comp)}
        if cats & {str(c).casefold() for c in cond.get("categoryIn", [])}:
            return True
        pattern = cond.get("summaryPattern")
        if pattern and not rxsafe.problems(pattern):
            return rxsafe.first_group(pattern, title) is not None
        return False

    # --- records -----------------------------------------------------------------------------

    def _records(self, calendar: Calendar, ctx: SyncContext) -> list[Record]:
        window_start = ctx.now - timedelta(days=LOOKBACK_DAYS)
        window_end = ctx.now + timedelta(days=HORIZON_DAYS)
        components = list(calendar.walk("VEVENT"))[:MAX_EVENTS]
        overrides: dict[str, set[datetime]] = {}
        for comp in components:
            rid = _as_datetime(_prop(comp, "RECURRENCE-ID"), ctx.tz)
            if rid is not None:
                overrides.setdefault(str(comp.get("UID")), set()).add(rid)
        out: dict[str, Record] = {}
        for comp in components:
            if _text(comp, "STATUS").upper() == "CANCELLED":
                continue
            uid = str(comp.get("UID") or "")
            if not uid:
                continue
            start = _as_datetime(_prop(comp, "DTSTART"), ctx.tz)
            if start is None:
                continue
            end = _as_datetime(_prop(comp, "DTEND"), ctx.tz)
            duration = _prop(comp, "DURATION")
            if end is None and isinstance(duration, timedelta):
                end = start + duration
            rid = _as_datetime(_prop(comp, "RECURRENCE-ID"), ctx.tz)
            if comp.get("RRULE") is not None and rid is None:
                for occ_start in self._occurrences(comp, start, window_start, window_end, ctx):
                    if occ_start in overrides.get(uid, set()):
                        continue
                    occ_end = occ_start + (end - start) if end else None
                    rec = self._record(comp, _external_id(uid, occ_start), occ_start, occ_end, ctx)
                    if rec:
                        out[rec.external_id] = rec
                continue
            ext = _external_id(uid, start if rid is not None else None)
            rec = self._record(comp, ext, start, end, ctx)
            if rec:
                out[ext + rec.kind] = rec
        for todo in calendar.walk("VTODO"):
            rec = self._todo(todo, ctx)
            if rec:
                out[rec.external_id + "todo"] = rec
        return list(out.values())

    def _occurrences(
        self, comp: Any, start: datetime, lo: datetime, hi: datetime, ctx: SyncContext
    ) -> list[datetime]:
        try:
            text = comp.get("RRULE").to_ical().decode()
            rule = rrulestr(text, dtstart=start)
            found: list[datetime] = []
            for occ in rule.between(lo, hi, inc=True):
                found.append(occ if occ.tzinfo else occ.replace(tzinfo=ctx.tz))
                if len(found) >= MAX_OCCURRENCES:
                    break
        except (ValueError, TypeError, OverflowError):
            return [start] if lo <= start <= hi else []
        excluded: set[datetime] = set()
        raw = comp.get("EXDATE")
        for group in raw if isinstance(raw, list) else [raw] if raw is not None else []:
            for dt in getattr(group, "dts", []):
                value = _as_datetime(dt.dt, ctx.tz)
                if value is not None:
                    excluded.add(value)
        return [o for o in found if o not in excluded]

    def _record(
        self, comp: Any, ext: str, start: datetime, end: datetime | None, ctx: SyncContext
    ) -> Record | None:
        title = _text(comp, "SUMMARY")
        if not title:
            return None
        url_prop = comp.get("URL")
        url = str(url_prop) if url_prop and str(url_prop).startswith("https://") else None
        source_url = url or self.spec.origin
        if self._is_deadline(comp, start, end, title):
            due = end if end is not None and end != start else start
            state = "open" if due >= ctx.now else "unknown"
            return Record(
                "deadlines",
                ext,
                {"title": title, "dueAt": to_utc_iso(due), "state": state, "url": url},
                source_url,
            )
        all_day = not isinstance(_prop(comp, "DTSTART"), datetime)
        if all_day and end is None:
            end = start + timedelta(days=1)
        cats = _categories(comp)
        return Record(
            "scheduleEvents",
            ext,
            {
                k: v
                for k, v in {
                    "title": title,
                    "startsAt": to_utc_iso(start),
                    "endsAt": to_utc_iso(end if end is not None else start),
                    "location": _text(comp, "LOCATION") or None,
                    "kind": cats[0] if cats else None,
                    "url": url,
                }.items()
                if v is not None
            },
            source_url,
        )

    def _todo(self, todo: Any, ctx: SyncContext) -> Record | None:
        uid = str(todo.get("UID") or "")
        title = _text(todo, "SUMMARY")
        due = _as_datetime(_prop(todo, "DUE"), ctx.tz)
        if not uid or not title or due is None:
            return None
        status = _text(todo, "STATUS").upper()
        if status == "CANCELLED":
            return None
        state = "submitted" if status == "COMPLETED" else ("open" if due >= ctx.now else "unknown")
        return Record(
            "deadlines",
            _external_id(uid),
            {"title": title, "dueAt": to_utc_iso(due), "state": state},
            self.spec.origin,
        )
