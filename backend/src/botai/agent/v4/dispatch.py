"""V4 engine dispatch of the source jobs: CONNECT, SYNC and REPAIR (task-19).

With `BOTAI_AGENT_ENGINE=v4` the product engine stops being "learn/" for source jobs:

* `CONNECT` creates the v4 `Source` of the connection's space first (so the record exists even if
  the agent fails), then runs the v4 runtime with the full harness: browser + secrets to log in,
  `source_*` to research, write/activate a playbook and sync. The answer is posted to the thread as
  a text block with source cards; a successful connect asks the queue for a follow-up sync.
* `SYNC` is the deterministic fast path: the saved playbook is replayed by `SourceRunner` without a
  model. Drift and auth failures are reported honestly; drift asks for a `REPAIR` follow-up, while
  transient source errors keep the queue's retry semantics (`source_unavailable`).
* `REPAIR` first replays the active playbook deterministically (`SourceRunner.repair` + a sync to
  record the result); only if that does not fix it does the v4 agent take over (`source_*` tools to
  study the live source, write a new version and activate it after a successful canary).

Compatibility bridges, both marked and deliberate:

* a `CONNECT` of a calendar-by-link connection stays on the v3 path: the link flow (protected input,
  link validation, ical adapter) is a v3 UI contract; it becomes an ical-playbook v4 source in the
  next round;
* a source job of a connection that has no v4 `Source` yet keeps the v3 handler: old connections are
  not silently reconnected, they are migrated by their next connect.

The connection row stays the task's durable handle (the job payload points at it); the v4 source is
linked through `Source.config["connection_id"]`. Every status change is written to both records, so
the old space screen and the new sources screen agree on freshness.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import AnyUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.factory import build_source_toolkit
from botai.agent.v4.handlers import SYSTEM_PROMPT
from botai.agent.v4.job_runtime import (
    MAX_ANSWER_CHARS,
    error_of,
    run_result_dict,
    run_v4_job,
    stage_of,
)
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.tools.sources import SourceToolkit
from botai.contracts.agent_ui import Source as SourceCard
from botai.contracts.agent_ui import TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import HandlerTable
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import Connection, Source, Space
from botai.sources import schedule
from botai.sources.v4 import identity, playbooks, service, store
from botai.sources.v4 import kinds as kinds_vocab

log = logging.getLogger("botai.agent.v4.dispatch")

CONNECTION_KEY = "connection_id"
CONNECT_PREFIX = "Подключение"
REPAIR_PREFIX = "Починка"
SYNC_PREFIX = "Обновление"
CONNECT_STAGE = "Подключение: собираю контекст"

CONNECT_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + """

Сейчас твоя задача — подключение источника, а не свободный чат. Порядок работы:
1. Вход. Если кабинет требует входа, управляй браузером; пароль/код запрашивай только
   через secret_request и вводи через secret_use (browser_field). Сессию (cookie) можно
   переиспользовать.
2. Исследование. Найди, где лежат данные: страницы, XHR/fetch JSON, API. Читай
   source_read_live/http_request/browser_network; не полагайся на один DOM-каркас.
3. Playbook. Опиши чтение данными: декларативный spec (reads/fields) или код-парсер
   (python/js), сохрани через source_update_playbook с activate=true (canary обязателен) и
   проверь source_probe перед активацией, если сомневаешься.
4. Синхронизация. Выполни source_sync и убедись по отчёту, что записи прочитаны.
5. Если источник уже создан (source_id дан ниже) — не создавай дубликат, работай с ним.
Не выдумывай данные: если доступ не получен, скажи честно, где и на чём остановился.
В конце перечисли в ответе: что за источник, какие виды данных читаются, где лежат
(endpoint/URL), что осталось сделать."""
)


async def handle_connect(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    provider: Provider | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`JobKind.CONNECT` on the v4 engine: research + playbook + first sync by the agent."""
    _require_v4(services)
    connection_id = _connection_id(ctx, payload)
    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        space = await session.get(Space, connection.space_id)
        if space is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Пространство подключения не найдено")
        if payload.get("calendar") is True or connection.adapter == "ical":
            await session.rollback()
            log.info("connect %s: calendar-by-link stays on the v3 path this round", connection.id)
            return await _v3_connect(ctx, services, payload)
        if connection.status == "disabled":
            raise BotaiError(ErrorCode.CONFLICT, "Подключение выключено: включите источник")
        await _mark_source_status(session, ctx.user_id, connection, "learning")
        source = await _ensure_source(session, ctx.user_id, connection, payload)
        source_id, space_id = source.id, space.id
        task = _connect_task(connection, space, source, payload)
        await session.commit()

    await ctx.status(CONNECT_STAGE)
    try:
        run = await run_v4_job(
            ctx,
            services,
            task=task,
            system=CONNECT_SYSTEM_PROMPT,
            provider=provider,
            run_id=_payload_run_id(payload),
            workspace_root=_optional_text(payload.get("_workspace_root")),
            with_browser=payload.get("_browser", True) is not False,
            http_transport=http_transport,
            artifact_root=_artifact_root(payload, artifact_root),
            source_cookies=source_cookies,
            phase_prefix=CONNECT_PREFIX,
            metadata={"kind": "connect", "connection_id": connection_id},
        )
    except BotaiError as exc:
        await _record_failure(services, ctx.user_id, source_id, exc)
        raise
    await ctx.status(stage_of(run.result, prefix=CONNECT_PREFIX))

    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        source = await service.get_source(session, user_id=ctx.user_id, source_id=source_id)
        outcome = _source_outcome(result_status=run.result.status, source=source)
        status = "connected" if outcome == "active" else outcome
        has_playbook = source.playbook_id is not None
        if outcome is not None:
            await _sync_connection_view(
                session,
                connection,
                source,
                status=outcome,
                now=services.clock.now(),
                next_sync_at=None,
            )
        cards = await _source_cards(session, ctx.user_id, space_id, connection)
        await session.commit()

    if run.result.content:
        await ctx.emit(
            TextBlock(
                id=new_id(),
                at=services.clock.now(),
                markdown=run.result.content[:MAX_ANSWER_CHARS],
                sources=cards or None,
            )
        )
    if run.result.status == "error":
        raise error_of(run.result)

    body = run_result_dict(
        run,
        extra={
            "connection_id": connection_id,
            "source_id": source_id,
            "space_id": space_id,
            "status": status or run.result.status,
        },
    )
    if run.result.status == "answer" and has_playbook:
        # The connect has a playbook: let the deterministic runner record the first sync after this
        # job is terminal (the queue dedupe refuses a same-connection job while this one is running).
        body["_next_job"] = {
            "kind": JobKind.SYNC.value,
            "connection_id": connection_id,
            "payload": {"source_id": source_id},
        }
    return body


async def handle_sync(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`JobKind.SYNC` on the v4 engine: replay the saved playbook without a model."""
    _require_v4(services)
    connection_id = _connection_id(ctx, payload)
    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        source = await _find_source(session, ctx.user_id, connection)
        if source is None:
            await session.rollback()
            log.info("sync %s: no v4 source yet, keeping the v3 handler", connection.id)
            return await _v3_sync(ctx, services, payload)
        source_id = source.id

    await ctx.status(f"{SYNC_PREFIX}: читаю источник")
    toolkit = _toolkit(services, ctx, http_transport, artifact_root, source_cookies)
    async with services.db() as session:
        report = await toolkit.runner(session, ctx.user_id).sync(source_id)
    await ctx.status(f"{SYNC_PREFIX}: {_report_stage(report)}")

    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        source = await service.get_source(session, user_id=ctx.user_id, source_id=source_id)
        now = services.clock.now()
        status = "ok" if report.ok else ("needs_reauth" if _auth_error(report) else "error")
        await _sync_connection_view(
            session,
            connection,
            source,
            status=status,
            now=now,
            next_sync_at=_next_sync_at(now, connection, report.ok),
        )
        await session.commit()

    if not report.ok and _only_retryable(report):
        # The queue retries source_unavailable/rate-limited with backoff, as for v3 sync.
        detail = report.errors[0].detail if report.errors else "Источник недоступен"
        raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, detail)

    body: dict[str, Any] = {
        "status": status,
        "source_id": source_id,
        "run": report.run,
        "counts": report.counts,
        "reads": len(report.reads),
        "artifact_ids": report.artifact_ids[:20],
        "errors": [error.model_dump(mode="json") for error in report.errors],
        "detail": "",
    }
    if not report.ok and _drift_error(report):
        # Repair is queued after this job is terminal so the dedupe sees a finished sync.
        body["_next_job"] = {
            "kind": JobKind.REPAIR.value,
            "connection_id": connection_id,
            "payload": {
                "reason": "drift",
                "source_id": source_id,
                "problems": [error.model_dump(mode="json") for error in report.errors[:10]],
            },
        }
    return body


async def handle_repair(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    provider: Provider | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`JobKind.REPAIR` on the v4 engine: deterministic replay first, agent fallback after it."""
    _require_v4(services)
    connection_id = _connection_id(ctx, payload)
    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        source = await _find_source(session, ctx.user_id, connection)
        if source is None:
            await session.rollback()
            log.info("repair %s: no v4 source yet, keeping the v3 handler", connection.id)
            return await _v3_repair(ctx, services, payload)
        source_id = source.id
        await _mark_source_status(session, ctx.user_id, connection, "learning")
        await session.commit()

    toolkit = _toolkit(services, ctx, http_transport, artifact_root, source_cookies)
    fast = await _repair_fast_path(toolkit, services, ctx.user_id, source_id)
    if fast is not None and fast.ok:
        async with services.db() as session:
            connection = await _owned_connection(session, ctx.user_id, connection_id)
            source = await service.get_source(session, user_id=ctx.user_id, source_id=source_id)
            await _sync_connection_view(
                session,
                connection,
                source,
                status="ok",
                now=services.clock.now(),
                next_sync_at=_next_sync_at(services.clock.now(), connection, True),
            )
            await session.commit()
        return {
            "status": "ok",
            "source_id": source_id,
            "run": "repair",
            "counts": fast.counts,
            "reads": len(fast.reads),
            "errors": [],
            "detail": "deterministic replay",
        }

    await ctx.status(f"{REPAIR_PREFIX}: детерминированная проверка не помогла, работает агент")
    try:
        run = await run_v4_job(
            ctx,
            services,
            task=_repair_task(connection_id, source_id, payload, fast),
            system=CONNECT_SYSTEM_PROMPT,
            provider=provider,
            run_id=_payload_run_id(payload),
            workspace_root=_optional_text(payload.get("_workspace_root")),
            with_browser=payload.get("_browser", True) is not False,
            http_transport=http_transport,
            artifact_root=_artifact_root(payload, artifact_root),
            source_cookies=source_cookies,
            phase_prefix=REPAIR_PREFIX,
            metadata={"kind": "repair", "connection_id": connection_id},
        )
    except BotaiError as exc:
        await _record_failure(services, ctx.user_id, source_id, exc)
        raise
    await ctx.status(stage_of(run.result, prefix=REPAIR_PREFIX))

    async with services.db() as session:
        connection = await _owned_connection(session, ctx.user_id, connection_id)
        source = await service.get_source(session, user_id=ctx.user_id, source_id=source_id)
        status = _source_outcome(result_status=run.result.status, source=source)
        has_playbook = source.playbook_id is not None
        if status is not None:
            await _sync_connection_view(
                session,
                connection,
                source,
                status=status,
                now=services.clock.now(),
                next_sync_at=_next_sync_at(services.clock.now(), connection, status == "ok"),
            )
        cards = await _source_cards(session, ctx.user_id, connection.space_id, connection)
        await session.commit()

    if run.result.content:
        await ctx.emit(
            TextBlock(
                id=new_id(),
                at=services.clock.now(),
                markdown=run.result.content[:MAX_ANSWER_CHARS],
                sources=cards or None,
            )
        )
    if run.result.status == "error":
        raise error_of(run.result)

    body = run_result_dict(
        run,
        extra={
            "connection_id": connection_id,
            "source_id": source_id,
            "status": status or run.result.status,
        },
    )
    if has_playbook and status in ("active", "ok"):
        body["_next_job"] = {
            "kind": JobKind.SYNC.value,
            "connection_id": connection_id,
            "payload": {"source_id": source_id},
        }
    return body


def make_v4_handlers(
    *,
    provider: Provider | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
) -> HandlerTable:
    """CONNECT/SYNC/REPAIR handlers of the v4 engine; tests inject provider/transport/root."""

    async def connect(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await handle_connect(
            ctx,
            services,
            payload,
            provider=provider,
            http_transport=http_transport,
            artifact_root=str(artifact_root) if artifact_root is not None else None,
            source_cookies=source_cookies,
        )

    async def sync(ctx: JobContext, services: Services, payload: dict[str, Any]) -> dict[str, Any]:
        return await handle_sync(
            ctx,
            services,
            payload,
            http_transport=http_transport,
            artifact_root=str(artifact_root) if artifact_root is not None else None,
            source_cookies=source_cookies,
        )

    async def repair(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await handle_repair(
            ctx,
            services,
            payload,
            provider=provider,
            http_transport=http_transport,
            artifact_root=str(artifact_root) if artifact_root is not None else None,
            source_cookies=source_cookies,
        )

    return {JobKind.CONNECT: connect, JobKind.SYNC: sync, JobKind.REPAIR: repair}


# --- deterministic repair ---------------------------------------------------------------------


async def _repair_fast_path(
    toolkit: SourceToolkit,
    services: Services,
    user_id: str,
    source_id: str,
) -> Any:
    """Replay the active playbook without a model: a working version needs no agent at all.

    `SourceRunner.repair` only records a candidate run; it does not claim the source is fixed, so
    after a successful replay we run the normal sync to write status/artifacts honestly. Returns the
    sync report, or None when there is nothing to replay (no playbook/active version).
    """
    async with services.db() as session:
        source = await service.get_source(session, user_id=user_id, source_id=source_id)
        if source.playbook_id is None:
            return None
        version = await playbooks.active_playbook_version(
            session, user_id=user_id, playbook_id=source.playbook_id
        )
        if version is None:
            return None
        runner = toolkit.runner(session, user_id)
        replay = await runner.repair(source_id, version=version.version)
        if not replay.ok:
            return replay
    async with services.db() as session:
        return await toolkit.runner(session, user_id).sync(source_id, persist=True)


# --- source <-> connection mapping ------------------------------------------------------------


def _source_url(connection: Connection) -> str | None:
    """The address a source is identified by: the UI page, else the origin of the connection."""
    if connection.entry_url:
        return connection.entry_url
    if connection.origin:
        return f"{connection.origin.rstrip('/')}/"
    return None


def _v4_kinds(connection: Connection, payload: dict[str, Any]) -> list[str]:
    """The v4 kinds of a connection and of the current connect intent (legacy values translated)."""
    raw: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value:
            return
        if value in kinds_vocab.ALL_KINDS:
            raw.append(value)
        elif value in kinds_vocab.LEGACY_KIND_ALIASES:
            raw.append(kinds_vocab.LEGACY_KIND_ALIASES[value])

    add(connection.kind)
    kinds = payload.get("kinds")
    if isinstance(kinds, list):
        for kind in kinds:
            add(kind)
    intent = payload.get("learning_intent")
    if isinstance(intent, dict):
        requested = intent.get("requested_kinds")
        if isinstance(requested, list):
            for kind in requested:
                add(kind)
        absent = intent.get("absent_kinds")
        if isinstance(absent, list):
            for kind in absent:
                add(kind)
    try:
        resolved = kinds_vocab.normalize_kinds(raw)
    except BotaiError:
        resolved = []
    if not any(kind in kinds_vocab.DATA_KINDS for kind in resolved):
        resolved.extend(["schedule", "deadlines"])
    return kinds_vocab.normalize_kinds(resolved)


async def _find_source(
    session: AsyncSession, user_id: str, connection: Connection
) -> Source | None:
    """The v4 source of a connection: the explicit link first, then the normalized address."""
    linked = await session.scalar(
        select(Source)
        .where(
            Source.user_id == user_id,
            Source.space_id == connection.space_id,
            Source.config[CONNECTION_KEY].as_string() == connection.id,
        )
        .order_by(Source.created_at, Source.id)
        .limit(1)
    )
    if linked is not None:
        return linked
    url = _source_url(connection)
    if url is None:
        return None
    try:
        key = identity.normalize_url(url)
    except BotaiError:
        return None
    return await store.source_by_identity(session, connection.space_id, key)


async def _ensure_source(
    session: AsyncSession, user_id: str, connection: Connection, payload: dict[str, Any]
) -> Source:
    """The v4 source of this connection, created in the connection's space when absent.

    The source exists before the agent starts: a failed connect still leaves a draft record the
    owner can see and retry, and the agent gets `source_id`/`space_id` in the task instead of
    guessing where to create it.
    """
    existing = await _find_source(session, user_id, connection)
    if existing is not None:
        if existing.config.get(CONNECTION_KEY) != connection.id:
            existing.config = {**existing.config, CONNECTION_KEY: connection.id}
            await session.flush()
        return existing
    url = _source_url(connection)
    try:
        return await service.create_source(
            session,
            user_id=user_id,
            space_id=connection.space_id,
            title=connection.title,
            entry_url=url,
            kinds=_v4_kinds(connection, payload),
            status="draft",
            config={CONNECTION_KEY: connection.id, "origin": connection.origin},
        )
    except BotaiError as exc:
        if exc.code is not ErrorCode.CONFLICT:
            raise
        # A source of the same address already exists (created by the agent of an earlier attempt):
        # reuse it instead of failing the whole connect on a duplicate.
        again = await _find_source(session, user_id, connection)
        if again is None:
            raise
        again.config = {**again.config, CONNECTION_KEY: connection.id}
        await session.flush()
        return again


async def _mark_source_status(
    session: AsyncSession, user_id: str, connection: Connection, status: str
) -> None:
    """Best effort at the start of a connect/repair: the source shows what is happening."""
    source = await _find_source(session, user_id, connection)
    if source is None or source.status in ("disabled", "archived"):
        return
    try:
        await service.set_status(session, user_id=user_id, source_id=source.id, status=status)
    except BotaiError:
        log.warning("could not mark source %s as %s", source.id, status)


async def _record_failure(
    services: Services, user_id: str, source_id: str, exc: BotaiError
) -> None:
    """Leave an honest error on the source/connection when the run could not even start."""
    now = services.clock.now()
    async with services.db() as session:
        try:
            connection = await session.scalar(
                select(Connection)
                .join(Source, Source.space_id == Connection.space_id)
                .where(Source.id == source_id, Connection.user_id == user_id)
                .limit(1)
            )
            await service.set_status(
                session,
                user_id=user_id,
                source_id=source_id,
                status="error",
                last_error={
                    "code": exc.code.value,
                    "detail": exc.detail[:2000],
                    "at": now.isoformat(),
                },
            )
            if connection is not None:
                connection.status = "error"
                connection.error_code = exc.code.value
                connection.error_detail = exc.detail[:2000]
            await session.commit()
        except BotaiError:
            await session.rollback()
            log.warning("could not record the failure of source %s", source_id)


async def _source_cards(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    connection: Connection,
) -> list[SourceCard]:
    """Cards of the space's sources for the connect answer (title, address, freshness)."""
    rows = await service.list_sources(session, user_id=user_id, space_id=space_id)
    cards: list[SourceCard] = []
    now = connection.created_at
    for row in rows[:10]:
        url = row.entry_url or _source_url(connection)
        if not url:
            continue
        cards.append(
            SourceCard(
                title=row.title[:200],
                url=AnyUrl(url[:2000]),
                fetched_at=row.last_sync_at or now,
                stale=row.status in ("stale", "error", "needs_reauth"),
            )
        )
    return cards


def _source_outcome(*, result_status: str, source: Source) -> str | None:
    """The connection status of the finished run, honestly derived from the source record.

    `None` means "leave the connection as it was" (a paused run or a limit is not a verdict about
    the source). A run that answered without producing a playbook did not connect anything: the
    source stays a draft with an explicit reason, it is never reported as connected.
    """
    if result_status == "error":
        return "error"
    if result_status in (
        "paused",
        "cancelled",
        "step_limit",
        "time_limit",
        "token_limit",
        "cost_limit",
        "hard_stop",
    ):
        return None
    if source.status in ("disabled", "archived"):
        return None
    if source.status in ("active", "stale"):
        return "active"
    if source.status == "needs_reauth":
        return "needs_reauth"
    if source.status == "error":
        return "error"
    return "learning"


async def _sync_connection_view(
    session: AsyncSession,
    connection: Connection,
    source: Source | None,
    *,
    status: str,
    now: datetime,
    next_sync_at: datetime | None,
) -> None:
    """Keep the v3 `connections` row coherent with the v4 source (old screen + scheduler)."""
    connection.status = {
        "ok": "active",
        "active": "active",
        "connected": "active",
        "needs_reauth": "needs_reauth",
        "error": "error",
    }.get(status, status if status in ("active", "learning", "draft") else "error")
    if source is not None:
        connection.last_sync_at = source.last_sync_at or connection.last_sync_at
        error = source.last_error or {}
        connection.error_code = str(error["code"]) if isinstance(error.get("code"), str) else None
        connection.error_detail = (
            str(error["detail"])[:2000] if isinstance(error.get("detail"), str) else None
        )
    connection.next_sync_at = next_sync_at
    await session.flush()


def _next_sync_at(now: datetime, connection: Connection, ok: bool) -> datetime | None:
    """The scheduler's next tick for a v4 source: the v3 default cadence, shorter after a failure."""
    if not ok:
        return now + schedule.FAILURE_RETRY
    freshness = schedule.freshness_of({})
    return schedule.next_sync_at(now, freshness, connection.id)


# --- prompts and small helpers -----------------------------------------------------------------


def _connect_task(
    connection: Connection, space: Space, source: Source, payload: dict[str, Any]
) -> str:
    url = _source_url(connection)
    kinds = _v4_kinds(connection, payload)
    lines = [
        f"Подключи источник «{connection.title}» в пространстве «{space.title}».",
        f"connection_id: {connection.id}",
        f"Уже созданный v4-источник: source_id={source.id}, space_id={space.id}"
        " (работай с ним, дубликат не создавай).",
        f"Адрес входа: {url or 'не задан — найди его сам по названию/кабинету'}",
        f"Нужные виды данных: {', '.join(kinds)}.",
    ]
    intent = payload.get("learning_intent")
    if isinstance(intent, dict):
        note = intent.get("user_note")
        if isinstance(note, str) and note.strip():
            lines.append(f"Пожелание владельца: {note.strip()[:2000]}")
        absent = intent.get("absent_kinds")
        if isinstance(absent, list) and absent:
            lines.append(f"Этих данных владелец не хочет: {', '.join(map(str, absent[:20]))}")
        if intent.get("discovery_mode") == "requested":
            lines.append("Режим: читать только запрошенные виды, остальное не добавлять.")
    if payload.get("manual_login") is True:
        lines.append("Это повторный вход в уже подключённый источник: обнови сессию.")
    note = payload.get("note")
    if isinstance(note, str) and note.strip() and note != (intent or {}).get("user_note"):
        lines.append(f"Заметка: {note.strip()[:2000]}")
    lines.append(
        "План: вход (если нужен) → исследование данных → playbook с canary и активацией → "
        "source_sync. В конце ответь, что подключено и где лежат данные."
    )
    return "\n".join(lines)


def _repair_task(
    connection_id: str,
    source_id: str,
    payload: dict[str, Any],
    fast: Any,
) -> str:
    lines = [
        f"Почини источник source_id={source_id} (connection_id: {connection_id}).",
        "Активный playbook не прочитал данные при детерминированном повторе.",
    ]
    problems = payload.get("problems")
    if isinstance(problems, list) and problems:
        rendered = []
        for item in problems[:10]:
            if isinstance(item, dict):
                code = item.get("code") or "error"
                detail = str(item.get("detail") or "")[:300]
                rendered.append(f"- {code}: {detail}")
            else:
                rendered.append(f"- {str(item)[:300]}")
        lines.append("Ошибки прошлого прогона:")
        lines.extend(rendered)
    if fast is not None and fast.errors:
        lines.append(
            "Ошибки детерминированного повтора: "
            + "; ".join(f"{error.code}: {error.detail[:200]}" for error in fast.errors[:5])
        )
    lines.append(
        "Порядок: source_read_live/http_request/browser_* — изучи, что изменилось; "
        "source_update_playbook с исправленным spec/code и activate=true (только после успешного "
        "canary); source_sync — убедись по отчёту, что записи читаются. Если вход потерян — "
        "запроси secret_request и войди заново. Не выдумывай данные: если починить не удалось, "
        "скажи честно, что именно сломано."
    )
    return "\n".join(lines)


def _toolkit(
    services: Services,
    ctx: JobContext,
    http_transport: httpx.AsyncBaseTransport | None,
    artifact_root: str | Path | None,
    cookies: Mapping[str, str] | None,
) -> SourceToolkit:
    return build_source_toolkit(
        services,
        user_id=ctx.user_id,
        artifact_root=artifact_root,
        transport=http_transport,
        cookies=cookies,
    )


def _require_v4(services: Services) -> None:
    if services.settings.agent_engine != "v4":
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Задачи источников на v4 выключены: включите BOTAI_AGENT_ENGINE=v4.",
        )


def _connection_id(ctx: JobContext, payload: dict[str, Any]) -> str:
    connection_id = ctx.connection_id or payload.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        raise BotaiError(ErrorCode.VALIDATION, "У задачи источника нет connection_id")
    return connection_id


async def _owned_connection(session: AsyncSession, user_id: str, connection_id: str) -> Connection:
    connection = await session.scalar(
        select(Connection).where(Connection.id == connection_id, Connection.user_id == user_id)
    )
    if connection is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
    return connection


def _payload_run_id(payload: dict[str, Any]) -> str | None:
    run_id = payload.get("_run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _artifact_root(payload: dict[str, Any], default: str | Path | None) -> str | None:
    override = payload.get("_artifact_root")
    if isinstance(override, str) and override:
        return override
    return str(default) if default is not None else None


def _auth_error(report: Any) -> bool:
    return any(error.code == "source_auth_required" for error in report.errors)


def _drift_error(report: Any) -> bool:
    return any(error.code in ("recipe_drift", "recipe_rejected") for error in report.errors)


def _only_retryable(report: Any) -> bool:
    return bool(report.errors) and all(error.retryable for error in report.errors)


def _report_stage(report: Any) -> str:
    if report.ok:
        counts = ", ".join(f"{kind}: {count}" for kind, count in list(report.counts.items())[:5])
        return f"готово ({counts or 'без записей'})"
    if _auth_error(report):
        return "нужен повторный вход"
    if report.errors:
        return f"ошибка: {report.errors[0].code}"
    return "без изменений"


# --- v3 compatibility bridges -----------------------------------------------------------------


async def _v3_connect(
    ctx: JobContext, services: Services, payload: dict[str, Any]
) -> dict[str, Any]:
    from botai.learn.handlers import HANDLERS as LEARN_HANDLERS

    handler = LEARN_HANDLERS[JobKind.CONNECT]
    return await handler(ctx, services, payload) or {}


async def _v3_sync(ctx: JobContext, services: Services, payload: dict[str, Any]) -> dict[str, Any]:
    from botai.sources.handlers import HANDLERS as SOURCE_HANDLERS

    handler = SOURCE_HANDLERS[JobKind.SYNC]
    return await handler(ctx, services, payload) or {}


async def _v3_repair(
    ctx: JobContext, services: Services, payload: dict[str, Any]
) -> dict[str, Any]:
    from botai.learn.handlers import HANDLERS as LEARN_HANDLERS

    handler = LEARN_HANDLERS[JobKind.REPAIR]
    return await handler(ctx, services, payload) or {}


__all__ = [
    "CONNECT_SYSTEM_PROMPT",
    "handle_connect",
    "handle_repair",
    "handle_sync",
    "make_v4_handlers",
]
