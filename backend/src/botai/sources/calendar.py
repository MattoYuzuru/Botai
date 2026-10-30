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

A link typed into the chat is dropped before it is stored (`refuse_calendar_link`); the thread gets
a plain question in its place, with buttons, and only the student's «Да» (to the agent, which calls
`connect_calendar`) opens the field. The link is never an argument, a log line, a diagnostic field,
an event, a history message or an API answer; the only part of it that is stored in the open is its
host (`origin`).
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.agent_ui import InputBlock, TextBlock
from botai.contracts.specs import IcalSpec, Kind
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import save_secret
from botai.core.http import RetryPolicy
from botai.core.ids import new_id
from botai.core.jobs import JobClaimLost, JobContext, JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import ChatMessage, ChatThread, Connection, Job, Space, University
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import enqueue_in_session
from botai.learn import revisions
from botai.learn.summary import confirm_details, finished_text, weeks_ahead
from botai.learn.ui import Ui
from botai.sources.adapter import Record
from botai.sources.adapters.ical import IcalAdapter, read_link
from botai.sources.calendar_url import normalize_calendar_link, origin_of_link
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
from botai.spaces import service

CALENDAR_TITLE = "Календарь"
KIND = "calendar"
ADAPTER = "ical"
ATTEMPTS = 3
log = logging.getLogger(__name__)

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
    one, `job` is it), or `exists` (a calendar of this university that works, or was switched off by
    the student: nothing was started)."""

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
    elif conn.status == "disabled":
        # Switched off by the student: theirs to switch on, not the agent's to restart.
        return CalendarStart("exists", conn, None, space)
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


# --- a link typed into the chat -------------------------------------------------------------------

OFFER = "Похоже, это личная ссылка календаря"
REFUSAL = (
    "Это личная ссылка календаря, в чат её вставлять нельзя: она открывает весь календарь. "
    "Подключить календарь можно через защищённое поле: я предложил это ниже, в чате"
)
YES = "Да, подключить календарь"
YES_REPLACE = "Да, заменить ссылку календаря"
NO = "Нет, это другое"
NO_REPLACE = "Нет, оставить как есть"
QUICK_MAX = 60
BUTTONS = 4


@dataclass(frozen=True, slots=True)
class Offer:
    """What the thread says to the student in place of the link: a plain question, with buttons."""

    text: str
    quick_replies: tuple[str, ...] = ()


def _button(text: str) -> str:
    return text if len(text) <= QUICK_MAX else text[: QUICK_MAX - 1].rstrip() + "…"


async def _offer_for(
    session: AsyncSession, user_id: str, thread_id: str, thread_space_id: str | None
) -> Offer:
    """The question for this thread: the thread's university, else the student's only one; with
    several and no hint, which one. Nothing is created: the answer decides."""
    owned = [entry for entry in await service.list_sources(session, user_id) if entry.university]
    chosen = next((e for e in owned if e.space.id == thread_space_id), None)
    if chosen is None and len(owned) == 1:
        chosen = owned[0]
    if chosen is None:
        if not owned:
            return Offer(
                f"{OFFER}, а в чат её присылать нельзя. Для какого вуза подключить календарь? Напишите название вуза."
            )
        text = f"{OFFER}, а в чат её присылать нельзя. Для какого вуза подключить календарь?"
        if len(owned) > BUTTONS:
            text += " Если нужного вуза нет в кнопках, напишите его название."
        return Offer(
            text, tuple(_button(f"Календарь для {e.short_title}") for e in owned[:BUTTONS])
        )
    space = chosen.space
    conn = next((v.connection for v in chosen.sources if v.connection.adapter == ADAPTER), None)
    where = f"«{space.title}»"
    if conn is not None:
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
        if running is not None and running.thread_id == thread_id:
            return Offer(
                f"{OFFER}: в чат её присылать нельзя. Защищённое поле для неё уже открыто выше, "
                "введите ссылку там."
            )
        if running is not None and running.thread_id:
            return Offer(
                f"{OFFER}: в чат её присылать нельзя. Календарь вуза {where} уже ждёт ссылку в "
                f"[другом чате](botai://threads/{running.thread_id}): введите её в защищённое поле там."
            )
        if conn.status == "disabled":
            return Offer(
                f"{OFFER}: в чат её присылать нельзя. Календарь вуза {where} отключён: включите "
                "его в списке источников, и я открою защищённое поле для ссылки."
            )
        if conn.spec_id is not None and conn.status not in {"needs_reauth", "error", "draft"}:
            return Offer(
                f"{OFFER}: в чат её присылать нельзя. Календарь вуза {where} уже подключён. "
                "Заменить ссылку на новую? Я открою для неё защищённое поле.",
                (YES_REPLACE, NO_REPLACE),
            )
    return Offer(
        f"{OFFER}, а в чат её присылать нельзя: она открывает весь календарь. Подключить "
        f"календарь вуза {where}? Ссылку я попрошу ввести в защищённом поле.",
        (YES, NO),
    )


async def offer_calendar(
    session: AsyncSession, user_id: str, thread: ChatThread, now: datetime
) -> None:
    """Writes the question into the thread, once: a second ask while the first is the last word of
    the thread adds nothing. Commits. Creates no source and no task."""
    thread_id, space_id = thread.id, thread.space_id
    offer = await _offer_for(session, user_id, thread_id, space_id)
    last = await session.scalar(
        select(ChatMessage)
        .where(ChatMessage.user_id == user_id, ChatMessage.thread_id == thread_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    already = (
        last is not None
        and last.role == "agent"
        and bool(last.blocks)
        and str(last.blocks[0].get("markdown", "")).startswith(OFFER)
    )
    if already:
        await session.rollback()
        return
    at = await dialogue.message_time(session, user_id, thread_id, now)
    block = TextBlock(
        id=new_id(), at=at, markdown=offer.text, quick_replies=list(offer.quick_replies) or None
    )
    session.add(
        ChatMessage(
            id=new_id(),
            user_id=user_id,
            thread_id=thread_id,
            role="agent",
            blocks=[block.model_dump(by_alias=True, mode="json", exclude_none=True)],
            created_at=at,
        )
    )
    await session.commit()


async def refuse_calendar_link(
    session: AsyncSession, user_id: str, thread: ChatThread, now: datetime
) -> BotaiError:
    """A personal calendar link must not stay in a chat. Nothing of the message is kept, nothing is
    created: the thread gets the question, and the answer is the student's to give."""
    thread_id = thread.id
    await offer_calendar(session, user_id, thread, now)
    diagnostic_event("chat.calendar_link", thread_id=thread_id, outcome="refused")
    return BotaiError(ErrorCode.SECRET_LINK_CALENDAR, REFUSAL)


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
        # A calendar that had a link keeps working (and keeps its status) until a new one does.
        configured = info.origin != ""
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
        except (ValueError, httpx.HTTPError):  # an address the client or the proxy cannot use
            await stage.finish("failed")
            raise LinkProblem(
                "Эту ссылку не удалось открыть: проверьте, что она скопирована целиком."
            ) from None
        if reading.calendar is None:
            await stage.finish("failed")
            raise LinkProblem(_READING_TEXT[reading.problem or "format"])
        await stage.finish("ok")
        diagnostic_event("learn.calendar", outcome="ok")
        return link, reading.calendar

    async def _fence(self, connection_id: str) -> None:
        """The task is still the source's current one and the source is still there: checked before
        anything is written (the Vault write takes the same row lock, so this is its own transaction)."""
        async with self.services.db() as db, db.begin():
            conn = await guard_source_write(db, self.ctx, self.ctx.user_id, connection_id)
            if conn is None or conn.status == "disabled":
                raise JobClaimLost("the source is gone")

    async def _put_back(self, connection_id: str, previous: SecretStr | None) -> None:
        """The calendar that worked keeps its link when the new one could not be registered."""
        vault, user_id = self.services.vault, self.ctx.user_id
        try:
            if previous is None:
                await vault.delete(user_id, connection_id, SecretKind.ICAL_URL)
            else:
                await vault.put(
                    user_id, connection_id, SecretKind.ICAL_URL, previous.get_secret_value()
                )
        except Exception:  # never the link in the message: the type and the frames are enough
            log.error("the previous calendar link could not be restored", exc_info=False)

    async def _register(self, connection_id: str, origin: str, host: str) -> None:
        """The spec of the link's host (shared, without the link) and the fields of the source, in
        one transaction: all or nothing."""
        svc, ctx, user_id = self.services, self.ctx, self.ctx.user_id
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

    async def _connect(self, info: ConnInfo, link: str, calendar: Calendar) -> dict[str, Any]:
        svc, ctx = self.services, self.ctx
        connection_id, user_id = info.connection_id, info.user_id
        origin = origin_of_link(link)
        host = urlsplit(link).hostname or ""
        previous = await svc.vault.get(user_id, connection_id, SecretKind.ICAL_URL)
        await self._fence(connection_id)
        await save_secret(ctx, svc.vault, user_id, connection_id, SecretKind.ICAL_URL, link)
        try:
            await self._register(connection_id, origin, host)
        except BaseException:
            await self._put_back(connection_id, previous)
            raise
        async with svc.db() as db:
            snap = await load_snapshot(db, user_id, connection_id)
        sctx = build_context(svc, snap, None, transport=self.transport, retry=self.retry)
        result = IcalAdapter(IcalSpec.model_validate(snap.spec)).result_for(calendar, sctx)
        outcome = await commit_result(
            svc, snap, result, sctx.now, accept_empty=True, queue_repair=False, ctx=ctx
        )
        if outcome.status != "ok":
            return {"status": "error", "detail": outcome.status}
        await self._tell(snap.space_id, snap.tz, snap.freshness.every_minutes, result.records)
        diagnostic_event("learn.calendar", outcome="done", record_count=len(result.records))
        return {
            "status": "ok",
            "events": sum(1 for r in result.records if r.kind == "scheduleEvents"),
            "deadlines": sum(1 for r in result.records if r.kind == "deadlines"),
        }

    async def _tell(
        self, space_id: str, tz: ZoneInfo, every_minutes: int, records: list[Record]
    ) -> None:
        """What the calendar holds, in the words of every connection (`learn/summary.py`): the
        counts and the nearest days of classes, then how far ahead it reaches and how it is kept."""
        now = self.services.clock.now()
        found: dict[Kind, list[Record]] = {
            kind: rows
            for kind in ("scheduleEvents", "deadlines")
            if (rows := [r for r in records if r.kind == kind])
        }
        ahead = [r for r in found.get("scheduleEvents", []) if (_end(r) or now) >= now]
        starts = [at for r in ahead if (at := _moment(r, "startsAt")) is not None]
        counts: dict[Kind, int] = {kind: len(rows) for kind, rows in found.items()}
        if ahead:
            counts["scheduleEvents"] = len(ahead)
        async with self.services.db() as db:
            space = await db.get(Space, space_id)
            university = (
                await db.get(University, space.university_id)
                if space and space.university_id
                else None
            )
        name = university.short_title if university else space.title if space else None
        if found:
            await self.ui.say(
                confirm_details(found, now, tz, heading="Нашла в календаре:"),
            )
        await self.ui.say(
            finished_text(
                counts,
                university=name,
                every_minutes=every_minutes,
                weeks=weeks_ahead(max(starts) if starts else None, now, tz),
            )
        )


def _moment(record: Record, name: str) -> datetime | None:
    value = record.fields.get(name)
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _end(record: Record) -> datetime | None:
    return _moment(record, "endsAt") or _moment(record, "startsAt")
