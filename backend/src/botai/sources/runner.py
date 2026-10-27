"""Runner: one sync of one connection, without a model in the hot path (FR-011, NFR-008).

Flow (docs/architecture/lld.md §4–§5):

1. load the connection and its active spec, mark it `syncing`;
2. fetch through the adapter with the user's session from the Vault;
3. a dead session (401/403, redirect to the login page) -> `services.sessions.ensure(...)` and
   exactly one more attempt; a dead token or link -> `needs_reauth` for the user to fix;
4. an answer that no longer matches the spec -> the previous snapshot stays, the connection becomes
   `repairing` and a `repair` job is queued;
5. otherwise the Normalizer upserts the snapshot and `next_sync_at` is set from `freshness`.

A transport failure re-raises after recording the state, so the job runner applies its backoff
(1, 5, 30 minutes) while the connection keeps showing the old data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.specs import Kind
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import Handler
from botai.core.http import RetryPolicy
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db import models as m
from botai.learn import revisions
from botai.sources import normalize, schedule
from botai.sources.adapter import (
    AdapterResult,
    ExpiredRules,
    NeedsUserInput,
    SessionExpired,
    SyncContext,
)
from botai.sources.factory import AdapterFactory
from botai.sources.session import load_session
from botai.sources.vocab import REQUIRED

log = logging.getLogger(__name__)

ADAPTERS_NEEDING_BROWSER_SESSION = {"recipe"}
REPAIRABLE_ADAPTERS = {"recipe"}
# Names of the profile fields a spec may refer to (`rowFilter.equalsParam`).
PROFILE_PARAMS = ("fullName", "email", "group", "studentId")


@dataclass(slots=True)
class ConnInfo:
    """What the sync and the learning jobs need to know about a connection, read once so that no
    transaction stays open while the network is slow."""

    connection_id: str
    user_id: str
    space_id: str
    origin: str
    kind: str
    title: str
    status: str
    adapter: str | None
    allow_hosts: tuple[str, ...]
    tz: ZoneInfo
    profile: dict[str, str]
    expired: ExpiredRules
    last_sync_at: datetime | None
    university_id: str | None


@dataclass(slots=True)
class Snapshot(ConnInfo):
    """A connection together with the spec it is synced by."""

    spec_id: str
    spec_revision: int
    spec: dict[str, Any]
    freshness: schedule.Freshness = field(init=False)

    def __post_init__(self) -> None:
        self.freshness = schedule.freshness_of(self.spec)


@dataclass(slots=True)
class Drift:
    endpoint_id: str
    kind: Kind | None
    problem: str

    def as_dict(self) -> dict[str, str]:
        return {"endpoint": self.endpoint_id, "kind": self.kind or "", "problem": self.problem}


@dataclass(slots=True)
class Assessment:
    drift: list[Drift] = field(default_factory=list)
    suspicious: set[Kind] = field(default_factory=set)  # never mark absent rows for these


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def _profile(raw: dict[str, Any]) -> dict[str, str]:
    values = {
        "fullName": raw.get("fullName") or raw.get("name"),
        "email": raw.get("email"),
        "group": raw.get("group"),
        "studentId": raw.get("studentId"),
    }
    return {k: str(v) for k, v in values.items() if isinstance(v, (str, int)) and str(v).strip()}


def _expired_rules(preset: dict[str, Any] | None) -> ExpiredRules:
    expired = (preset or {}).get("expired") or {}
    return ExpiredRules(
        redirect_path_prefixes=tuple(
            p for p in expired.get("redirectPathPrefix", []) if isinstance(p, str)
        ),
        probe401_paths=tuple(p for p in expired.get("probe401", []) if isinstance(p, str)),
    )


async def load_conn_info(session: AsyncSession, user_id: str, connection_id: str) -> ConnInfo:
    info, _ = await _load_conn(session, user_id, connection_id)
    return info


async def _load_conn(
    session: AsyncSession, user_id: str, connection_id: str
) -> tuple[ConnInfo, m.Connection]:
    conn = (
        await session.execute(
            select(m.Connection).where(
                m.Connection.id == connection_id, m.Connection.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
    space = (await session.execute(select(m.Space).where(m.Space.id == conn.space_id))).scalar_one()
    preset: dict[str, Any] | None = None
    if conn.login_preset_id:
        row = (
            await session.execute(
                select(m.LoginPresetRow).where(m.LoginPresetRow.id == conn.login_preset_id)
            )
        ).scalar_one_or_none()
        preset = row.preset if row else None
    declared: list[str] = list(conn.allow_hosts or [])
    host = _host(conn.origin)
    hosts: tuple[str, ...] = tuple(sorted({*declared, *([host] if host else [])}))
    info = ConnInfo(
        connection_id=conn.id,
        user_id=conn.user_id,
        space_id=conn.space_id,
        origin=conn.origin,
        kind=conn.kind,
        title=conn.title,
        status=conn.status,
        adapter=conn.adapter,
        allow_hosts=hosts,
        tz=_zone(space.time_zone),
        profile=_profile(space.profile or {}),
        expired=_expired_rules(preset),
        last_sync_at=conn.last_sync_at,
        university_id=space.university_id,
    )
    return info, conn


async def load_snapshot(session: AsyncSession, user_id: str, connection_id: str) -> Snapshot:
    info, conn = await _load_conn(session, user_id, connection_id)
    spec_row = await _spec_row(session, conn)
    if spec_row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "У подключения ещё нет спецификации")
    return _snapshot(info, spec_row)


def _snapshot(info: ConnInfo, spec_row: m.AdapterSpecRow) -> Snapshot:
    return Snapshot(
        connection_id=info.connection_id,
        user_id=info.user_id,
        space_id=info.space_id,
        origin=info.origin,
        kind=info.kind,
        title=info.title,
        status=info.status,
        adapter=spec_row.adapter,
        allow_hosts=info.allow_hosts,
        tz=info.tz,
        profile=info.profile,
        expired=info.expired,
        last_sync_at=info.last_sync_at,
        university_id=info.university_id,
        spec_id=spec_row.id,
        spec_revision=spec_row.revision,
        spec=spec_row.spec,
    )


def _host(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


async def _spec_row(session: AsyncSession, conn: m.Connection) -> m.AdapterSpecRow | None:
    """The connection's own spec, or the origin's active one (a second student of a university
    reuses what is already learned, FR-009)."""
    if conn.spec_id:
        row = (
            await session.execute(
                select(m.AdapterSpecRow).where(m.AdapterSpecRow.id == conn.spec_id)
            )
        ).scalar_one_or_none()
        if row is not None and row.state == "active":
            return row
    if conn.adapter:
        return (
            await session.execute(
                select(m.AdapterSpecRow).where(
                    m.AdapterSpecRow.origin == conn.origin,
                    m.AdapterSpecRow.adapter == conn.adapter,
                    m.AdapterSpecRow.state == "active",
                )
            )
        ).scalar_one_or_none()
    return None


def context_for(
    services: Services,
    info: ConnInfo,
    session: SessionState | None,
    *,
    spec_revision: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry: RetryPolicy | None = None,
    now: datetime | None = None,
) -> SyncContext:
    return SyncContext(
        user_id=info.user_id,
        connection_id=info.connection_id,
        origin=info.origin,
        allow_hosts=info.allow_hosts,
        now=now or services.clock.now(),
        vault=services.vault,
        tz=info.tz,
        session=session,
        profile=info.profile,
        proxy_url=services.settings.egress_proxy_url,
        transport=transport,
        retry=retry or RetryPolicy(),
        expired=info.expired,
        spec_revision=spec_revision,
    )


def build_context(
    services: Services,
    snap: Snapshot,
    session: SessionState | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry: RetryPolicy | None = None,
    now: datetime | None = None,
) -> SyncContext:
    return context_for(
        services,
        snap,
        session,
        spec_revision=snap.spec_revision,
        transport=transport,
        retry=retry,
        now=now,
    )


async def known_document_hash(session: AsyncSession, connection_id: str) -> str | None:
    """Hash of the document stored for a connection: the same file is not processed again."""
    return (
        await session.execute(
            select(m.Document.hash)
            .where(m.Document.connection_id == connection_id)
            .order_by(m.Document.fetched_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# --- assessment -----------------------------------------------------------------------------


async def baseline_counts(
    session: AsyncSession, connection_id: str, last_sync_at: datetime | None
) -> dict[Kind, int]:
    """Rows of the previous successful snapshot, per kind."""
    if last_sync_at is None:
        return {}
    counts: dict[Kind, int] = {}
    for kind, model in normalize.MODELS.items():
        n = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.connection_id == connection_id, model.seen_at >= last_sync_at)
            )
        ).scalar_one()
        if n:
            counts[kind] = int(n)
    return counts


def assess(spec: dict[str, Any], result: AdapterResult, baseline: dict[Kind, int]) -> Assessment:
    """Decide whether the answer still matches the spec.

    * a structural problem (path gone, wrong format, no field resolves, bootstrap failed) is drift;
    * an endpoint that found nothing while the previous snapshot had data is suspicious: it may be
      a changed markup, so absent rows are not marked and a repair looks at it;
    * nothing found and nothing before is simply an empty student, not drift.
    """
    out = Assessment()
    for o in result.outcomes:
        if o.structural:
            out.drift.append(Drift(o.endpoint_id, o.kind, o.problem or "format"))
            if o.kind:
                out.suspicious.add(o.kind)
        elif o.problem == "empty" and o.kind and baseline.get(o.kind, 0) > 0:
            out.drift.append(Drift(o.endpoint_id, o.kind, "empty"))
            out.suspicious.add(o.kind)
    if spec.get("adapter") == "recipe":
        validation = spec.get("recipe", {}).get("validation", {})
        minimum = int(validation.get("minItems", 1))
        for kind in validation.get("requireKinds", []):
            if (
                kind in REQUIRED
                and result.count(kind) < minimum
                and baseline.get(kind, 0) >= minimum
            ):
                if kind not in out.suspicious:
                    out.drift.append(Drift("validation", kind, "empty"))
                out.suspicious.add(kind)
    return out


# --- state writes ---------------------------------------------------------------------------


async def set_connection(
    services: Services,
    connection_id: str,
    user_id: str,
    *,
    status: str | None = None,
    error: tuple[str, str] | None = None,
    clear_error: bool = False,
    **values: Any,
) -> None:
    """One short transaction on the connection row; a disabled connection is never touched."""
    cols: dict[str, Any] = dict(values)
    now = services.clock.now()
    if status is not None:
        cols["status"] = status
    if error is not None:
        cols.update(error_code=error[0], error_detail=error[1][:500], error_at=now)
    if clear_error:
        cols.update(error_code=None, error_detail=None, error_at=None)
    async with services.db() as db, db.begin():
        await db.execute(
            update(m.Connection)
            .where(
                m.Connection.id == connection_id,
                m.Connection.user_id == user_id,
                m.Connection.status != "disabled",
            )
            .values(**cols)
        )


# --- the sync itself ------------------------------------------------------------------------


@dataclass(slots=True)
class SyncOutcome:
    status: str
    kinds: dict[str, dict[str, int]] = field(default_factory=dict)
    drift: list[Drift] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "kinds": self.kinds,
            "drift": [d.as_dict() for d in self.drift],
            "detail": self.detail,
        }


async def fetch_with_reauth(
    ctx: JobContext,
    services: Services,
    snap: Snapshot,
    sctx: SyncContext,
) -> AdapterResult | SyncOutcome:
    """Fetch; on a dead session log in again once. Returns an outcome when the user must act."""
    adapter = AdapterFactory().create(snap.spec)
    reauthed = False
    while True:
        try:
            if (
                snap.adapter in ADAPTERS_NEEDING_BROWSER_SESSION
                and sctx.session is None
                and snap.spec.get("recipe", {}).get("auth", {}).get("type", "cookie") != "none"
            ):
                raise SessionExpired("no session stored")
            return await adapter.fetch(sctx)
        except SessionExpired as exc:
            if exc.reauth == "token" or reauthed:
                code = ErrorCode.SOURCE_AUTH_REQUIRED
                await set_connection(
                    services,
                    snap.connection_id,
                    snap.user_id,
                    status="needs_reauth",
                    error=(code.value, "Нужно войти заново или дать новую ссылку или токен"),
                    next_sync_at=None,
                )
                return SyncOutcome("needs_reauth", detail=code.value)
            reauthed = True
            await set_connection(services, snap.connection_id, snap.user_id, status="needs_reauth")
            await ctx.status("Вхожу заново")
            try:
                sctx.session = await services.sessions.ensure(
                    ctx, user_id=snap.user_id, connection_id=snap.connection_id
                )
            except BotaiError as err:
                if err.code not in {ErrorCode.LOGIN_FAILED, ErrorCode.SOURCE_BOT_PROTECTION}:
                    raise
                await set_connection(
                    services,
                    snap.connection_id,
                    snap.user_id,
                    status="needs_reauth",
                    error=(err.code.value, err.detail or "Не удалось войти"),
                    next_sync_at=None,
                )
                return SyncOutcome("needs_reauth", detail=err.code.value)
            await set_connection(services, snap.connection_id, snap.user_id, status="syncing")


async def sync_connection(
    ctx: JobContext,
    services: Services,
    connection_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry: RetryPolicy | None = None,
    accept_empty: bool = False,
    queue_repair: bool = True,
) -> SyncOutcome:
    """One sync. `accept_empty` takes an empty answer as the truth (a repair concluded that the
    source really has nothing); `queue_repair=False` is for the repair job itself, which handles
    drift and the health of the revision on its own."""
    user_id = ctx.user_id
    async with services.db() as db:
        snap = await load_snapshot(db, user_id, connection_id)
    if snap.status in {"disabled", "learning", "draft", "repairing"}:
        return SyncOutcome("skipped", detail=snap.status)
    if snap.adapter == "document" and snap.spec.get("source", {}).get("kind") == "upload":
        return SyncOutcome("skipped", detail="upload")  # nothing to fetch again: no source address
    previous_status = snap.status
    await set_connection(services, connection_id, user_id, status="syncing")
    await ctx.status("Обновляю данные")
    try:
        sctx = build_context(
            services,
            snap,
            await load_session(services.vault, user_id, connection_id),
            transport=transport,
            retry=retry,
        )
        if snap.adapter == "document":
            async with services.db() as db:
                sctx.known_hash = await known_document_hash(db, connection_id)
        fetched = await fetch_with_reauth(ctx, services, snap, sctx)
        if isinstance(fetched, SyncOutcome):
            return fetched
        return await commit_result(
            services, snap, fetched, sctx.now, accept_empty=accept_empty, queue_repair=queue_repair
        )
    except BotaiError as err:
        return await _record_failure(services, snap, err)
    except NeedsUserInput as need:
        await set_connection(
            services,
            connection_id,
            user_id,
            status="active" if previous_status == "syncing" else previous_status,
            error=("validation", f"Нужно значение профиля: {need.param}"),
            next_sync_at=None,
        )
        return SyncOutcome("needs_input", detail=need.param)
    except BaseException:
        await set_connection(services, connection_id, user_id, status=previous_status)
        raise


async def _record_failure(services: Services, snap: Snapshot, err: BotaiError) -> SyncOutcome:
    now = services.clock.now()
    transient = err.code in {ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.SOURCE_RATE_LIMITED}
    if transient:
        retry_in = (
            schedule.RATE_LIMIT_RETRY
            if err.code == ErrorCode.SOURCE_RATE_LIMITED
            else schedule.FAILURE_RETRY
        )
        stale = snap.last_sync_at is None or schedule.is_stale(
            now, snap.last_sync_at, snap.freshness
        )
        await set_connection(
            services,
            snap.connection_id,
            snap.user_id,
            status="stale" if stale else "active",
            error=(err.code.value, err.detail),
            next_sync_at=now + retry_in,
        )
        raise err  # the job runner backs off 1, 5, 30 minutes
    await set_connection(
        services,
        snap.connection_id,
        snap.user_id,
        status="error",
        error=(err.code.value, err.detail),
        next_sync_at=None,
    )
    return SyncOutcome("error", detail=err.code.value)


async def commit_result(
    services: Services,
    snap: Snapshot,
    result: AdapterResult,
    now: datetime,
    *,
    accept_empty: bool,
    queue_repair: bool,
) -> SyncOutcome:
    async with services.db() as db, db.begin():
        await normalize.lock_connection(db, snap.connection_id)
        current = (
            await db.execute(
                select(m.Connection).where(m.Connection.id == snap.connection_id).with_for_update()
            )
        ).scalar_one_or_none()
        if current is None or current.status == "disabled":
            return SyncOutcome("skipped", detail="disabled")
        baseline = (
            {}
            if accept_empty
            else await baseline_counts(db, snap.connection_id, current.last_sync_at)
        )
        verdict = assess(snap.spec, result, baseline)
        spec_row = await db.get(m.AdapterSpecRow, snap.spec_id) if queue_repair else None
        stats = await normalize.apply_result(
            db,
            user_id=snap.user_id,
            connection_id=snap.connection_id,
            result=result,
            now=now,
            tz=snap.tz,
            mark_missing=result.complete_kinds() - verdict.suspicious,
        )
        kinds = {k: v.as_dict() for k, v in stats.items()}
        for doc in result.documents:
            await normalize.store_document(
                db,
                user_id=snap.user_id,
                space_id=snap.space_id,
                connection_id=snap.connection_id,
                doc=doc,
                now=now,
            )
        if result.unchanged:  # the same file again: still current as of now
            await db.execute(
                update(m.Document)
                .where(m.Document.connection_id == snap.connection_id)
                .values(fetched_at=now)
            )
        if verdict.drift:
            repairable = snap.adapter in REPAIRABLE_ADAPTERS
            current.status = "repairing" if repairable else "stale"
            current.error_code = ErrorCode.RECIPE_DRIFT.value
            current.error_detail = "Источник отвечает иначе, чем раньше"
            current.error_at = now
            current.next_sync_at = None if repairable else now + schedule.FAILURE_RETRY
            outcome = SyncOutcome("repairing" if repairable else "stale", kinds, verdict.drift)
            # three drifts in a row retire the revision in favour of the previous one
            if (
                spec_row is not None
                and spec_row.state == "active"
                and await revisions.register_drift(db, spec_row, now, user_id=snap.user_id)
            ):
                outcome.detail = "rolled_back"
        else:
            current.status = "active"
            current.last_sync_at = now
            current.next_sync_at = schedule.next_sync_at(now, snap.freshness, snap.connection_id)
            current.error_code = current.error_detail = current.error_at = None
            current.spec_id, current.spec_revision = snap.spec_id, snap.spec_revision
            outcome = SyncOutcome("ok", kinds)
            if spec_row is not None and spec_row.state == "active" and snap.adapter == "recipe":
                await revisions.register_ok(db, spec_row, now, user_id=snap.user_id)
    if outcome.status == "repairing" and queue_repair:
        await services.queue.enqueue(
            JobKind.REPAIR,
            user_id=snap.user_id,
            connection_id=snap.connection_id,
            payload={"reason": "drift", "problems": [d.as_dict() for d in verdict.drift]},
        )
    return outcome


# --- job handler ----------------------------------------------------------------------------


def make_sync_handler(
    *, transport: httpx.AsyncBaseTransport | None = None, retry: RetryPolicy | None = None
) -> Handler:
    """The `JobKind.SYNC` handler. `transport` and `retry` exist for tests."""

    async def handle(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        connection_id = ctx.connection_id or str(payload.get("connection_id", ""))
        if not connection_id:
            raise BotaiError(ErrorCode.VALIDATION, "У задания синхронизации нет подключения")
        outcome = await sync_connection(
            ctx, services, connection_id, transport=transport, retry=retry
        )
        await _schedule_canaries(services)
        return outcome.as_dict()

    return handle


async def _schedule_canaries(services: Services) -> None:
    """The daily canary needs a periodic caller; every sync is one. Never breaks the sync."""
    try:
        await revisions.enqueue_due_canaries(services.db, services.queue, services.clock.now())
    except Exception:
        log.warning("scheduling canary checks failed", exc_info=True)
