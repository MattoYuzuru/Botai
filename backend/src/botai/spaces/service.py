"""Spaces and connections: a university per space, sources per university, secrets, jobs.

Callers pass the request's `AsyncSession`. Reads for the agent (`ensure_space`, `list_sources`,
`find_source`) flush but never commit: the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobKind
from botai.core.queue import JobQueue
from botai.core.vault import SecretKind, Vault
from botai.db.models import Connection, Job, Space, University
from botai.jobs.queue import PgJobQueue, enqueue_in_session
from botai.learn.addresses import navigation_url, url_origin
from botai.spaces import repo, universities, views
from botai.spaces.schemas import (
    ActiveTaskOut,
    ConnectionCreate,
    ConnectionOut,
    SecretIn,
    SpaceCreate,
    SpaceOut,
    SpaceUpdate,
)
from botai.spaces.views import ActiveTask, SourceView, TaskInfo, UniversitySources

# Connections in these states have no finished setup (or were switched off): no sync, no repair.
_NOT_READY = frozenset({"draft", "learning", "disabled"})
_ICAL_SCHEMES = frozenset({"http", "https", "webcal"})
MATERIALS_TITLE = "Мои материалы"
MATERIALS_SHORT_TITLE = "Материалы"
_NAME_NOT_ADDRESS = "Назовите вуз словами, а не адресом сайта"


# --- what the API shows ---------------------------------------------------------------------------


def space_out(space: Space, university: University | None) -> SpaceOut:
    """`university` is the registry row of `space.university_id` (None for "My materials")."""
    return SpaceOut(
        id=space.id,
        title=space.title,
        short_title=university.short_title if university else MATERIALS_SHORT_TITLE,
        aliases=list(universities.aliases_of(university)) if university else [],
        university_id=space.university_id,
        accent_hue=space.accent_hue,
        time_zone=space.time_zone,
        created_at=space.created_at,
    )


async def spaces_out(session: AsyncSession, spaces: Sequence[Space]) -> list[SpaceOut]:
    ids = {s.university_id for s in spaces if s.university_id}
    rows = (
        {u.id: u for u in await session.scalars(select(University).where(University.id.in_(ids)))}
        if ids
        else {}
    )
    return [space_out(s, rows.get(s.university_id or "")) for s in spaces]


def _active_task_out(task: ActiveTask | None) -> ActiveTaskOut | None:
    return ActiveTaskOut(job_id=task.job_id, kind=task.kind, state=task.state) if task else None


async def connection_out(
    vault: Vault, conn: Connection, *, task: TaskInfo | None = None
) -> ConnectionOut:
    """`task` carries the thread and the active task of the source (`connection_outs` loads it)."""
    task = task or TaskInfo()
    return ConnectionOut(
        id=conn.id,
        space_id=conn.space_id,
        origin=conn.origin,
        kind=views.normalize_kind(conn.kind),
        adapter=conn.adapter,
        title=views.display_title(conn),
        entry_url=conn.entry_url,
        status=conn.status,  # type: ignore[arg-type]  # validated by the response model
        has_password=await vault.has(conn.user_id, conn.id, SecretKind.PASSWORD),
        stored_secrets=[
            kind.value  # type: ignore[misc]
            for kind in (SecretKind.TOKEN, SecretKind.ICAL_URL, SecretKind.API_KEY)
            if await vault.has(conn.user_id, conn.id, kind)
        ],
        last_sync_at=conn.last_sync_at,
        next_sync_at=conn.next_sync_at,
        error_code=conn.error_code,
        error_detail=conn.error_detail,
        thread_id=task.thread_id,
        active_task=_active_task_out(task.active_task),
    )


async def connection_outs(
    session: AsyncSession, vault: Vault, user_id: str, conns: Sequence[Connection]
) -> list[ConnectionOut]:
    tasks = await views.load_task_info(session, user_id, [c.id for c in conns])
    return [await connection_out(vault, c, task=tasks.get(c.id)) for c in conns]


# --- spaces ---------------------------------------------------------------------------------------


async def _space_of(
    session: AsyncSession, user_id: str, university: University | None
) -> tuple[Space, bool]:
    """The one space of (user, university) - or of the user's materials - made if it is missing."""
    uni_id = university.id if university else None
    # The same key as the chat's own lookup: two requests for one university queue up here, and the
    # unique index is only the last line of defence.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"university-space:{user_id}:{uni_id or ''}", 0)
            )
        )
    )
    existing = await session.scalar(
        select(Space)
        .where(
            Space.user_id == user_id,
            Space.university_id == uni_id if uni_id else Space.university_id.is_(None),
        )
        .order_by(Space.created_at, Space.id)
        .limit(1)
    )
    if existing is not None:
        return existing, False
    space = Space(
        user_id=user_id,
        university_id=uni_id,
        title=(university.title if university else MATERIALS_TITLE)[:120],
    )
    if university is not None:
        space.accent_hue = university.accent_hue
    session.add(space)
    await session.flush()
    return space, True


async def ensure_space(session: AsyncSession, user_id: str, university_id: str | None) -> Space:
    """The space of this university for this user, created on first use (never a second one).

    `university_id` is a registry id or the id of a catalog suggestion (which becomes a registry
    row now); `None` is the user's "My materials" for documents outside any university.
    """
    university = (
        await universities.find_or_create(session, title=university_id, university_id=university_id)
        if university_id
        else None
    )
    return (await _space_of(session, user_id, university))[0]


async def create_space(
    session: AsyncSession, user_id: str, body: SpaceCreate
) -> tuple[Space, University, bool]:
    """POST /spaces: the university by id, name or site, and its one space for this user.
    The third value tells whether the space is new."""
    if body.university_id is None and views.looks_like_address(body.title):
        raise BotaiError(ErrorCode.VALIDATION, _NAME_NOT_ADDRESS)
    university = await universities.find_or_create(
        session,
        title=body.title,
        university_id=body.university_id,
        homepage_url=body.homepage_url,
    )
    space, created = await _space_of(session, user_id, university)
    await session.commit()
    return space, university, created


async def update_space(
    session: AsyncSession, user_id: str, space_id: str, body: SpaceUpdate
) -> Space:
    space = await repo.get_space(session, user_id, space_id)
    if body.title is not None:
        title = " ".join(body.title.split())
        if not title:
            raise BotaiError(ErrorCode.VALIDATION, "Название не может быть пустым")
        if views.looks_like_address(title):
            raise BotaiError(ErrorCode.VALIDATION, _NAME_NOT_ADDRESS)
        space.title = title
    if body.time_zone is not None:
        try:
            ZoneInfo(body.time_zone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise BotaiError(ErrorCode.VALIDATION, "Неизвестный часовой пояс") from None
        space.time_zone = body.time_zone
    await session.commit()
    return space


async def delete_space(session: AsyncSession, user_id: str, space_id: str) -> None:
    """Removes the space with everything it owns (FR-023): connections, snapshots, documents and
    facts go by foreign keys; the secrets, which have none, go here."""
    space = await repo.get_space(session, user_id, space_id)
    connection_ids = list(
        await session.scalars(
            select(Connection.id).where(
                Connection.user_id == user_id, Connection.space_id == space.id
            )
        )
    )
    await repo.purge_connection_secrets(session, user_id, connection_ids)
    await session.delete(space)
    await session.commit()


# --- the universities of a student and their sources (the agent's view) ----------------------------


async def list_sources(session: AsyncSession, user_id: str) -> list[UniversitySources]:
    """Every university of the user with its sources, oldest first; each source with its
    human title, the thread of its latest task and its active task."""
    spaces = await repo.list_spaces(session, user_id)
    unis = {
        u.id: u
        for u in await session.scalars(
            select(University).where(
                University.id.in_({s.university_id for s in spaces if s.university_id})
            )
        )
    }
    conns = list(
        await session.scalars(
            select(Connection)
            .where(Connection.user_id == user_id)
            .order_by(Connection.created_at, Connection.id)
        )
    )
    tasks = await views.load_task_info(session, user_id, [c.id for c in conns])
    result: list[UniversitySources] = []
    for space in spaces:
        uni = unis.get(space.university_id or "")
        result.append(
            UniversitySources(
                space=space,
                university=uni,
                short_title=uni.short_title if uni else MATERIALS_SHORT_TITLE,
                aliases=universities.aliases_of(uni) if uni else (),
                sources=[_view(c, tasks.get(c.id)) for c in conns if c.space_id == space.id],
            )
        )
    return result


def _view(conn: Connection, task: TaskInfo | None) -> SourceView:
    task = task or TaskInfo()
    return SourceView(conn, views.display_title(conn), task.thread_id, task.active_task)


async def find_source(session: AsyncSession, user_id: str, origin: str) -> Connection | None:
    """The user's source on this site (`origin` may be a full address), oldest first."""
    try:
        wanted = universities.origin_of(origin)
    except BotaiError:
        return None
    return await session.scalar(
        select(Connection)
        .where(Connection.user_id == user_id, Connection.origin == wanted)
        .order_by(Connection.created_at, Connection.id)
        .limit(1)
    )


# --- connections ----------------------------------------------------------------------------------

_ORIGIN_MAX = 255


async def create_connection(
    session: AsyncSession, queue: JobQueue, user_id: str, space_id: str, body: ConnectionCreate
) -> tuple[Connection, str]:
    """Creates a `draft` connection and enqueues its `connect` job (login + learn)."""
    space = await repo.get_space(session, user_id, space_id)
    from botai.spaces.conversations import safe_url

    url = safe_url(body.url) if body.url else None
    origin = universities.origin_of(url) if url else ""
    if len(origin) > _ORIGIN_MAX:
        raise BotaiError(ErrorCode.VALIDATION, "Ссылка слишком длинная")
    kind = views.normalize_kind(body.kind)

    conn: Connection | None = None
    if origin:
        # A repeated request while the first one is still being set up is the same connection.
        conn = await session.scalar(
            select(Connection).where(
                Connection.user_id == user_id,
                Connection.space_id == space.id,
                Connection.origin == origin,
                Connection.kind == kind,
                Connection.status.in_(("draft", "learning")),
            )
        )
    created = conn is None
    if conn is None:
        conn = Connection(
            user_id=user_id,
            space_id=space.id,
            origin=origin,
            kind=kind,
            title=views.source_title(kind=kind, title=body.title),
            entry_url=views.clean_entry_url(url),
            status="draft",
            allow_hosts=[urlsplit(origin).hostname or ""] if origin else [],
        )
        session.add(conn)
    # End our transaction before the queue (which works in its own) is called.
    await session.commit()

    try:
        job_id = await queue.enqueue(
            JobKind.CONNECT,
            user_id=user_id,
            connection_id=conn.id,
            # Keep the pre-chat REST entry point's original data selection explicit.
            # Chat bootstrap supplies its own schedule-only LearningIntent instead.
            payload={
                **({"url": url, "kind": body.kind} if url or body.kind else {}),
                "kinds": ["scheduleEvents", "deadlines"],
            },
            dedupe=True,
        )
    except Exception:
        if created:  # no job means nobody would ever finish this connection
            await session.delete(conn)
            await session.commit()
        raise
    return conn, job_id


async def ensure_source(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    *,
    url: str,
    kind: str | None = None,
    title: str | None = None,
    page_title: str | None = None,
) -> tuple[Connection, bool]:
    """The source of a site in a university: the one already there, or a new `draft` one.

    The title is human (the given one, else the title of the page, else the name of the kind -
    never the host) and `entry_url` is the page the student pointed at, if it is safe to keep.
    The second value tells whether the source is new; starting its job is the caller's.
    """
    entry = views.clean_entry_url(url)
    if entry is None:
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Нужна обычная HTTPS-ссылка без паролей, токенов и служебных кодов",
        )
    origin = universities.origin_of(entry)
    if len(origin) > _ORIGIN_MAX:
        raise BotaiError(ErrorCode.VALIDATION, "Ссылка слишком длинная")
    space = await repo.get_space(session, user_id, space_id)
    await session.execute(  # two requests for one site queue up here
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"source:{user_id}:{origin}", 0)))
    )
    existing = await session.scalar(
        select(Connection)
        .where(
            Connection.user_id == user_id,
            Connection.space_id == space.id,
            Connection.origin == origin,
        )
        .order_by(Connection.created_at, Connection.id)
        .limit(1)
    )
    if existing is not None:
        return existing, False
    conn = Connection(
        user_id=user_id,
        space_id=space.id,
        origin=origin,
        kind=views.normalize_kind(kind),
        title=views.source_title(kind=kind, title=title, page_title=page_title),
        entry_url=entry,
        status="draft",
        allow_hosts=[urlsplit(origin).hostname or ""],
    )
    session.add(conn)
    await session.flush()
    return conn, True


async def delete_connection(session: AsyncSession, user_id: str, connection_id: str) -> None:
    conn = await repo.get_connection(session, user_id, connection_id)
    await repo.purge_connection_secrets(session, user_id, [conn.id])
    await session.delete(conn)
    await session.commit()


def _same_origin_intent(data: object, origin: str) -> dict[str, Any] | None:
    """Only the source's own explicit learning context may enter a retry payload."""
    from botai.spaces.conversations import intent_of, normalize_context

    if not isinstance(data, dict) or "requested_kinds" not in data:
        return None
    raw_urls = data.get("entry_urls")
    urls = raw_urls[:6] if isinstance(raw_urls, list) else []
    own_urls = [
        url for value in urls if (url := navigation_url(value)) and url_origin(url) == origin
    ]
    return intent_of(
        normalize_context(
            {
                "entry_urls": own_urls,
                "requested_kinds": data["requested_kinds"],
                "absent_kinds": data.get("absent_kinds", []),
                "user_note": data.get("user_note", ""),
                **({"discovery_mode": data["discovery_mode"]} if "discovery_mode" in data else {}),
            }
        )
    )


async def _connect_retry_payload(
    session: AsyncSession,
    conn: Connection,
    context: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    previous = await session.scalar(
        select(Job)
        .where(
            Job.user_id == conn.user_id,
            Job.connection_id == conn.id,
            Job.kind == JobKind.CONNECT.value,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )
    prior = previous.payload if previous else {}
    canonical = _same_origin_intent(context, conn.origin)
    source_intent = _same_origin_intent(prior.get("learning_intent"), conn.origin)
    context_urls = context.get("entry_urls") if context else None
    foreign_context = isinstance(context_urls, list) and any(
        url_origin(url) != conn.origin for value in context_urls if (url := navigation_url(value))
    )
    # A conversation may now point at a child source: its parent's own intent wins then.
    if canonical and not canonical["entry_urls"] and foreign_context:
        intent = source_intent
    else:
        intent = canonical or source_intent
    result = {
        key: value
        for key, value in (payload or {}).items()
        if key not in {"learning_intent", "url", "kinds"}
    }
    entry = navigation_url(prior.get("url"))
    if entry and url_origin(entry) != conn.origin:
        entry = None
    if intent is not None:
        if not intent["entry_urls"]:
            intent["entry_urls"] = list(source_intent["entry_urls"]) if source_intent else []
            if not intent["entry_urls"] and entry:
                intent["entry_urls"] = [entry]
        result["learning_intent"] = intent
        result["kinds"] = list(intent["requested_kinds"])
        if intent["entry_urls"]:
            result["url"] = intent["entry_urls"][0]
    else:
        result["kinds"] = ["scheduleEvents", "deadlines"]  # Legacy REST before a chat context.
        if entry:
            result["url"] = entry
    return result


async def enqueue_for_connection(
    session: AsyncSession,
    queue: JobQueue,
    user_id: str,
    connection_id: str,
    kind: JobKind,
    *,
    thread_id: str | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Manual `sync` / `repair`. Deduplicated: a job already queued or running is returned."""
    conn = await repo.get_connection(session, user_id, connection_id)
    if conn.status == "disabled":
        raise BotaiError(ErrorCode.CONFLICT, "Подключение ещё настраивается или отключено")
    manual_login = kind == JobKind.CONNECT and (payload or {}).get("manual_login") is True
    if manual_login:
        pass  # Explicit human login can resume both unfinished and configured sources.
    elif (
        kind in {JobKind.SYNC, JobKind.REPAIR}
        and conn.status in {"draft", "learning", "error"}
        and conn.adapter is None
        and conn.spec_id is None
    ):
        # «Войти снова» must resume setup if the first login never produced an adapter.
        # CONNECT deduplication also brings an existing waiting setup back into view.
        kind = JobKind.CONNECT
    elif conn.status in _NOT_READY:
        raise BotaiError(ErrorCode.CONFLICT, "Подключение ещё настраивается или отключено")
    from botai.spaces.conversations import bind_thread, canonical_thread, owned_thread

    thread = await canonical_thread(session, user_id, conn.id)
    bind_after_enqueue = False
    if thread is None and thread_id is not None:
        thread = await owned_thread(session, user_id, thread_id)
        if manual_login:
            # Worker terminal projection locks Job -> Thread. Keep this thread clean
            # until enqueue has locked/selected the existing source job as well.
            bind_after_enqueue = True
        else:
            bind_thread(thread, conn.id)
    target_thread = thread.id if thread is not None else None
    if kind == JobKind.CONNECT:
        payload = await _connect_retry_payload(
            session, conn, thread.connect_context if thread else None, payload
        )
    if manual_login and isinstance(queue, PgJobQueue):
        job = await enqueue_in_session(
            session,
            kind,
            user_id=user_id,
            connection_id=conn.id,
            thread_id=target_thread,
            payload=payload,
            dedupe=True,
            now=now or SystemClock().now(),
        )
        if target_thread is not None and job.thread_id is None:
            job.thread_id = target_thread
        if bind_after_enqueue and thread is not None:
            bind_thread(thread, conn.id)
        await session.commit()
        return job.id
    if manual_login:
        running = await session.scalar(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.connection_id == conn.id,
                Job.kind.in_(("connect", "learn", "repair", "sync")),
                Job.state.in_(("queued", "running", "waiting_user")),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
            .with_for_update()
        )
        if running is not None:
            if target_thread is not None and running.thread_id is None:
                running.thread_id = target_thread
            if bind_after_enqueue and thread is not None:
                bind_thread(thread, conn.id)
            await session.commit()
            return running.id
    if bind_after_enqueue and thread is not None:
        bind_thread(thread, conn.id)
    await session.commit()  # the queue works in its own transaction
    job_id = await queue.enqueue(
        kind,
        user_id=user_id,
        connection_id=conn.id,
        thread_id=target_thread,
        payload=payload,
        dedupe=True,
    )
    # An existing deduplicated legacy job can predate its conversation.
    if target_thread is not None:
        job = await session.scalar(
            select(Job).where(Job.id == job_id, Job.user_id == user_id).with_for_update()
        )
        if job is not None and job.thread_id is None:
            job.thread_id = target_thread
            await session.commit()
    return job_id


async def put_secret(
    session: AsyncSession, vault: Vault, user_id: str, connection_id: str, body: SecretIn
) -> None:
    conn = await repo.get_connection(session, user_id, connection_id)
    value = body.value.strip()
    if not value:
        raise BotaiError(ErrorCode.VALIDATION, "Значение не может быть пустым")
    if body.kind == "ical_url" and urlsplit(value).scheme.lower() not in _ICAL_SCHEMES:
        raise BotaiError(ErrorCode.VALIDATION, "Нужна ссылка на календарь (https:// или webcal://)")
    await session.commit()  # the Vault works in its own transaction
    await vault.put(user_id, conn.id, SecretKind(body.kind), value)


async def forget_password(
    session: AsyncSession, vault: Vault, user_id: str, connection_id: str
) -> None:
    conn = await repo.get_connection(session, user_id, connection_id)
    await session.commit()  # the Vault works in its own transaction
    await vault.delete(user_id, conn.id, SecretKind.PASSWORD)
    await vault.delete(user_id, conn.id, SecretKind.LOGIN)
