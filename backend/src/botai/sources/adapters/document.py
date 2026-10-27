"""`document` adapter: a file or link -> searchable text and, when asked, table rows.

Text is split into chunks for full-text search (`documents` + `chunks`). A table becomes domain
records by the spec's column map; the rows that belong to the student are picked by a profile
parameter (`rowFilter.equalsParam`), never by a value stored in the spec. An unchanged document
(same hash) is not processed again.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from botai.contracts.specs import DocumentSpec, Kind
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapter import (
    AdapterKind,
    AdapterResult,
    EndpointOutcome,
    ExtractedDocument,
    NeedsUserInput,
    Problem,
    Record,
    SyncContext,
)
from botai.sources.documents import DocFormat, Extracted, Table, detect_format, extract, to_chunks
from botai.sources.extract import apply_transform, collapse, parse_datetime, parse_time, to_utc_iso
from botai.sources.fetch import SourceClient
from botai.sources.vocab import REQUIRED, VOCAB

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GOOGLE_ID = re.compile(r"/(?:document|spreadsheets)/d/([A-Za-z0-9_-]{10,})")


def google_export_url(kind: str, url: str) -> str:
    """Export link of a link-shared Google document or sheet (no sign-in involved)."""
    match = _GOOGLE_ID.search(urlsplit(url).path)
    if not match:
        raise BotaiError(ErrorCode.VALIDATION, "Это не похоже на ссылку на документ Google")
    doc_id = match.group(1)
    if kind == "google_doc":
        return f"https://docs.google.com/document/d/{doc_id}/export?format=docx"
    gid = parse_qs(urlsplit(url).fragment).get("gid") or parse_qs(urlsplit(url).query).get("gid")
    suffix = f"&gid={gid[0]}" if gid and gid[0].isdigit() else ""
    return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx{suffix}"


def _norm(value: str) -> str:
    return collapse(value).casefold().replace("ё", "е")


class DocumentAdapter:
    kind: ClassVar[AdapterKind] = "document"

    def __init__(self, spec: DocumentSpec) -> None:
        self.spec = spec

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        source = self.spec.source
        kind = str(source.get("kind", "upload"))
        url: str | None = source.get("url")
        content_type: str | None = None
        if kind == "upload":
            if ctx.content is None:
                raise BotaiError(ErrorCode.VALIDATION, "Файл не передан")
            data = ctx.content
        else:
            if not url:
                raise BotaiError(ErrorCode.VALIDATION, "У источника нет ссылки")
            target = google_export_url(kind, url) if kind.startswith("google_") else url
            async with SourceClient(ctx, auth_type="none", use_session=False) as client:
                response = await client.request("GET", target, check_session=False)
            if response.status_code >= 400:
                problem: Problem = "not_found" if response.status_code in (404, 410) else "status"
                outcome = EndpointOutcome(
                    "document", None, ok=False, problem=problem, source_url=url
                )
                return AdapterResult([], ctx.now, [outcome], spec_revision=ctx.spec_revision)
            data = response.content
            content_type = response.headers.get("content-type")
        digest = hashlib.sha256(data).hexdigest()
        if ctx.known_hash == digest:
            return AdapterResult([], ctx.now, [], spec_revision=ctx.spec_revision, unchanged=True)

        fmt: DocFormat = self.spec.format or detect_format(data, ctx.filename or url, content_type)
        extracted = extract(data, fmt)
        title = (
            (ctx.filename or "").rsplit(".", 1)[0]
            or extracted.title
            or (urlsplit(url).netloc if url else "")
            or "Документ"
        )
        doc_kind = "sheet" if fmt in ("xlsx", "csv") else "page" if fmt == "html" else "file"
        document = ExtractedDocument(
            title=title[:300],
            kind=doc_kind,
            hash=digest,
            chunks=to_chunks(extracted),
            mime=content_type.split(";")[0] if content_type else None,
            url=url,
        )
        records: list[Record] = []
        outcomes: list[EndpointOutcome] = []
        if self.spec.table:
            records, outcome = self._table(extracted, self.spec.table, ctx, url or "upload:")
            outcomes.append(outcome)
        return AdapterResult(
            records, ctx.now, outcomes, [document], spec_revision=ctx.spec_revision
        )

    # --- table -> records --------------------------------------------------------------------

    def _table(
        self, extracted: Extracted, table_spec: dict[str, Any], ctx: SyncContext, source_url: str
    ) -> tuple[list[Record], EndpointOutcome]:
        kind: Kind = table_spec["kind"]
        outcome = EndpointOutcome("table", kind, ok=True, source_url=source_url)
        table = _pick_table(extracted.tables, table_spec.get("sheet"))
        if table is None:
            outcome.ok, outcome.problem = False, "format"
            return [], outcome
        header_row = int(table_spec.get("headerRow", 0))
        if header_row >= len(table.rows):
            outcome.ok, outcome.problem = False, "format"
            return [], outcome
        headers = [_norm(h) for h in table.rows[header_row]]
        columns: dict[str, dict[str, Any]] = table_spec["columns"]
        index: dict[str, int] = {}
        for target, mapping in columns.items():
            wanted = _norm(str(mapping["column"]))
            if wanted not in headers:
                outcome.ok, outcome.problem = False, "format"
                return [], outcome
            index[target] = headers.index(wanted)

        row_filter = table_spec.get("rowFilter")
        filter_idx: int | None = None
        filter_value = ""
        if row_filter:
            col = _norm(str(row_filter.get("column", "")))
            param = str(row_filter.get("equalsParam", ""))
            if col not in headers:
                outcome.ok, outcome.problem = False, "format"
                return [], outcome
            value = ctx.profile.get(param, "")
            if not value.strip():
                raise NeedsUserInput(param)
            filter_idx, filter_value = headers.index(col), _norm(value)

        records: dict[str, Record] = {}
        seen = 0
        for row in table.rows[header_row + 1 :]:
            if filter_idx is not None:
                cell = row[filter_idx] if filter_idx < len(row) else ""
                if _norm(cell) != filter_value:
                    continue
            seen += 1
            fields = self._row_fields(kind, row, index, columns, ctx)
            if fields is None:
                continue
            ident = json.dumps([kind, sorted(fields.items())], ensure_ascii=False, default=str)
            external_id = hashlib.sha256(ident.encode()).hexdigest()
            records[external_id] = Record(kind, external_id, fields, source_url)
        outcome.items_seen, outcome.items_valid = seen, len(records)
        if not records:
            outcome.problem = "empty" if seen == 0 else "fields"
            outcome.ok = seen == 0
        return list(records.values()), outcome

    def _row_fields(
        self,
        kind: Kind,
        row: list[str],
        index: dict[str, int],
        columns: dict[str, dict[str, Any]],
        ctx: SyncContext,
    ) -> dict[str, Any] | None:
        raw: dict[str, Any] = {}
        for target, idx in index.items():
            cell = row[idx] if idx < len(row) else ""
            if not cell.strip():
                continue
            raw[target] = _table_transform(columns[target].get("transform"), cell, ctx)
        fields = {
            k: v
            for k, v in raw.items()
            if v is not None and k in VOCAB[kind] and k not in {"externalId"}
        }
        self._compose_times(kind, raw, fields, ctx)
        if not REQUIRED[kind] <= fields.keys():
            return None
        if kind == "scheduleEvents" and "endsAt" not in fields:
            return None
        return fields

    @staticmethod
    def _compose_times(
        kind: Kind, raw: dict[str, Any], fields: dict[str, Any], ctx: SyncContext
    ) -> None:
        """Combine separate date/time columns; a date without a time spans the whole day."""
        day = raw.get("date")
        if kind == "scheduleEvents":
            start_t, end_t = parse_time(raw.get("startTime")), parse_time(raw.get("endTime"))
            starts = fields.get("startsAt")
            if isinstance(starts, str) and _DATE_ONLY.match(starts):
                day = starts
                fields.pop("startsAt")
            ends = fields.get("endsAt")
            if isinstance(ends, str) and _DATE_ONLY.match(ends):
                end = _local(ends, time(23, 59), ctx)
                if end:
                    fields["endsAt"] = to_utc_iso(end)
            if "startsAt" not in fields and isinstance(day, str):
                base = _local(day, start_t or time(0, 0), ctx)
                if base:
                    fields["startsAt"] = to_utc_iso(base)
                    if "endsAt" not in fields:
                        end = _local(day, end_t or start_t or time(23, 59), ctx)
                        if end:
                            fields["endsAt"] = to_utc_iso(end)
            elif "startsAt" in fields and "endsAt" not in fields and isinstance(day, str) and end_t:
                end = _local(day, end_t, ctx)
                if end:
                    fields["endsAt"] = to_utc_iso(end)
        elif kind == "deadlines":
            due = fields.get("dueAt")
            if isinstance(due, str) and _DATE_ONLY.match(due):
                day = due
                fields.pop("dueAt")
            if "dueAt" not in fields and isinstance(day, str):
                moment = _local(day, parse_time(raw.get("startTime")) or time(23, 59), ctx)
                if moment:
                    fields["dueAt"] = to_utc_iso(moment)


def _pick_table(tables: list[Table], sheet: str | None) -> Table | None:
    if sheet:
        wanted = _norm(sheet)
        for t in tables:
            if _norm(t.name) == wanted:
                return t
        return None
    return tables[0] if tables else None


def _local(day: str, at: time, ctx: SyncContext) -> datetime | None:
    parsed = parse_datetime(day, ctx.tz)
    if parsed is None:
        return None
    return parsed.replace(hour=at.hour, minute=at.minute, second=at.second)


def _table_transform(name: str | None, cell: str, ctx: SyncContext) -> Any:
    if name is None or name == "trim":
        return collapse(cell)
    if name == "iso":
        dt = parse_datetime(cell, ctx.tz)
        return to_utc_iso(dt) if dt else None
    if name == "date":
        dt = parse_datetime(cell, ctx.tz)
        return dt.date().isoformat() if dt else None
    if name == "time":
        t = parse_time(cell)
        if t is None:
            dt = parse_datetime(cell, ctx.tz)
            t = dt.time() if dt else None
        return t.strftime("%H:%M") if t else None
    if name == "number":
        return apply_transform("number", cell, ctx.tz)
    return None
