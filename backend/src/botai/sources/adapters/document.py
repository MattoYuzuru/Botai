"""`document` adapter: a file or link -> searchable text and, when asked, table rows.

Text is split into chunks for full-text search (`documents` + `chunks`). A table becomes domain
records by the spec's column map; the rows that belong to the student are picked by a profile
parameter (`rowFilter.equalsParam`), never by a value stored in the spec. An unchanged document
(same hash) is not processed again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any, ClassVar
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from botai.contracts.specs import DocumentSpec, Kind
from botai.core.errors import BotaiError, ErrorCode
from botai.sources import public_links
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
_FILENAME_STAR = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", re.I)
_FILENAME = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.I)
MAX_HOPS = 5


def _norm(value: str) -> str:
    return collapse(value).casefold().replace("ё", "е")


def _is_html(content_type: str | None) -> bool:
    return (content_type or "").lower().startswith(("text/html", "application/xhtml"))


def _failed(url: str, status: int) -> EndpointOutcome:
    problem: Problem = "not_found" if status in (404, 410) else "status"
    return EndpointOutcome(
        "document", None, ok=False, problem=problem, source_url=url, status=status
    )


def _disposition_name(header: str | None) -> str | None:
    """The file name a server gives a download (Google names an export after the document)."""
    if not header:
        return None
    match = _FILENAME_STAR.search(header)
    raw = unquote(match.group(1).strip()) if match else None
    if raw is None and (plain := _FILENAME.search(header)):
        raw = plain.group(1).strip()
    name = "".join(
        ch for ch in (raw or "").replace("\\", "/").rsplit("/", 1)[-1] if ch.isprintable()
    )
    return name[:200] or None


async def _get(ctx: SyncContext, target: str) -> httpx.Response:
    async with SourceClient(ctx, auth_type="none", use_session=False) as client:
        return await client.request("GET", target, check_session=False)


async def _get_google(
    ctx: SyncContext, target: str, original: str
) -> httpx.Response | EndpointOutcome:
    """An export of a link-shared Google document. Google answers with a redirect to a file host
    named after the document, so every hop is a request of its own to a host that is checked
    against Google's own and granted for that request only. A redirect to the sign-in page means
    the document is not shared by link."""
    current = target
    for _ in range(MAX_HOPS):
        hop = replace(ctx, allow_hosts=((urlsplit(current).hostname or "").lower(),))
        async with SourceClient(
            hop, auth_type="none", use_session=False, follow_redirects=False
        ) as client:
            response = await client.request("GET", current, check_session=False)
        location = response.headers.get("location")
        if not (response.is_redirect and location):
            return response
        current = urljoin(current, location)
        if public_links.is_sign_in(current):
            return _failed(original, 403)
        if not public_links.hop_allowed(current):
            return _failed(original, 502)
    return _failed(original, 508)


@dataclass(slots=True)
class LoadedDocument:
    format: DocFormat
    extracted: Extracted
    document: ExtractedDocument


class DocumentAdapter:
    kind: ClassVar[AdapterKind] = "document"

    def __init__(self, spec: DocumentSpec) -> None:
        self.spec = spec

    async def fetch_bytes(self, ctx: SyncContext) -> tuple[bytes, str | None] | EndpointOutcome:
        """The raw document: the uploaded bytes, or the download. A failed download is a problem
        of the source (an outcome), not an exception; a document that is not shared by link is an
        outcome with status 403."""
        source = self.spec.source
        kind = str(source.get("kind", "upload"))
        url: str | None = source.get("url")
        if kind == "upload":
            if ctx.content is None:
                raise BotaiError(ErrorCode.VALIDATION, "Файл не передан")
            return ctx.content, None
        if not url:
            raise BotaiError(ErrorCode.VALIDATION, "У источника нет ссылки")
        target = public_links.export_url(url)
        google = public_links.is_google(target)
        response = await (_get_google(ctx, target, url) if google else _get(ctx, target))
        if isinstance(response, EndpointOutcome):
            return response
        content_type = response.headers.get("content-type")
        if response.status_code >= 400:
            return _failed(url, response.status_code)
        if google and _is_html(content_type) and not urlsplit(target).path.endswith("/pub"):
            return _failed(url, 403)  # a sign-in page instead of the file
        ctx.filename = ctx.filename or _disposition_name(
            response.headers.get("content-disposition")
        )
        return response.content, content_type

    def process(self, data: bytes, content_type: str | None, ctx: SyncContext) -> LoadedDocument:
        url: str | None = self.spec.source.get("url")
        fmt: DocFormat = self.spec.format or detect_format(data, ctx.filename or url, content_type)
        extracted = extract(data, fmt)
        # The last path segment of an editor link ("edit") is not a name; a host is not either.
        leaf = (
            urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if url and not public_links.is_google(url)
            else ""
        )
        title = (
            (ctx.filename or "").rsplit(".", 1)[0]
            or extracted.title
            or leaf
            or ("Таблица" if fmt in ("xlsx", "csv") else "Документ")
        )
        doc_kind = "sheet" if fmt in ("xlsx", "csv") else "page" if fmt == "html" else "file"
        document = ExtractedDocument(
            title=title[:300],
            kind=doc_kind,
            hash=hashlib.sha256(data).hexdigest(),
            chunks=to_chunks(extracted),
            mime=content_type.split(";")[0] if content_type else None,
            url=url,
        )
        return LoadedDocument(fmt, extracted, document)

    def result_for(self, loaded: LoadedDocument, ctx: SyncContext) -> AdapterResult:
        records: list[Record] = []
        outcomes: list[EndpointOutcome] = []
        if self.spec.table:
            records, outcome = self._table(
                loaded.extracted, self.spec.table, ctx, loaded.document.url or "upload:"
            )
            outcomes.append(outcome)
        return AdapterResult(
            records, ctx.now, outcomes, [loaded.document], spec_revision=ctx.spec_revision
        )

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        raw = await self.fetch_bytes(ctx)
        if isinstance(raw, EndpointOutcome):
            return AdapterResult([], ctx.now, [raw], spec_revision=ctx.spec_revision)
        data, content_type = raw
        if ctx.known_hash == hashlib.sha256(data).hexdigest():
            return AdapterResult([], ctx.now, [], spec_revision=ctx.spec_revision, unchanged=True)
        loaded = await asyncio.to_thread(self.process, data, content_type, ctx)
        return self.result_for(loaded, ctx)

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
