"""User sources (priority 2): a link or an uploaded file becomes a `document` connection.

The type of the file is detected from its bytes. Its text is split into chunks for full-text
search. When it holds a table (an exam list, a deadline sheet), the agent proposes which columns
mean what, asks the student about every column that is not clear and about how to find their rows
(never guessing a student), shows what would be read, and only then saves a `document` spec. The
model sees a redacted sample; free-form documents are not sent to a model at all.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import update

from botai.contracts.agent_ui import SummaryGroup
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import Handler
from botai.core.http import RetryPolicy
from botai.core.jobs import JobCancelled, JobContext
from botai.core.services import Services
from botai.db import models as m
from botai.learn import revisions, tables
from botai.learn.lint import LintContext, lint_spec
from botai.learn.prompting import FIELD_HELP
from botai.learn.redact import Redactor
from botai.learn.ui import Ui, describe, item, sample_of
from botai.sources import schedule
from botai.sources.adapter import EndpointOutcome, NeedsUserInput, Record, SyncContext
from botai.sources.adapters.document import DocumentAdapter, LoadedDocument
from botai.sources.factory import AdapterFactory
from botai.sources.runner import (
    ConnInfo,
    commit_result,
    context_for,
    load_conn_info,
    load_snapshot,
    set_connection,
)
from botai.sources.vocab import KIND_LABELS, TABLE_KINDS

log = logging.getLogger(__name__)

UPLOAD_ORIGIN = "https://upload.botai.local"  # uploads have no host; the allowlist stays empty
GOOGLE_HOST = "docs.google.com"
MAX_UPLOAD = 8 * 1024 * 1024
REFRESH_URL_MINUTES = 720
PROFILE_KEYS = {
    "fullName": "fullName",
    "email": "email",
    "group": "group",
    "studentId": "studentId",
}
PARAM_QUESTIONS = {
    "fullName": "Как вас записали в таблице (например, «Фамилия Имя Отчество»)?",
    "email": "Какая почта указана для вас в таблице?",
    "group": "Какая группа указана для вас в таблице?",
    "studentId": "Какой номер (студенческого или зачётки) указан для вас в таблице?",
}


def source_for_url(url: str) -> tuple[dict[str, Any], list[str]]:
    """The `source` of a document spec and the hosts it may use, from a link."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host == GOOGLE_HOST and "/spreadsheets/d/" in parts.path:
        kind = "google_sheet"
    elif host == GOOGLE_HOST and "/document/d/" in parts.path:
        kind = "google_doc"
    else:
        kind = "url"
    source: dict[str, Any] = {
        "kind": kind,
        "url": url.strip(),
        "refreshEveryMinutes": REFRESH_URL_MINUTES,
    }
    return source, [host]


def check_url(url: str) -> str:
    """A link a student may add: plain https, a real host, no credentials."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or not host
        or "." not in host
        or host.endswith((".local", ".localhost", ".internal"))
        or _is_ip(host)
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Нужна ссылка вида https://example.org/файл")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


@dataclass(slots=True)
class TableChoice:
    spec: dict[str, Any]
    profile: dict[str, str]


class Ingestor:
    def __init__(
        self,
        ctx: JobContext,
        services: Services,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.ctx, self.services = ctx, services
        self.ui = Ui(ctx, services.clock)
        self.transport, self.retry = transport, retry

    async def _set(self, connection_id: str, **values: Any) -> None:
        await set_connection(self.services, connection_id, self.ctx.user_id, **values)

    async def run(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.services.db() as db:
            info = await load_conn_info(db, self.ctx.user_id, connection_id)
        if info.status == "disabled":
            return {"status": "skipped", "detail": "disabled"}
        await self._set(connection_id, status="learning", clear_error=True)
        try:
            return await self._ingest(info, payload)
        except JobCancelled:
            await self._set(connection_id, status="draft")
            raise
        except BotaiError as err:
            await self.ui.say(f"Не получилось добавить документ: {err.detail or 'ошибка'}.")
            await self._set(connection_id, status="error", error=(err.code.value, err.detail))
            return {"status": "error", "detail": err.code.value}
        finally:
            await self._strip_payload(payload)

    async def _strip_payload(self, payload: dict[str, Any]) -> None:
        """The uploaded bytes ride in the job payload; once read they are not kept there."""
        if "content_b64" not in payload:
            return
        slim = {k: v for k, v in payload.items() if k != "content_b64"}
        async with self.services.db() as db, db.begin():
            await db.execute(update(m.Job).where(m.Job.id == self.ctx.job_id).values(payload=slim))

    # --- the job -----------------------------------------------------------------------------

    async def _ingest(self, info: ConnInfo, payload: dict[str, Any]) -> dict[str, Any]:
        ui, svc = self.ui, self.services
        source = payload.get("source")
        if not isinstance(source, dict) or source.get("kind") not in {
            "upload",
            "url",
            "google_doc",
            "google_sheet",
        }:
            raise BotaiError(ErrorCode.VALIDATION, "Не указан источник документа")
        base_spec: dict[str, Any] = {"adapter": "document", "source": source}
        sctx = context_for(svc, info, None, transport=self.transport, retry=self.retry)
        sctx.filename = str(payload.get("filename") or "") or None
        if source["kind"] == "upload":
            try:
                sctx.content = base64.b64decode(str(payload.get("content_b64", "")), validate=True)
            except (binascii.Error, ValueError):
                raise BotaiError(ErrorCode.VALIDATION, "Файл повреждён при передаче") from None
            if not sctx.content or len(sctx.content) > MAX_UPLOAD:
                raise BotaiError(ErrorCode.VALIDATION, "Файл пустой или больше 8 МБ")
        adapter = AdapterFactory().create(base_spec)
        assert isinstance(adapter, DocumentAdapter)

        stage = await ui.stage("Читаю документ")
        raw = await adapter.fetch_bytes(sctx)
        if isinstance(raw, EndpointOutcome):
            await stage.finish("failed", "Файл по ссылке не скачался")
            code = (
                ErrorCode.NOT_FOUND if raw.problem == "not_found" else ErrorCode.SOURCE_UNAVAILABLE
            )
            raise BotaiError(code, "Файл по ссылке недоступен")
        data, content_type = raw
        loaded = adapter.process(data, content_type, sctx)
        doc = loaded.document
        await stage.finish("ok", f"Тип: {loaded.format}; фрагментов для поиска: {len(doc.chunks)}")

        table: TableChoice | None = None
        if loaded.extracted.tables:
            table = await self._table(info, loaded, payload.get("note"), sctx)

        spec: dict[str, Any] = {**base_spec, "format": loaded.format}
        if table is not None:
            spec["table"] = table.spec
        verdict = lint_spec(
            spec,
            LintContext(
                allowed_hosts=[
                    *info.allow_hosts,
                    *([GOOGLE_HOST] if "google" in source["kind"] else []),
                ],
                observed=tables.all_data_cells(loaded.extracted),
            ),
        )
        if not verdict.ok:
            await ui.note(
                "Проверка отклонила разбор таблицы", "; ".join(verdict.reasons()[:3]), "failed"
            )
            if table is None:  # nothing table-related to drop: the document itself is refused
                verdict.raise_if_failed()
            spec.pop("table")
            table = None
        return await self._save(info, spec, loaded, table, sctx)

    # --- the table ---------------------------------------------------------------------------

    async def _table(
        self, info: ConnInfo, loaded: LoadedDocument, note: Any, sctx: SyncContext
    ) -> TableChoice | None:
        ui, svc = self.ui, self.services
        redactor = Redactor()
        redactor.know(*info.profile.values())
        stage = await ui.stage("Разбираю таблицу")
        text_note = note if isinstance(note, str) else None
        proposal, _, not_a_table = await tables.propose(
            svc.llm, loaded.extracted, redactor, note=text_note
        )
        if proposal is None and not_a_table:
            options = [
                ("scheduleEvents", "Расписание или экзамены", None),
                ("deadlines", "Сроки сдачи", None),
                ("grades", "Оценки", None),
                ("none", "Ничего, достаточно текста", "Документ останется для поиска"),
            ]
            kind = await ui.choose("В файле есть таблица. Что из неё нужно читать?", options)
            if kind in TABLE_KINDS:
                proposal, _, _ = await tables.propose(
                    svc.llm,
                    loaded.extracted,
                    redactor,
                    note=text_note,
                    force=kind,  # type: ignore[arg-type]
                )
        if proposal is None:
            await stage.finish(
                "skipped", "Таблицу как данные читать не буду: текст доступен для поиска"
            )
            return None

        picked: dict[str, str] = {}
        table = self._source_table(loaded, proposal)
        for target, col in proposal.columns.items():
            if not col.alternatives:
                continue
            options: list[tuple[str, str, str | None]] = [
                (h, h, self._column_hint(table, h)) for h in [col.column, *col.alternatives]
            ]
            answer = await ui.choose(
                f"Какой столбец в таблице — «{FIELD_HELP.get(target, target)}»?", options
            )
            if answer in {o[0] for o in options}:
                picked[target] = str(answer)
        spec = proposal.spec(picked)

        profile = dict(info.profile)
        probe = AdapterFactory().create(
            {"adapter": "document", "source": {"kind": "upload"}, "table": spec}
        )
        assert isinstance(probe, DocumentAdapter)
        records: list[Record] = []
        for attempt in range(2):
            if proposal.row_filter:
                param = proposal.row_filter["equalsParam"]
                profile = await self._row_owner(
                    info, param, proposal.row_filter["column"], profile, again=attempt > 0
                )
                if param not in profile:
                    await stage.finish(
                        "skipped", "Без значения для поиска ваших строк таблица не читается"
                    )
                    return None
            sctx_profile = context_for(svc, info, None, transport=self.transport, retry=self.retry)
            sctx_profile.profile = profile
            records = probe.result_for(loaded, sctx_profile).records
            if records or not proposal.row_filter:
                break
            profile.pop(proposal.row_filter["equalsParam"], None)  # it matched nobody: ask again
        if not records:
            await stage.finish("failed", "В таблице не нашлось подходящих строк")
            await ui.say(
                "По этой таблице не нашлось ни одной вашей строки, поэтому как данные она не "
                "подключена. Текст документа доступен для поиска."
            )
            return None
        await stage.finish("ok", f"Строк: {len(records)}")

        now = svc.clock.now()
        kind = proposal.kind
        assert kind is not None
        items = [item("Найдено строк", str(len(records)))]
        for rec in sample_of(records, kind, now):
            label, value = describe(rec, info.tz)
            items.append(item(label, value or "—"))
        await ui.summary(
            "Вот что читается из таблицы",
            [SummaryGroup(title=KIND_LABELS[kind], items=items)],
        )
        if not await ui.confirm(
            "Это ваши данные? Если да, они будут подключены, а таблица — обновляться по ссылке.",
            "Да, всё верно",
            "Нет, не то",
        ):
            await ui.say("Хорошо, таблицу как данные не подключаю. Текст остаётся для поиска.")
            return None
        return TableChoice(spec, profile)

    @staticmethod
    def _source_table(loaded: LoadedDocument, proposal: tables.Proposal) -> Any:
        wanted = tables.norm(proposal.sheet or "")
        for t in loaded.extracted.tables:
            if not wanted or tables.norm(t.name) == wanted:
                return t
        return loaded.extracted.tables[0]

    @staticmethod
    def _column_hint(table: Any, header: str) -> str | None:
        """Two sample values of a column, for the student's own eyes (not for a model)."""
        try:
            head = next(
                r for r in table.rows if any(tables.norm(c) == tables.norm(header) for c in r)
            )
        except StopIteration:
            return None
        idx = next(i for i, c in enumerate(head) if tables.norm(c) == tables.norm(header))
        at = table.rows.index(head)
        values = [r[idx] for r in table.rows[at + 1 : at + 3] if idx < len(r) and r[idx]]
        return ("например: " + "; ".join(v[:40] for v in values)) if values else None

    async def _row_owner(
        self,
        info: ConnInfo,
        param: str,
        column: str,
        profile: dict[str, str],
        *,
        again: bool = False,
    ) -> dict[str, str]:
        """The value that finds the student's rows: from the profile, else asked and remembered."""
        if profile.get(param):
            return profile
        lead = (
            "Строк с таким значением в столбце «{}» не нашлось."
            if again
            else "В таблице есть строки разных людей (столбец «{}»)."
        ).format(column)
        answer = await self.ui.ask_text(f"{lead} {PARAM_QUESTIONS[param]}")
        if not answer:
            return profile
        async with self.services.db() as db, db.begin():
            space = await db.get(m.Space, info.space_id)
            if space is not None:
                space.profile = {**(space.profile or {}), PROFILE_KEYS[param]: answer}
        return {**profile, param: answer}

    # --- saving ------------------------------------------------------------------------------

    async def _save(
        self,
        info: ConnInfo,
        spec: dict[str, Any],
        loaded: LoadedDocument,
        table: TableChoice | None,
        sctx: SyncContext,
    ) -> dict[str, Any]:
        ui, svc = self.ui, self.services
        stage = await ui.stage("Сохраняю документ")
        now = svc.clock.now()
        source = spec["source"]
        sctx.profile = table.profile if table else dict(info.profile)
        if source["kind"] != "upload" and "url" not in source:
            raise BotaiError(ErrorCode.VALIDATION, "У источника нет ссылки")
        pseudo_origin = f"doc:{info.connection_id}"
        doc = loaded.document
        async with svc.db() as db, db.begin():
            row = await revisions.add_revision(
                db, origin=pseudo_origin, adapter="document", spec=spec, state="canary"
            )
            await revisions.activate(db, row, now=now)
            conn = await db.get(m.Connection, info.connection_id)
            if conn is not None and conn.status != "disabled":
                conn.adapter, conn.spec_id, conn.spec_revision = "document", row.id, row.revision
                conn.kind = "sheet" if doc.kind == "sheet" else "document"
                if source["kind"] != "upload":
                    conn.origin = f"https://{urlsplit(source['url']).hostname}"
                if not conn.title or conn.title in {"Источник", "Документ"}:
                    conn.title = doc.title[:120]
                conn.status = "active"
        async with svc.db() as db:
            snap = await load_snapshot(db, info.user_id, info.connection_id)
        adapter = AdapterFactory().create(spec)
        assert isinstance(adapter, DocumentAdapter)
        result = adapter.result_for(loaded, sctx)
        outcome = await commit_result(svc, snap, result, now, accept_empty=True, queue_repair=False)
        await stage.finish("ok")
        rows = len(result.records)
        message = f"Документ «{doc.title}» добавлен: фрагментов для поиска — {len(doc.chunks)}."
        if table is not None:
            message += f" Из таблицы прочитано строк: {rows}."
        if source["kind"] != "upload":
            message += " Он будет обновляться по ссылке."
        await ui.say(message)
        return {
            "status": outcome.status,
            "format": loaded.format,
            "chunks": len(doc.chunks),
            "rows": rows,
            "next_sync": schedule.freshness_of(spec).every_minutes,
        }


def make_ingest_handler(
    *, transport: httpx.AsyncBaseTransport | None = None, retry: RetryPolicy | None = None
) -> Handler:
    async def handle(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        connection_id = ctx.connection_id or str(payload.get("connection_id", ""))
        if not connection_id:
            raise BotaiError(ErrorCode.VALIDATION, "У задания нет подключения")
        try:
            return await Ingestor(ctx, services, transport=transport, retry=retry).run(
                connection_id, payload
            )
        except NeedsUserInput as need:  # a refresh that cannot find the student's rows
            return {"status": "needs_input", "detail": need.param}

    return handle
