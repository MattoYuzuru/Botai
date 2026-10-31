"""Playbook runner: probe, sync, repair and canary of one v4 source.

A run resolves a playbook version (or an inline candidate spec/code), fetches the endpoints
through the single v4 HTTP client factory, saves artifacts into a content-addressed store, parses
declarative fields or executes the version's parser code in the executor sandbox, coerces records
with the existing `sources.normalize.coerce` and reports counts, errors and provenance.

Nothing here evaluates agent code: `code` goes to `Executor.run` (LocalExecutor by default), a
separate process with its own workspace. Nothing here writes the domain tables either: v4 sources
have no `connection_id`, and adding `source_id` to the domain is a separate migration. The runner
therefore returns normalized records in the report and persists artifacts, cursor/site map and the
source status/freshness; the domain write is the next step (docs/v4/16-sources-runner-tools.md).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.executor import ExecRequest, Executor, LocalExecutor, Workspace
from botai.agent.v4.http import build_client
from botai.contracts.specs import Kind
from botai.core.clock import Clock, SystemClock
from botai.db.models import PlaybookVersion, Source, Space
from botai.sources import extract
from botai.sources.adapter import Record
from botai.sources.normalize import coerce
from botai.sources.v4 import artifacts, identity, playbooks, service, state
from botai.sources.v4 import kinds as kinds_vocab

RunKind = Literal["probe", "sync", "repair", "canary"]
ProgressHook = Callable[[Mapping[str, Any]], Awaitable[None]]
SecretReader = Callable[[str], Awaitable[str | None]]
ExecutorFactory = Callable[[], Executor]

DEFAULT_TIMEZONE = "Europe/Moscow"
MAX_REPORT_RECORDS = 200
MAX_ERROR_DETAIL = 2000

V4_TO_KIND: dict[str, Kind] = {
    "schedule": "scheduleEvents",
    "deadlines": "deadlines",
    "assignments": "assignments",
    "courses": "courses",
    "grades": "grades",
    "people": "people",
    "locations": "locations",
    "news": "news",
}
KIND_TO_V4: dict[Kind, str] = {kind: name for name, kind in V4_TO_KIND.items()}
SITE_IDENTITY_PREFIXES = ("http://", "https://")


# --- declarative playbook spec ------------------------------------------------------------------


class FieldSpec(BaseModel):
    """How to take one field from one item: a JSON pointer or an HTML selector."""

    model_config = ConfigDict(extra="forbid")

    pointer: str | None = None
    selector: str | None = None
    attr: str | None = None
    pattern: str | None = None
    transform: str | None = None


class ReadSpec(BaseModel):
    """One request of a playbook and how its items become records."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,48}$")
    kind: str | None = None
    url: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "POST"] = "GET"
    body: Any = None
    format: Literal["json", "html", "text"] = "json"
    items_path: str | None = None  # JSON pointer to the list; None = the document is the list
    items_selector: str | None = None  # CSS selector of list items (HTML)
    fields: dict[str, str | FieldSpec] = Field(default_factory=dict)
    open_url: str | None = None  # the page that shows these records to the student
    min_items: int = Field(default=0, ge=0)


class PlaybookSpecV1(BaseModel):
    """The declarative half of a playbook version. Its `version` is the spec format, not a revision."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    reads: list[ReadSpec] = Field(default_factory=list, max_length=50)
    timeout_s: float = Field(default=30.0, ge=1, le=300)

    @model_validator(mode="after")
    def _names_are_unique(self) -> PlaybookSpecV1:
        names = [read.name for read in self.reads]
        if len(names) != len(set(names)):
            raise ValueError("read names must be unique")
        return self


class CodeRecord(BaseModel):
    """One record returned by parser code (v4 or reading kind name)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    external_id: str = Field(min_length=1, max_length=200)
    fields: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None
    open_url: str | None = None


class CodeDocument(BaseModel):
    """A document returned by parser code; its text is saved as an artifact."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    text: str
    url: str | None = None
    mime: str | None = None


class CodeOutput(BaseModel):
    """What parser code prints: a single JSON object on the last non-empty line of stdout."""

    model_config = ConfigDict(extra="forbid")

    records: list[CodeRecord] = Field(default_factory=list, max_length=20_000)
    documents: list[CodeDocument] = Field(default_factory=list, max_length=1000)
    cursor: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=2000)


# --- report --------------------------------------------------------------------------------------


class RunError(BaseModel):
    """A structured, secret-free failure of one read or of the whole run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    detail: str = ""
    read: str | None = None
    retryable: bool = False


class ReadOutcome(BaseModel):
    """What one read did: transport result plus how many records it produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str | None
    format: str
    ok: bool
    status: int | None = None
    items_seen: int = 0
    items_valid: int = 0
    items_invalid: int = 0
    artifact_id: str | None = None
    error: RunError | None = None


class NormalizedRecord(BaseModel):
    """A record in the reading vocabulary of the playbook (the domain write is separate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    external_id: str
    fields: dict[str, Any]
    source_url: str | None = None
    open_url: str | None = None


class RunReport(BaseModel):
    """The honest outcome of one run: counts, errors, provenance and the new source state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    run: RunKind
    version: int | None = None
    ok: bool = False
    source_status: str | None = None
    started_at: datetime
    finished_at: datetime
    reads: list[ReadOutcome] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    records: list[NormalizedRecord] = Field(default_factory=list)
    records_truncated: bool = False
    errors: list[RunError] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    cursor: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)
    persisted: bool = False


class LiveReadResult(BaseModel):
    """One live read of a source's page/API with its session: enough for the agent to look."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    url: str | None
    status: int
    content_type: str | None
    text: str
    truncated: bool = False
    artifact_id: str | None = None
    ok: bool = True
    error: RunError | None = None


# --- runner --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Fetched:
    read: ReadSpec
    url: str
    sanitized_url: str | None
    status: int
    content_type: str | None
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class _Parsed:
    records: list[NormalizedRecord] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[RunError] = field(default_factory=list)
    outcomes: list[ReadOutcome] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


class SourceRunner:
    """Runs playbook versions of v4 sources. One runner = one session/user; call it per request."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        artifact_store: artifacts.ArtifactStore | None = None,
        executor_factory: ExecutorFactory | None = None,
        clock: Clock | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_reader: SecretReader | None = None,
        workspace_root: Path | None = None,
        report_record_limit: int = MAX_REPORT_RECORDS,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._transport = transport
        self._artifact_store = artifact_store
        self._executor_factory = executor_factory
        self._clock = clock or SystemClock()
        self._cookies = dict(cookies or {})
        self._headers = dict(headers or {})
        self._secret_reader = secret_reader
        self._workspace_root = workspace_root
        self._report_record_limit = max(1, report_record_limit)
        self._progress: ProgressHook | None = None

    # --- public runs ---------------------------------------------------------------------------

    async def probe(
        self,
        source_id: str,
        *,
        spec: Mapping[str, Any] | None = None,
        code: str | None = None,
        language: str = "python",
        version: int | None = None,
        persist: bool = False,
        progress: ProgressHook | None = None,
    ) -> RunReport:
        """Try a candidate or the active playbook without changing the source's status."""
        return await self._run(
            source_id,
            run="probe",
            inline_spec=spec,
            inline_code=code,
            inline_language=language,
            version=version,
            persist=persist,
            progress=progress,
        )

    async def sync(
        self,
        source_id: str,
        *,
        version: int | None = None,
        persist: bool = True,
        progress: ProgressHook | None = None,
    ) -> RunReport:
        """Run the active (or the named) version and record freshness, artifacts and state."""
        return await self._run(
            source_id,
            run="sync",
            inline_spec=None,
            inline_code=None,
            inline_language="python",
            version=version,
            persist=persist,
            progress=progress,
        )

    async def repair(
        self,
        source_id: str,
        *,
        spec: Mapping[str, Any] | None = None,
        code: str | None = None,
        language: str = "python",
        version: int | None = None,
        persist: bool = True,
        progress: ProgressHook | None = None,
    ) -> RunReport:
        """Run a candidate version/spec and record what it found; activation is a separate step."""
        return await self._run(
            source_id,
            run="repair",
            inline_spec=spec,
            inline_code=code,
            inline_language=language,
            version=version,
            persist=persist,
            progress=progress,
        )

    async def canary(
        self,
        source_id: str,
        *,
        version: int | None = None,
        progress: ProgressHook | None = None,
    ) -> RunReport:
        """Run a version as a canary: record its result and health, do not touch the source status."""
        report = await self._run(
            source_id,
            run="canary",
            inline_spec=None,
            inline_code=None,
            inline_language="python",
            version=version,
            persist=False,
            progress=progress,
        )
        return report

    async def read_live(
        self,
        source_id: str,
        *,
        url: str,
        method: Literal["GET", "POST"] = "GET",
        body: Any = None,
        max_chars: int = 20_000,
        save_artifact: bool = True,
    ) -> LiveReadResult:
        """Read one page/API of the source with its session; the text is what the agent may see."""
        source = await service.get_source(self._session, user_id=self._user_id, source_id=source_id)
        resolved = self._read_url(source, url)
        headers = await self._auth_headers(source)
        client = build_client(
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0),
            headers=headers,
            cookies=dict(self._cookies),
        )
        try:
            response = await client.request(method, resolved, json=body, timeout=60.0)
        except httpx.HTTPError as exc:
            return LiveReadResult(
                source_id=source.id,
                url=self._sanitize(resolved),
                status=0,
                content_type=None,
                text="",
                ok=False,
                error=RunError(code="source_unavailable", detail=_short(exc), retryable=True),
            )
        finally:
            await client.aclose()
        content_type = response.headers.get("content-type")
        sanitized = self._sanitize(str(response.url)) or self._sanitize(resolved)
        artifact_id = (
            await self._artifact(
                source.id,
                resolved,
                response.content,
                format="html" if "html" in (content_type or "") else "json",
                content_type=content_type,
                status=response.status_code,
                read_name="live",
            )
            if save_artifact
            else None
        )
        text = response.content.decode(response.encoding or "utf-8", errors="replace")
        truncated = len(text) > max_chars
        if response.status_code >= 400:
            code = (
                "source_auth_required"
                if response.status_code in (401, 403)
                else "source_unavailable"
                if response.status_code >= 500
                else "source_rate_limited"
                if response.status_code == 429
                else "read_failed"
            )
            return LiveReadResult(
                source_id=source.id,
                url=sanitized,
                status=response.status_code,
                content_type=content_type,
                text=text[:max_chars],
                truncated=truncated,
                artifact_id=artifact_id,
                ok=False,
                error=RunError(code=code, detail=f"HTTP {response.status_code}"),
            )
        return LiveReadResult(
            source_id=source.id,
            url=sanitized,
            status=response.status_code,
            content_type=content_type,
            text=text[:max_chars],
            truncated=truncated,
            artifact_id=artifact_id,
        )

    # --- one run -------------------------------------------------------------------------------

    async def _run(
        self,
        source_id: str,
        *,
        run: RunKind,
        inline_spec: Mapping[str, Any] | None,
        inline_code: str | None,
        inline_language: str,
        version: int | None,
        persist: bool,
        progress: ProgressHook | None,
    ) -> RunReport:
        self._progress = progress
        started = self._clock.now()
        source = await service.get_source(self._session, user_id=self._user_id, source_id=source_id)
        space = await self._session.get(Space, source.space_id)
        tz = self._timezone(space)
        playbook_row = None
        version_row = None
        if source.playbook_id is not None:
            playbook_row = await playbooks.get_playbook(
                self._session, user_id=self._user_id, playbook_id=source.playbook_id
            )
            if version is not None:
                version_row = await playbooks.get_playbook_version(
                    self._session,
                    user_id=self._user_id,
                    playbook_id=source.playbook_id,
                    version=version,
                )
            elif run == "sync":
                version_row = await playbooks.active_playbook_version(
                    self._session, user_id=self._user_id, playbook_id=source.playbook_id
                )
            elif run == "canary":
                # A canary without a version validates the newest one: the candidate the owner
                # is about to activate (the active one, if that is the newest).
                versions = await playbooks.list_playbook_versions(
                    self._session, user_id=self._user_id, playbook_id=source.playbook_id
                )
                if versions:
                    version_row = versions[0]

        spec_payload = (
            inline_spec if inline_spec is not None else version_row.spec if version_row else None
        )
        code_payload = (
            inline_code if inline_code is not None else version_row.code if version_row else None
        )
        language = (
            playbook_row.language
            if inline_code is None and playbook_row and playbook_row.language in ("python", "js")
            else inline_language
        )
        errors: list[RunError] = []
        notes: list[str] = []
        spec: PlaybookSpecV1 | None = None
        if spec_payload is not None:
            try:
                spec = PlaybookSpecV1.model_validate(spec_payload)
            except ValidationError as exc:
                errors.append(RunError(code="recipe_rejected", detail=_validation_detail(exc)))
        if spec is None and code_payload is None:
            detail = (
                "У playbook источника нет активной версии: прогоните canary и активируйте версию"
                if playbook_row is not None
                else "У источника нет playbook: выполните probe с черновиком spec или code"
            )
            errors.append(RunError(code="playbook_required", detail=detail))
        if spec is not None and code_payload is None:
            errors.extend(self._check_declarative(spec))
        if errors:
            finished = self._clock.now()
            return RunReport(
                source_id=source.id,
                run=run,
                version=version_row.version if version_row else version,
                ok=False,
                source_status=source.status,
                started_at=started,
                finished_at=finished,
                errors=errors,
            )

        await self._emit(
            {
                "event": "status",
                "run": run,
                "sourceId": source.id,
                "reads": len(spec.reads) if spec else 0,
            }
        )
        fetched: list[_Fetched] = []
        outcomes: list[ReadOutcome] = []
        if spec is not None and spec.reads:
            fetched, outcomes, fetch_errors = await self._fetch_all(source, spec, persist=persist)
            errors.extend(fetch_errors)
        parsed = _Parsed()
        if not errors:
            if code_payload is not None:
                parsed = await self._run_code(
                    source, spec, fetched, code_payload, language, tz, persist
                )
            elif spec is not None:
                parsed = await self._parse_declarative(spec, fetched, tz)
        errors.extend(parsed.errors)
        all_outcomes = [*outcomes, *parsed.outcomes]
        counts = dict(parsed.counts)
        report_ok = not errors and self._meets_minimum(spec, all_outcomes)
        if not report_ok and not errors:
            errors.append(
                RunError(
                    code="recipe_drift", detail="Источник прочитан, но записей меньше ожидаемого"
                )
            )
        errors = errors[:50]
        finished = self._clock.now()
        report = RunReport(
            source_id=source.id,
            run=run,
            version=version_row.version if version_row else version,
            ok=report_ok,
            source_status=source.status,
            started_at=started,
            finished_at=finished,
            reads=all_outcomes,
            counts=counts,
            records=parsed.records[: self._report_record_limit],
            records_truncated=len(parsed.records) > self._report_record_limit,
            errors=errors,
            artifact_ids=[o.artifact_id for o in all_outcomes if o.artifact_id],
            cursor=parsed.cursor,
            notes=[*notes, *parsed.notes],
            persisted=False,
        )
        if persist and run in ("sync", "repair"):
            report = await self._persist(
                report, source, spec, version_row, update_status=run == "sync"
            )
        if run == "canary" and version_row is not None:
            await self._record_canary(report)
        await self._emit(
            {
                "event": "done",
                "run": run,
                "ok": report.ok,
                "counts": report.counts,
                "errors": [error.model_dump() for error in report.errors],
            }
        )
        return report

    # --- fetching ------------------------------------------------------------------------------

    async def _fetch_all(
        self, source: Source, spec: PlaybookSpecV1, *, persist: bool
    ) -> tuple[list[_Fetched], list[ReadOutcome], list[RunError]]:
        fetched: list[_Fetched] = []
        outcomes: list[ReadOutcome] = []
        errors: list[RunError] = []
        headers = await self._auth_headers(source)
        client = build_client(
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=spec.timeout_s, write=20.0, pool=10.0),
            headers=headers,
            cookies=dict(self._cookies),
        )
        try:
            for read in spec.reads:
                try:
                    url = self._read_url(source, read.url)
                except ValueError as exc:
                    error = RunError(code="read_failed", detail=str(exc), read=read.name)
                    errors.append(error)
                    outcomes.append(
                        ReadOutcome(
                            name=read.name, url=None, format=read.format, ok=False, error=error
                        )
                    )
                    continue
                await self._emit({"event": "read", "name": read.name, "url": self._sanitize(url)})
                try:
                    response = await client.request(
                        read.method,
                        url,
                        json=read.body if read.body is not None else None,
                        timeout=spec.timeout_s,
                    )
                except httpx.HTTPError as exc:
                    error = RunError(
                        code="source_unavailable",
                        detail=_short(exc),
                        read=read.name,
                        retryable=True,
                    )
                    errors.append(error)
                    outcomes.append(
                        ReadOutcome(
                            name=read.name,
                            url=self._sanitize(url),
                            format=read.format,
                            ok=False,
                            error=error,
                        )
                    )
                    continue
                sanitized = self._sanitize(str(response.url)) or self._sanitize(url)
                if response.status_code >= 400:
                    code = (
                        "source_auth_required"
                        if response.status_code in (401, 403)
                        else "source_rate_limited"
                        if response.status_code == 429
                        else "source_unavailable"
                        if response.status_code >= 500
                        else "read_failed"
                    )
                    error = RunError(
                        code=code,
                        detail=f"HTTP {response.status_code}",
                        read=read.name,
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                    errors.append(error)
                    outcomes.append(
                        ReadOutcome(
                            name=read.name,
                            url=sanitized,
                            format=read.format,
                            ok=False,
                            status=response.status_code,
                            error=error,
                        )
                    )
                    if code == "source_auth_required":
                        break
                    continue
                content_type = response.headers.get("content-type")
                artifact_id = (
                    await self._artifact(
                        source.id,
                        url,
                        response.content,
                        format=read.format,
                        content_type=content_type,
                        status=response.status_code,
                        read_name=read.name,
                    )
                    if persist
                    else None
                )
                fetched.append(
                    _Fetched(
                        read=read,
                        url=str(response.url),
                        sanitized_url=sanitized,
                        status=response.status_code,
                        content_type=content_type,
                        body=response.content,
                    )
                )
                outcomes.append(
                    ReadOutcome(
                        name=read.name,
                        url=sanitized,
                        format=read.format,
                        ok=True,
                        status=response.status_code,
                        artifact_id=artifact_id,
                    )
                )
        finally:
            await client.aclose()
        return fetched, outcomes, errors

    def _read_url(self, source: Source, raw: str) -> str:
        value = raw.strip()
        parts = urlsplit(value)
        if parts.scheme in ("http", "https"):
            return value
        base = source.entry_url
        if not base and source.identity_key.startswith(SITE_IDENTITY_PREFIXES):
            base = source.identity_key
        if not base:
            raise ValueError("Относительный адрес без entry_url источника")
        return urljoin(base.rstrip("/") + "/", value)

    async def _auth_headers(self, source: Source) -> dict[str, str]:
        headers = dict(self._headers)
        if source.auth_ref and self._secret_reader is not None:
            secret = await self._secret_reader(source.auth_ref)
            if secret:
                headers.setdefault("authorization", f"Bearer {secret}")
        return headers

    # --- declarative parsing -------------------------------------------------------------------

    def _check_declarative(self, spec: PlaybookSpecV1) -> list[RunError]:
        errors: list[RunError] = []
        if not spec.reads:
            errors.append(RunError(code="recipe_rejected", detail="В spec нет ни одного read"))
        for read in spec.reads:
            if read.kind is None:
                errors.append(
                    RunError(code="recipe_rejected", detail=f"У read {read.name} нет kind")
                )
            elif read.kind not in kinds_vocab.ALL_KINDS:
                errors.append(
                    RunError(
                        code="recipe_rejected",
                        detail=f"У read {read.name} неизвестный kind: {read.kind}",
                    )
                )
            if not read.fields:
                errors.append(
                    RunError(code="recipe_rejected", detail=f"У read {read.name} нет fields")
                )
            if read.format == "html" and not read.items_selector:
                errors.append(
                    RunError(
                        code="recipe_rejected",
                        detail=f"У HTML-read {read.name} нет items_selector",
                    )
                )
            if read.format == "text":
                errors.append(
                    RunError(
                        code="recipe_rejected",
                        detail=f"Read {read.name}: text читается кодом, а не декларативно",
                    )
                )
        return errors

    async def _parse_declarative(
        self, spec: PlaybookSpecV1, fetched: list[_Fetched], tz: ZoneInfo
    ) -> _Parsed:
        parsed = _Parsed()
        by_name = {item.read.name: item for item in fetched}
        for read in spec.reads:
            item = by_name.get(read.name)
            if item is None:
                continue
            try:
                items = self._items(read, item)
            except ValueError as exc:
                error = RunError(code="recipe_drift", detail=str(exc), read=read.name)
                parsed.errors.append(error)
                parsed.outcomes.append(
                    ReadOutcome(
                        name=read.name,
                        url=item.sanitized_url,
                        format=read.format,
                        ok=False,
                        status=item.status,
                        error=error,
                    )
                )
                continue
            seen = len(items)
            valid = 0
            invalid = 0
            for raw_item in items:
                record = self._record(read, raw_item, item, tz)
                if record is None:
                    invalid += 1
                    continue
                valid += 1
                parsed.records.append(record)
                parsed.counts[read.kind or "other"] = parsed.counts.get(read.kind or "other", 0) + 1
            parsed.outcomes.append(
                ReadOutcome(
                    name=read.name,
                    url=item.sanitized_url,
                    format=read.format,
                    ok=True,
                    status=item.status,
                    items_seen=seen,
                    items_valid=valid,
                    items_invalid=invalid,
                )
            )
        return parsed

    def _items(self, read: ReadSpec, item: _Fetched) -> list[Any]:
        text = item.text()
        if read.format == "json":
            try:
                document = json.loads(text)
            except ValueError:
                raise ValueError(f"Read {read.name}: ответ не JSON") from None
            if read.items_path is None:
                value: Any = document
            else:
                value = extract.resolve(document, read.items_path)
                if value is extract.MISSING:
                    raise ValueError(f"Read {read.name}: нет items_path {read.items_path}")
            if isinstance(value, list):
                return value
            return [value]
        if read.format == "html":
            tree = extract.parse_html(text)
            selector = read.items_selector or ""
            nodes = extract.select_items(tree, selector)
            if not nodes:
                raise ValueError(f"Read {read.name}: селектор {selector!r} ничего не нашёл")
            return list(nodes)
        raise ValueError(f"Read {read.name}: формат {read.format} не декларативный")

    def _record(
        self, read: ReadSpec, raw_item: Any, item: _Fetched, tz: ZoneInfo
    ) -> NormalizedRecord | None:
        fields = self._fields(read, raw_item, tz)
        external_id = fields.get("externalId")
        if external_id is None:
            return None
        payload = {name: value for name, value in fields.items() if name != "externalId"}
        v3_kind = V4_TO_KIND.get(read.kind or "")
        source_url = item.sanitized_url
        if v3_kind is None:
            return NormalizedRecord(
                kind=read.kind or "other",
                external_id=str(external_id)[:200],
                fields=payload,
                source_url=source_url,
                open_url=self._sanitize(read.open_url) or source_url,
            )
        record = Record(
            kind=v3_kind,
            external_id=str(external_id),
            fields=payload,
            source_url=item.url,
            open_url=read.open_url,
        )
        try:
            row = coerce(v3_kind, record, tz)
        except (KeyError, TypeError, ValueError):
            return None
        if row is None:
            return None
        return NormalizedRecord(
            kind=read.kind or KIND_TO_V4[v3_kind],
            external_id=str(external_id)[:200],
            fields=row,
            source_url=source_url,
            open_url=self._sanitize(read.open_url) or source_url,
        )

    def _fields(self, read: ReadSpec, raw_item: Any, tz: ZoneInfo) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, raw_spec in read.fields.items():
            value: Any = None
            if read.format == "json":
                field_spec = FieldSpec() if isinstance(raw_spec, str) else raw_spec
                pointer = raw_spec if isinstance(raw_spec, str) else field_spec.pointer
                if pointer is not None:
                    value = extract.resolve(raw_item, pointer)
                    if value is extract.MISSING:
                        value = None
                if value is not None and field_spec.transform:
                    value = extract.apply_transform(field_spec.transform, value, tz)
            else:
                field_spec = FieldSpec(selector=raw_spec) if isinstance(raw_spec, str) else raw_spec
                value = extract.html_value(
                    raw_item,
                    selector=field_spec.selector or ".",
                    attr=field_spec.attr,
                    pattern=field_spec.pattern,
                    transform=field_spec.transform,
                    tz=tz,
                )
            if value is not None:
                result[name] = value
        return result

    def _meets_minimum(self, spec: PlaybookSpecV1 | None, outcomes: Sequence[ReadOutcome]) -> bool:
        if spec is None:
            return True
        by_name = {outcome.name: outcome for outcome in outcomes}
        for read in spec.reads:
            outcome = by_name.get(read.name)
            if outcome is None or not outcome.ok:
                return False
            if outcome.items_seen < read.min_items:
                return False
        return True

    # --- parser code ---------------------------------------------------------------------------

    async def _run_code(
        self,
        source: Source,
        spec: PlaybookSpecV1 | None,
        fetched: list[_Fetched],
        code: str,
        language: str,
        tz: ZoneInfo,
        persist: bool,
    ) -> _Parsed:
        parsed = _Parsed()
        files: dict[str, str] = {}
        for item in fetched:
            extension = {"json": "json", "html": "html", "text": "txt"}[item.read.format]
            files[f"reads/{item.read.name}.{extension}"] = item.text()
        executor = (
            self._executor_factory()
            if self._executor_factory is not None
            else LocalExecutor(
                workspace=Workspace.create(self._workspace_root),
            )
        )
        try:
            result = await executor.run(
                ExecRequest(
                    language=language if language in ("python", "js") else "python",
                    code=code,
                    files=files,
                    timeout_s=(spec.timeout_s if spec else 30.0) + 30.0,
                )
            )
        except Exception as exc:  # executor refused or unavailable: an honest run error
            parsed.errors.append(RunError(code="executor_failed", detail=_short(exc)))
            return parsed
        finally:
            if isinstance(executor, LocalExecutor):
                executor.close()
        if not result.ok:
            parsed.errors.append(
                RunError(
                    code="executor_failed",
                    detail=_short(f"exit {result.exit_code}: {result.stderr}"),
                )
            )
            return parsed
        payload = _last_json_object(result.stdout)
        if payload is None:
            parsed.errors.append(
                RunError(code="recipe_drift", detail="Код не напечатал JSON-объект с records")
            )
            return parsed
        try:
            output = CodeOutput.model_validate(payload)
        except ValidationError as exc:
            parsed.errors.append(RunError(code="recipe_drift", detail=_validation_detail(exc)))
            return parsed
        for raw_record in output.records:
            kind_v4 = _v4_kind(raw_record.kind)
            if kind_v4 is None:
                parsed.errors.append(
                    RunError(
                        code="recipe_drift",
                        detail=f"Код вернул неизвестный вид записи: {raw_record.kind}",
                    )
                )
                continue
            v3_kind = V4_TO_KIND.get(kind_v4)
            if v3_kind is None:
                parsed.records.append(
                    NormalizedRecord(
                        kind=kind_v4,
                        external_id=raw_record.external_id[:200],
                        fields=raw_record.fields,
                        source_url=self._sanitize(raw_record.source_url or ""),
                        open_url=self._sanitize(raw_record.open_url or ""),
                    )
                )
                continue
            record = Record(
                kind=v3_kind,
                external_id=raw_record.external_id,
                fields=raw_record.fields,
                source_url=self._sanitize(raw_record.source_url or "") or "",
                open_url=raw_record.open_url,
            )
            try:
                row = coerce(v3_kind, record, tz)
            except (KeyError, TypeError, ValueError):
                row = None
            if row is None:
                continue
            parsed.records.append(
                NormalizedRecord(
                    kind=kind_v4,
                    external_id=raw_record.external_id[:200],
                    fields=row,
                    source_url=self._sanitize(raw_record.source_url or ""),
                    open_url=self._sanitize(raw_record.open_url or ""),
                )
            )
            parsed.counts[kind_v4] = parsed.counts.get(kind_v4, 0) + 1
        for document in output.documents:
            if persist:
                await self._artifact(
                    source.id,
                    document.url or "",
                    document.text.encode(),
                    format="text",
                    content_type=document.mime,
                    status=200,
                    read_name="document",
                    kind="doc",
                )
        if output.cursor is not None:
            parsed.cursor = dict(output.cursor)
        if output.notes:
            parsed.notes.append(output.notes)
        return parsed

    # --- persistence and reporting -------------------------------------------------------------

    async def _persist(
        self,
        report: RunReport,
        source: Source,
        spec: PlaybookSpecV1 | None,
        version_row: PlaybookVersion | None,
        *,
        update_status: bool = True,
    ) -> RunReport:
        now = self._clock.now()
        if update_status and source.status not in ("disabled", "archived"):
            if report.ok:
                await service.mark_synced(
                    self._session,
                    user_id=self._user_id,
                    source_id=source.id,
                    synced_at=now,
                    status="active",
                )
            else:
                auth = any(error.code == "source_auth_required" for error in report.errors)
                await service.mark_error(
                    self._session,
                    user_id=self._user_id,
                    source_id=source.id,
                    status="needs_reauth" if auth else "error",
                    error={
                        "code": report.errors[0].code if report.errors else "source_error",
                        "detail": report.errors[0].detail if report.errors else "",
                        "at": now.isoformat(),
                    },
                )
        await state.upsert_source_state(
            self._session,
            user_id=self._user_id,
            source_id=source.id,
            last_revision=version_row.version if version_row else None,
            cursor=report.cursor if report.cursor is not None else None,
            endpoints=self._endpoints(spec),
        )
        await self._session.commit()
        status = (
            "active"
            if report.ok
            else (
                "needs_reauth"
                if any(e.code == "source_auth_required" for e in report.errors)
                else "error"
            )
        )
        return report.model_copy(update={"persisted": True, "source_status": status})

    async def _record_canary(self, report: RunReport) -> None:
        if report.version is None:
            return
        source = await service.get_source(
            self._session, user_id=self._user_id, source_id=report.source_id
        )
        if source.playbook_id is None:
            return
        await playbooks.record_canary(
            self._session,
            user_id=self._user_id,
            playbook_id=source.playbook_id,
            version=report.version,
            canary={
                "ok": report.ok,
                "checked_at": report.finished_at.isoformat(),
                "counts": report.counts,
                "errors": [error.model_dump() for error in report.errors],
            },
        )
        await playbooks.set_playbook_health(
            self._session,
            user_id=self._user_id,
            playbook_id=source.playbook_id,
            health={
                "ok": report.ok,
                "last_run": "canary",
                "checked_at": report.finished_at.isoformat(),
                "counts": report.counts,
                "error_count": len(report.errors),
            },
        )
        await self._session.commit()

    async def _artifact(
        self,
        source_id: str,
        url: str,
        data: bytes,
        *,
        format: str,
        content_type: str | None,
        status: int,
        read_name: str,
        kind: str | None = None,
    ) -> str | None:
        digest = hashlib.sha256(data).hexdigest()
        storage_ref: str | None = None
        if self._artifact_store is not None:
            blob = self._artifact_store.put(data)
            digest = blob.sha256
            storage_ref = blob.storage_ref
        if kind is None:
            kind = {"json": "json", "html": "page", "text": "other"}.get(format, "other")
        row = await artifacts.add_artifact(
            self._session,
            user_id=self._user_id,
            source_id=source_id,
            kind=kind,
            sha256=digest,
            url=self._sanitize(url),
            content_type=content_type,
            size=len(data),
            storage_ref=storage_ref,
            metadata={"read": read_name, "status": status, "format": format},
            captured_at=self._clock.now(),
        )
        return row.id

    def _endpoints(self, spec: PlaybookSpecV1 | None) -> list[dict[str, Any]]:
        if spec is None:
            return []
        endpoints: list[dict[str, Any]] = []
        for read in spec.reads:
            sanitized = self._sanitize(read.url)
            endpoints.append(
                {"name": read.name, "method": read.method, "url": sanitized or read.url}
            )
        return endpoints

    async def _emit(self, event: Mapping[str, Any]) -> None:
        if self._progress is not None:
            await self._progress(event)

    # --- small helpers -------------------------------------------------------------------------

    def _timezone(self, space: Space | None) -> ZoneInfo:
        name = space.time_zone if space is not None else DEFAULT_TIMEZONE
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo(DEFAULT_TIMEZONE)

    def _sanitize(self, url: str | None) -> str | None:
        if not url:
            return None
        try:
            return identity.normalize_url(url)
        except Exception:  # a weird read URL must not break the report
            return None


def _v4_kind(raw: str) -> str | None:
    value = raw.strip()
    if value in kinds_vocab.ALL_KINDS:
        return value
    if value in KIND_TO_V4:
        return KIND_TO_V4[value]
    return None


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    if stripped.startswith("{"):
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            return whole
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _short(value: object, limit: int = MAX_ERROR_DETAIL) -> str:
    text = str(value).replace("\n", " ")
    return text[:limit]


def _validation_detail(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "spec"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
