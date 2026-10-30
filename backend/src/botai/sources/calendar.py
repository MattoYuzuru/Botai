"""Connecting a calendar by the export link of its owner (FR-018, Q11).

The student's timetable often lives in a corporate calendar (Yandex, Google, Outlook). Such a
calendar can be exported as a personal iCal link, and the `ical` adapter reads it with no browser
and no login. The link is a secret, so the way to hand it over is a task, not a chat message:

1. `start_calendar_source` makes the source (kind `calendar`, adapter `ical`, one per university)
   and queues a `CONNECT` task with `payload.calendar` in the thread that asked;
2. `CalendarConnector` shows how to get the link, asks for it in a protected field (the answer goes
   API -> Vault -> worker, past the model and the history), checks it (HTTPS through the
   EgressProxy with a grant for the link's host alone, no cookies, size and time caps, parsed as a
   calendar), asks again after a wrong one (three tries), keeps the link in the Vault as
   `ical_url`, reads the calendar once and tells the student what is in it.

The link is never an argument, a log line, a diagnostic field, an event, a history message or an
API answer; the only part of it that is stored in the open is its host (`origin`).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import InputBlock, SummaryGroup, SummaryItem
from botai.contracts.specs import IcalSpec
from botai.core.calendar_link import normalize_calendar_link, origin_of_link
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import save_secret
from botai.core.http import RetryPolicy
from botai.core.ids import new_id
from botai.core.jobs import JobClaimLost, JobContext, JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import Connection, Job, Space
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import enqueue_in_session
from botai.learn import revisions
from botai.learn.ui import Ui, describe, item
from botai.sources.adapter import Record
from botai.sources.adapters.ical import IcalAdapter, read_link
from botai.sources.runner import (
    ConnInfo,
    build_context,
    commit_result,
    context_for,
    guard_source_write,
    load_conn_info,
    load_snapshot,
    set_connection,
)
from botai.spaces import repo, service

CALENDAR_TITLE = "Календарь"
KIND = "calendar"
ADAPTER = "ical"
ATTEMPTS = 3
UPCOMING = 5

GUIDE = (
    "Чтобы читать календарь, мне нужна его личная ссылка на экспорт (iCal). Где её взять:\n\n"
    "- **Яндекс Календарь**: настройки календаря → «Экспорт» → скопируйте ссылку на файл iCal.\n"
    "- **Google Календарь** (на компьютере): настройки нужного календаря → «Интеграция "
    "календаря» → «Секретный адрес в формате iCal».\n"
    "- **Outlook**: настройки → «Календарь» → «Общие календари» → «Опубликовать календарь» → "
    "права «Можно просматривать все сведения» → ссылка ICS.\n\n"
    "Эта ссылка открывает весь календарь, поэтому никому её не показывайте и не вставляйте в "
    "чат: введите её только в защищённое поле ниже. Я сохраню её зашифрованной и больше не "
    "покажу."
)
QUESTION = "Ссылка на экспорт календаря (iCal)"


# --- the source and its task ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarStart:
    """What `start_calendar_source` did: `started` (a new task), `running` (this source already has
    one, `job` is it), or `exists` (an active calendar of this university: nothing was started)."""

    outcome: Literal["started", "running", "exists"]
    connection: Connection
    job: Job | None
    space: Space


async def start_calendar_source(
    session: AsyncSession,
    *,
    user_id: str,
    thread_id: str | None,
    space: Space,
    title: str,
    replace_link: bool,
    now: datetime,
) -> CalendarStart:
    """The calendar source of a university (one per university) and its link task in a thread.

    A calendar that works is left alone unless `replace_link`; one that never got a link, or whose
    link stopped working, is asked for the link again. The caller owns the transaction.
    """
    # The lock every creator of a source of a university takes first (`service.ensure_space`).
    await service.lock_university_space(session, user_id, space.university_id)
    conn = await session.scalar(
        select(Connection)
        .where(
            Connection.user_id == user_id,
            Connection.space_id == space.id,
            Connection.adapter == ADAPTER,
        )
        .order_by(Connection.created_at, Connection.id)
        .limit(1)
    )
    if conn is None:
        conn = Connection(
            user_id=user_id,
            space_id=space.id,
            origin="",  # the host of the link, once the link is known
            kind=KIND,
            adapter=ADAPTER,
            title=title,
            status="draft",
            allow_hosts=[],
        )
        session.add(conn)
        await session.flush()
    else:
        running = await session.scalar(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.connection_id == conn.id,
                Job.kind == JobKind.CONNECT.value,
                Job.state.in_(sorted(IN_FLIGHT)),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        if running is not None:
            return CalendarStart("running", conn, running, space)
        working = conn.spec_id is not None and conn.status not in {"needs_reauth", "error", "draft"}
        if working and not replace_link:
            return CalendarStart("exists", conn, None, space)
    job = await enqueue_in_session(
        session,
        JobKind.CONNECT,
        user_id=user_id,
        connection_id=conn.id,
        thread_id=thread_id,
        payload={"calendar": True, "source": "chat"},
        now=now,
        dedupe=True,
    )
    return CalendarStart("started", conn, job, space)


async def start_from_chat(
    session: AsyncSession, user_id: str, thread_id: str, space_id: str | None, now: datetime
) -> CalendarStart | None:
    """A personal calendar link was written into a thread: open the protected field right there.

    The university is the thread's own, else the student's only one; with several and no hint
    nothing is started (the agent asks which one). Nothing of the message is kept.
    """
    space = await repo.get_space(session, user_id, space_id) if space_id else None
    if space is None:
        owned = [s for s in await repo.list_spaces(session, user_id) if s.university_id]
        space = owned[0] if len(owned) == 1 else None
    if space is None:
        return None
    return await start_calendar_source(
        session,
        user_id=user_id,
        thread_id=thread_id,
        space=space,
        title=CALENDAR_TITLE,
        replace_link=True,
        now=now,
    )


# --- the task -------------------------------------------------------------------------------------


def _problem_text(error: BotaiError) -> str:
    if error.code == ErrorCode.SOURCE_UNAVAILABLE:
        return "Сайт календаря не отвечает. Проверьте ссылку или повторите чуть позже."
    if error.code == ErrorCode.SOURCE_RATE_LIMITED:
        return "Сервис календаря просит подождать. Повторите чуть позже."
    if error.code == ErrorCode.EGRESS_BLOCKED:
        return "Ссылка ведёт на другой адрес, чем календарь: такую ссылку подключить нельзя."
    return error.detail or "Эту ссылку не удалось открыть."


_READING_TEXT = {
    "gone": (
        "Сервис календаря не принял эту ссылку: возможно, она скопирована не целиком или "
        "уже отозвана. Скопируйте ссылку экспорта заново."
    ),
    "status": (
        "Сервис календаря сейчас отвечает ошибкой. Повторите ту же ссылку чуть позже "
        "или скопируйте её заново."
    ),
    "format": (
        "По ссылке открылся не календарь, а обычная страница или другой файл. Нужна именно "
        "ссылка на экспорт календаря (файл iCal, .ics), а не адрес календаря в браузере."
    ),
}

_MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


def _date(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{local.day} {_MONTHS[local.month - 1]}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def _when(record: Record) -> datetime | None:
    value = record.fields.get("startsAt") or record.fields.get("dueAt")
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _dated(records: list[Record], kind: str) -> list[tuple[datetime, Record]]:
    found = [(at, r) for r in records if r.kind == kind and (at := _when(r)) is not None]
    return sorted(found, key=lambda pair: pair[0])


def _row(record: Record, tz: ZoneInfo) -> SummaryItem:
    label, value = describe(record, tz)
    return item(label, value.replace(", 00:00–00:00", ", весь день"))  # a holiday, a day off


def read_summary(
    records: list[Record], tz: ZoneInfo, now: datetime
) -> tuple[str, list[SummaryGroup]]:
    """What the calendar holds, for the student: «124 занятия с 26 октября по 29 января», the
    nearest classes and deadlines. Numbers and dates are ours; titles go to structured items,
    never into Markdown."""
    classes = _dated(records, "scheduleEvents")
    deadlines = _dated(records, "deadlines")
    if not classes and not deadlines:
        return (
            "Календарь открылся, но занятий и сроков в нём на ближайшие три месяца нет. "
            "Они появятся здесь сами, как только их добавят.",
            [],
        )
    parts: list[str] = []
    if classes:
        count = len(classes)
        parts.append(
            f"{count} {_plural(count, 'занятие', 'занятия', 'занятий')} "
            f"с {_date(classes[0][0], tz)} по {_date(classes[-1][0], tz)}"
        )
    if deadlines:
        count = len(deadlines)
        parts.append(f"{count} {_plural(count, 'срок сдачи', 'срока сдачи', 'сроков сдачи')}")
    groups: list[SummaryGroup] = []
    for title, rows in (("Ближайшие занятия", classes), ("Ближайшие сроки", deadlines)):
        ahead = [r for at, r in rows if at >= now][:UPCOMING]
        if ahead:
            groups.append(SummaryGroup(title=title, items=[_row(r, tz) for r in ahead]))
    return "Календарь прочитан: " + ", ".join(parts) + ".", groups


class LinkProblem(Exception):
    """The link cannot be used; `text` says why, in words for the student (never the link)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class CalendarConnector:
    """The `CONNECT` task of a calendar source (`payload.calendar`)."""

    def __init__(
        self,
        ctx: JobContext,
        services: Services,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.ctx, self.services = ctx, services
        self.transport, self.retry = transport, retry
        self.ui = Ui(ctx, services.clock)

    async def run(self, connection_id: str) -> dict[str, Any]:
        svc, user_id = self.services, self.ctx.user_id
        async with svc.db() as db:
            info = await load_conn_info(db, user_id, connection_id)
        if info.status == "disabled":
            return {"status": "skipped", "detail": "disabled"}
        configured = (
            info.origin != ""
        )  # a calendar that had a link: it keeps working until a new one does
        if not configured:
            await set_connection(
                svc, connection_id, user_id, ctx=self.ctx, status="learning", clear_error=True
            )
        await self.ui.say(GUIDE)
        for attempt in range(1, ATTEMPTS + 1):
            await self.ctx.status("Жду ссылку на календарь")
            try:
                link, calendar = await self._ask(info)
            except LinkProblem as problem:
                diagnostic_event("learn.calendar", outcome="rejected", attempt=attempt)
                await self.ui.say(
                    problem.text
                    + (
                        " Календарь пока не подключён. Когда будете готовы, попросите меня "
                        "подключить календарь ещё раз — я снова открою защищённое поле."
                        if attempt == ATTEMPTS
                        else " Попробуйте ещё раз."
                    )
                )
                continue
            return await self._connect(info, link, calendar)
        if not configured:
            await set_connection(
                svc,
                connection_id,
                user_id,
                ctx=self.ctx,
                status="error",
                error=(ErrorCode.VALIDATION.value, "Ссылка на календарь не прошла проверку"),
            )
        return {"status": "error", "detail": ErrorCode.VALIDATION.value}

    async def _ask(self, info: ConnInfo) -> tuple[str, Calendar]:
        """One round: the protected field, then the check. The link leaves this function only as
        the return value, to be stored; a wrong one raises `LinkProblem`."""
        answer = await self.ctx.ask_secret(
            InputBlock(
                id=new_id(),
                at=self.services.clock.now(),
                question=QUESTION,
                input_kind="url",
                placeholder="https://… или webcal://…",
                required=True,
                max_length=2000,
                secret=True,
                autocomplete="url",
            )
        )
        stage = await self.ui.stage("Проверяю ссылку на календарь")
        try:
            link = normalize_calendar_link(answer.get_secret_value())
            host = urlsplit(link).hostname or ""
            probe = dataclasses.replace(info, origin=origin_of_link(link), allow_hosts=(host,))
            sctx = context_for(
                self.services, probe, None, transport=self.transport, retry=self.retry
            )
            reading = await read_link(sctx, link)
        except BotaiError as error:
            await stage.finish("failed")
            raise LinkProblem(_problem_text(error)) from None
        if reading.calendar is None:
            await stage.finish("failed")
            raise LinkProblem(_READING_TEXT[reading.problem or "format"])
        await stage.finish("ok")
        diagnostic_event("learn.calendar", outcome="ok")
        return link, reading.calendar

    async def _connect(self, info: ConnInfo, link: str, calendar: Calendar) -> dict[str, Any]:
        svc, ctx = self.services, self.ctx
        connection_id, user_id = info.connection_id, info.user_id
        origin = origin_of_link(link)
        host = urlsplit(link).hostname or ""
        await save_secret(ctx, svc.vault, user_id, connection_id, SecretKind.ICAL_URL, link)
        spec = {"adapter": ADAPTER, "origin": origin}
        IcalSpec.model_validate(spec)
        now = svc.clock.now()
        async with svc.db() as db, db.begin():
            conn = await guard_source_write(
                db, ctx, user_id, connection_id, origin=origin, adapter=ADAPTER
            )
            if conn is None or conn.status == "disabled":
                raise JobClaimLost("the source is gone")
            row = await revisions.active_row(db, origin, ADAPTER)
            if row is None:
                row = await revisions.add_revision(
                    db, origin=origin, adapter=ADAPTER, spec=spec, state="canary"
                )
                await revisions.activate(db, row, now=now)
            conn.origin, conn.allow_hosts = origin, [host]
            conn.kind, conn.adapter = KIND, ADAPTER
            conn.spec_id, conn.spec_revision = row.id, row.revision
        async with svc.db() as db:
            snap = await load_snapshot(db, user_id, connection_id)
        sctx = build_context(svc, snap, None, transport=self.transport, retry=self.retry)
        result = IcalAdapter(IcalSpec.model_validate(snap.spec)).result_for(calendar, sctx)
        outcome = await commit_result(
            svc, snap, result, sctx.now, accept_empty=True, queue_repair=False, ctx=ctx
        )
        if outcome.status != "ok":
            return {"status": "error", "detail": outcome.status}
        text, groups = read_summary(result.records, snap.tz, svc.clock.now())
        await self.ui.say(text)
        if groups:
            await self.ui.summary("Что дальше в расписании", groups)
        diagnostic_event("learn.calendar", outcome="done", record_count=len(result.records))
        return {
            "status": "ok",
            "events": sum(1 for r in result.records if r.kind == "scheduleEvents"),
            "deadlines": sum(1 for r in result.records if r.kind == "deadlines"),
        }
