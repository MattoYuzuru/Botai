"""`moodle_ws` adapter: Moodle Web Services with the user's own token (Vault `kind=token`).

Only functions from the allowlist below are ever called; it equals the enum in
`contracts/adapter-spec.schema.json` (a test compares them). All are reads. The token goes in the
POST body, never in the URL, so it cannot reach an access log or a stored `source_url`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from botai.contracts.specs import Kind, MoodleWsSpec
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import SecretKind
from botai.sources.adapter import (
    AdapterKind,
    AdapterResult,
    EndpointOutcome,
    Problem,
    Record,
    SessionExpired,
    SyncContext,
)
from botai.sources.extract import clean_text, collapse, html_to_text, to_utc_iso
from botai.sources.fetch import SourceClient, origin_host

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_calendar_get_action_events_by_timesort",
        "core_calendar_get_calendar_upcoming_view",
        "mod_assign_get_assignments",
        "mod_assign_get_submission_status",
        "gradereport_user_get_grade_items",
    }
)
SERVER_PATH = "/webservice/rest/server.php"
MAX_SUBMISSION_CALLS = 30
MAX_GRADE_CALLS = 20
EVENT_PAGE = 50
EVENT_PAGES = 3
_DEADLINE_EVENTS = {"due", "close", "gradingdue", "expectcompletionon"}


def _ts(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return to_utc_iso(datetime.fromtimestamp(float(value), tz=UTC))


def _flatten(args: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in args.items():
        if isinstance(value, list):
            for i, item in enumerate(value):
                out[f"{key}[{i}]"] = str(item)
        else:
            out[key] = str(value)
    return out


class _FunctionFailed(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem)
        self.problem: Problem = problem


class MoodleWsAdapter:
    kind: ClassVar[AdapterKind] = "moodle_ws"

    def __init__(self, spec: MoodleWsSpec) -> None:
        self.spec = spec
        self._records: dict[tuple[str, str], Record] = {}
        self._assign_due: dict[int, str] = {}
        self._partial = False  # a per-course/per-assignment call failed inside a function
        unknown = set(spec.functions) - ALLOWED_FUNCTIONS
        if unknown:
            raise BotaiError(ErrorCode.RECIPE_REJECTED, "Функция Moodle не входит в allowlist")

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        spec = self.spec
        host = origin_host(spec.origin)
        if host not in {h.lower() for h in ctx.allow_hosts}:
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                "Адрес Moodle не входит в разрешённые для этого подключения",
                extra={"host": host},
            )
        token = await ctx.vault.get(ctx.user_id, ctx.connection_id, SecretKind.TOKEN)
        if token is None:
            raise SessionExpired("no Moodle token stored", reauth="token")
        secret = token.get_secret_value()
        self._records = {}
        self._assign_due = {}
        self._partial = False
        outcomes: list[EndpointOutcome] = []

        async with SourceClient(ctx, auth_type="none", use_session=False) as client:

            async def call(function: str, **args: Any) -> Any:
                if function not in ALLOWED_FUNCTIONS:
                    raise BotaiError(ErrorCode.RECIPE_REJECTED, "Функция вне allowlist")
                response = await client.request(
                    "POST",
                    spec.origin + SERVER_PATH,
                    data={
                        "wstoken": secret,
                        "wsfunction": function,
                        "moodlewsrestformat": "json",
                        **_flatten(args),
                    },
                    check_session=False,
                )
                if response.status_code in (404, 410):
                    raise _FunctionFailed("not_found")
                if response.status_code >= 400:
                    raise _FunctionFailed("status")
                try:
                    doc: Any = response.json()
                except (ValueError, RecursionError):
                    raise _FunctionFailed("format") from None
                if isinstance(doc, dict) and "exception" in doc:
                    code = str(doc.get("errorcode", ""))
                    if code in {"invalidtoken", "invalidlogin", "tokenexpired"}:
                        raise SessionExpired("Moodle rejected the token", reauth="token")
                    raise _FunctionFailed("status")
                return doc

            site = await self._site_info(call)
            userid = site["userid"] if site else None
            courses: list[dict[str, Any]] = []
            wanted = [f for f in spec.functions if f != "core_webservice_get_site_info"]
            horizon = ctx.now + timedelta(days=spec.horizon_days)
            for function in wanted:
                outcome = EndpointOutcome(endpoint_id=function, kind=None, ok=True)
                outcomes.append(outcome)
                try:
                    if userid is None and function != "core_calendar_get_calendar_upcoming_view":
                        raise _FunctionFailed("bootstrap")
                    count = await self._run(function, call, ctx, userid, courses, horizon, outcome)
                except _FunctionFailed as exc:
                    outcome.ok = False
                    outcome.problem = exc.problem
                    continue
                outcome.items_seen = outcome.items_valid = count
                if self._partial:
                    outcome.ok, outcome.problem = False, "status"
                    self._partial = False
                elif count == 0:
                    outcome.problem = "empty"
        return AdapterResult(
            records=list(self._records.values()),
            fetched_at=ctx.now,
            outcomes=outcomes,
            spec_revision=ctx.spec_revision,
        )

    # --- helpers -----------------------------------------------------------------------------

    def _add(self, kind: Kind, external_id: str, fields: dict[str, Any], url: str) -> None:
        clean = {k: v for k, v in fields.items() if v is not None}
        self._records[(kind, external_id)] = Record(kind, external_id[:200], clean, url)

    async def _site_info(self, call: Any) -> dict[str, Any] | None:
        try:
            doc = await call("core_webservice_get_site_info")
        except _FunctionFailed:
            return None
        if isinstance(doc, dict) and isinstance(doc.get("userid"), int):
            return doc
        return None

    async def _run(
        self,
        function: str,
        call: Any,
        ctx: SyncContext,
        userid: int | None,
        courses: list[dict[str, Any]],
        horizon: datetime,
        outcome: EndpointOutcome,
    ) -> int:
        origin = self.spec.origin
        now_ts = int(ctx.now.timestamp())
        if function == "core_enrol_get_users_courses":
            doc = await call(function, userid=userid)
            if not isinstance(doc, list):
                raise _FunctionFailed("format")
            outcome.kind = "courses"
            n = 0
            for c in doc:
                if not isinstance(c, dict) or not isinstance(c.get("id"), int):
                    continue
                courses.append(c)
                end = c.get("enddate")
                self._add(
                    "courses",
                    str(c["id"]),
                    {
                        "title": clean_text(
                            collapse(str(c.get("fullname") or c.get("shortname") or ""))
                        ),
                        "code": clean_text(str(c["shortname"])) if c.get("shortname") else None,
                        "url": f"{origin}/course/view.php?id={c['id']}",
                        "archived": bool(isinstance(end, int) and 0 < end < now_ts),
                    },
                    f"{origin}/course/view.php?id={c['id']}",
                )
                n += 1
            return n
        if function == "core_calendar_get_action_events_by_timesort":
            outcome.kind = "deadlines"
            return await self._action_events(call, ctx, horizon)
        if function == "core_calendar_get_calendar_upcoming_view":
            doc = await call(function)
            events = doc.get("events") if isinstance(doc, dict) else None
            if not isinstance(events, list):
                raise _FunctionFailed("format")
            outcome.kind = "scheduleEvents"
            return self._calendar_events(events, ctx)
        if function == "mod_assign_get_assignments":
            return await self._assignments(call, ctx, courses, outcome)
        if function == "mod_assign_get_submission_status":
            outcome.kind = "deadlines"
            return await self._submission_status(call, ctx, userid)
        if function == "gradereport_user_get_grade_items":
            outcome.kind = "grades"
            return await self._grades(call, ctx, userid, courses)
        raise _FunctionFailed("format")

    # --- calendar ----------------------------------------------------------------------------

    def _deadline_from_event(self, ev: dict[str, Any], origin: str) -> bool:
        due = _ts(ev.get("timesort") or ev.get("timestart"))
        if due is None or not ev.get("name"):
            return False
        module, instance = ev.get("modulename"), ev.get("instance")
        ext = f"{module}-{instance}" if module and instance else f"event-{ev.get('id')}"
        raw_course = ev.get("course")
        course: dict[str, Any] = raw_course if isinstance(raw_course, dict) else {}
        link = ev.get("url")
        self._add(
            "deadlines",
            ext,
            {
                "title": clean_text(collapse(str(ev["name"]))),
                "dueAt": due,
                "courseId": str(course["id"]) if course.get("id") else None,
                "assignmentId": str(instance) if module == "assign" and instance else None,
                "state": "open",
                "url": link if isinstance(link, str) else None,
            },
            link if isinstance(link, str) else origin,
        )
        return True

    async def _action_events(self, call: Any, ctx: SyncContext, horizon: datetime) -> int:
        n = 0
        after: int | None = None
        for _ in range(EVENT_PAGES):
            args: dict[str, Any] = {
                "timesortfrom": int(ctx.now.timestamp()),
                "timesortto": int(horizon.timestamp()),
                "limitnum": EVENT_PAGE,
            }
            if after is not None:
                args["aftereventid"] = after
            doc = await call("core_calendar_get_action_events_by_timesort", **args)
            events = doc.get("events") if isinstance(doc, dict) else None
            if not isinstance(events, list):
                raise _FunctionFailed("format")
            for ev in events:
                if isinstance(ev, dict) and self._deadline_from_event(ev, self.spec.origin):
                    n += 1
            last = doc.get("lastid") if isinstance(doc, dict) else None
            if len(events) < EVENT_PAGE or not isinstance(last, int):
                break
            after = last
        return n

    def _calendar_events(self, events: list[dict[str, Any]], ctx: SyncContext) -> int:
        n = 0
        for ev in events:
            duration = ev.get("timeduration")
            if (
                isinstance(duration, (int, float))
                and duration > 0
                and isinstance(ev.get("timestart"), int)
            ):
                start = datetime.fromtimestamp(float(ev["timestart"]), tz=UTC)
                if ev.get("eventtype") in _DEADLINE_EVENTS:
                    n += int(self._deadline_from_event(ev, self.spec.origin))
                    continue
                link = ev.get("url")
                self._add(
                    "scheduleEvents",
                    f"event-{ev.get('id')}",
                    {
                        "title": clean_text(collapse(str(ev.get("name") or ""))),
                        "startsAt": to_utc_iso(start),
                        "endsAt": to_utc_iso(start + timedelta(seconds=float(duration))),
                        "location": clean_text(collapse(str(ev["location"])))
                        if ev.get("location")
                        else None,
                        "url": link if isinstance(link, str) else None,
                    },
                    link if isinstance(link, str) else self.spec.origin,
                )
                n += 1
            elif self._deadline_from_event(ev, self.spec.origin):
                n += 1
        return n

    # --- assignments -------------------------------------------------------------------------

    async def _assignments(
        self, call: Any, ctx: SyncContext, courses: list[dict[str, Any]], outcome: EndpointOutcome
    ) -> int:
        ids = [c["id"] for c in courses]
        if not ids:
            return 0
        doc = await call("mod_assign_get_assignments", courseids=ids)
        listed = doc.get("courses") if isinstance(doc, dict) else None
        if not isinstance(listed, list):
            raise _FunctionFailed("format")
        outcome.kind = "assignments"
        n = 0
        for course in listed:
            if not isinstance(course, dict):
                continue
            for a in course.get("assignments") or []:
                if not isinstance(a, dict) or not isinstance(a.get("id"), int) or not a.get("name"):
                    continue
                cmid = a.get("cmid")
                url = f"{self.spec.origin}/mod/assign/view.php?id={cmid}" if cmid else None
                grade = a.get("grade")
                self._add(
                    "assignments",
                    str(a["id"]),
                    {
                        "title": clean_text(collapse(str(a["name"]))),
                        "courseId": str(course.get("id")) if course.get("id") else None,
                        "description": clean_text(html_to_text(str(a["intro"])), 4000)
                        if a.get("intro")
                        else None,
                        "url": url,
                        "maxScore": grade
                        if isinstance(grade, (int, float)) and grade > 0
                        else None,
                    },
                    url or self.spec.origin,
                )
                due = _ts(a.get("duedate"))
                if due:
                    self._assign_due[a["id"]] = due
                    self._add(
                        "deadlines",
                        f"assign-{a['id']}",
                        {
                            "title": clean_text(collapse(str(a["name"]))),
                            "dueAt": due,
                            "courseId": str(course.get("id")) if course.get("id") else None,
                            "assignmentId": str(a["id"]),
                            "state": "open",
                            "url": url,
                        },
                        url or self.spec.origin,
                    )
                n += 1
        return n

    async def _submission_status(self, call: Any, ctx: SyncContext, userid: int | None) -> int:
        n = 0
        due_map = self._assign_due
        for assign_id, due in list(due_map.items())[:MAX_SUBMISSION_CALLS]:
            try:
                doc = await call(
                    "mod_assign_get_submission_status", assignid=assign_id, userid=userid
                )
            except _FunctionFailed:
                self._partial = True
                continue
            last = doc.get("lastattempt") if isinstance(doc, dict) else None
            sub = (last or {}).get("submission") if isinstance(last, dict) else None
            status = str((sub or {}).get("status", "")) if isinstance(sub, dict) else ""
            graded = bool(isinstance(last, dict) and str(last.get("gradingstatus", "")) == "graded")
            extension = _ts(last.get("extensionduedate")) if isinstance(last, dict) else None
            if graded:
                state = "graded"
            elif status == "submitted":
                state = "submitted"
            elif datetime.fromisoformat(due) < ctx.now and not extension:
                state = "missed"
            else:
                state = "open"
            existing = self._records.get(("deadlines", f"assign-{assign_id}"))
            if existing is None:
                continue
            fields = dict(existing.fields)
            fields["state"] = state
            if extension:
                fields["effectiveDueAt"] = extension
            self._records[("deadlines", f"assign-{assign_id}")] = Record(
                "deadlines", existing.external_id, fields, existing.source_url
            )
            n += 1
        return n

    # --- grades ------------------------------------------------------------------------------

    async def _grades(
        self, call: Any, ctx: SyncContext, userid: int | None, courses: list[dict[str, Any]]
    ) -> int:
        n = 0
        for course in courses[:MAX_GRADE_CALLS]:
            try:
                doc = await call(
                    "gradereport_user_get_grade_items", courseid=course["id"], userid=userid
                )
            except _FunctionFailed:
                self._partial = True
                continue
            usergrades = doc.get("usergrades") if isinstance(doc, dict) else None
            if not isinstance(usergrades, list):
                raise _FunctionFailed("format")
            for ug in usergrades:
                for item in (ug.get("gradeitems") or []) if isinstance(ug, dict) else []:
                    if not isinstance(item, dict):
                        continue
                    value = item.get("gradeformatted")
                    if value in (None, "", "-"):
                        value = item.get("graderaw")
                    if value in (None, "", "-") or not isinstance(value, (str, int, float)):
                        continue
                    name = item.get("itemname")
                    title = (
                        clean_text(collapse(str(name)))
                        if name
                        else ("Итого по курсу" if item.get("itemtype") == "course" else None)
                    )
                    grademax = item.get("grademax")
                    weight = item.get("weightraw")
                    self._add(
                        "grades",
                        f"{course['id']}-{item.get('id')}",
                        {
                            "title": title,
                            "courseId": str(course["id"]),
                            "value": value
                            if isinstance(value, (int, float))
                            else clean_text(str(value), 60),
                            "scale": f"0–{grademax:g}"
                            if isinstance(grademax, (int, float))
                            else None,
                            "weight": weight if isinstance(weight, (int, float)) else None,
                            "gradedAt": _ts(item.get("gradedategraded")),
                        },
                        f"{self.spec.origin}/grade/report/user/index.php?id={course['id']}",
                    )
                    n += 1
        return n
