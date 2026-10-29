"""What the agent knows about the student's universities, sources and running tasks.

The reads behind `list_universities`, the turn context and `connect_source`. `ensure_space` and
`find_source` follow the contract of the university workstream (`botai.spaces`: one space per
`(user, university)`; a source is found by its origin) and are the single place that logic lives
for the agent; the university workstream's service may replace their bodies without touching
callers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Connection, Job, Space, University
from botai.jobs import dialogue
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import TASK_KINDS
from botai.jobs.tasks import task_title
from botai.spaces import catalog, universities

MY_MATERIALS = "Мои материалы"


@dataclass(slots=True)
class TaskView:
    job_id: str
    kind: str
    state: str
    thread_id: str | None
    connection_id: str | None
    title: str
    stage: str | None
    question: str | None  # the non-secret text of what the task is waiting for


@dataclass(slots=True)
class SourceView:
    id: str
    title: str
    kind: str
    origin: str
    status: str
    last_sync_at: datetime | None
    error_code: str | None
    task: TaskView | None = None


@dataclass(slots=True)
class SpaceView:
    space_id: str
    title: str
    university_id: str | None
    short_title: str | None
    sources: list[SourceView] = field(default_factory=list)


async def active_tasks(
    session: AsyncSession, user_id: str, *, thread_id: str | None = None
) -> list[TaskView]:
    """In-flight tasks of the student (of one thread when given), newest first."""
    query = select(Job).where(
        Job.user_id == user_id, Job.kind.in_(TASK_KINDS), Job.state.in_(sorted(IN_FLIGHT))
    )
    if thread_id is not None:
        query = query.where(Job.thread_id == thread_id)
    jobs = list(
        await session.scalars(
            query.order_by(Job.created_at.desc(), Job.id.desc()).execution_options(
                populate_existing=True
            )
        )
    )
    live_ids = {job.id for job in jobs}
    jobs = [job for job in jobs if job.payload.get("continuation_job_id") not in live_ids]
    titles = {
        cid: title
        for cid, title in await session.execute(
            select(Connection.id, Connection.title).where(
                Connection.user_id == user_id,
                Connection.id.in_([j.connection_id for j in jobs if j.connection_id]),
            )
        )
    }
    return [task_view(job, titles.get(job.connection_id or "")) for job in jobs]


def task_view(job: Job, connection_title: str | None) -> TaskView:
    pending = dialogue.state_of(job).get("pending")
    question = None
    if job.state == "waiting_user":
        # Only the kind of wait is public to the agent: never the text of a secret prompt.
        secret = isinstance(pending, dict) and bool(pending.get("secret"))
        channel = pending.get("channel") if isinstance(pending, dict) else None
        question = (
            "защищённый ввод пароля или кода в приложении"
            if secret
            else "вход в браузере в приложении"
            if channel == "manual"
            else "ответ студента в диалоге"
            if channel == "dialogue"
            else "выбор или подтверждение в приложении"
        )
    return TaskView(
        job_id=job.id,
        kind=job.kind,
        state=job.state,
        thread_id=job.thread_id,
        connection_id=job.connection_id,
        title=task_title(job.kind, connection_title),
        stage=job.stage,
        question=question,
    )


async def list_scope(session: AsyncSession, user_id: str) -> list[SpaceView]:
    """Universities (spaces) of the student with their sources, statuses and running tasks."""
    spaces = list(
        await session.scalars(
            select(Space).where(Space.user_id == user_id).order_by(Space.created_at, Space.id)
        )
    )
    unis = {
        u.id: u
        for u in await session.scalars(
            select(University).where(
                University.id.in_([s.university_id for s in spaces if s.university_id])
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
    tasks: dict[str, TaskView] = {}
    for task in await active_tasks(session, user_id):
        if task.connection_id and task.connection_id not in tasks:
            tasks[task.connection_id] = task
    by_space: dict[str, list[SourceView]] = defaultdict(list)
    for c in conns:
        by_space[c.space_id].append(
            SourceView(
                id=c.id,
                title=c.title,
                kind=c.kind,
                origin=c.origin,
                status=c.status,
                last_sync_at=c.last_sync_at,
                error_code=c.error_code,
                task=tasks.get(c.id),
            )
        )
    return [
        SpaceView(
            space_id=s.id,
            title=s.title,
            university_id=s.university_id,
            short_title=unis[s.university_id].short_title
            if s.university_id and s.university_id in unis
            else None,
            sources=by_space.get(s.id, []),
        )
        for s in spaces
    ]


async def find_source(session: AsyncSession, user_id: str, origin: str) -> Connection | None:
    """The student's source with this origin, whichever university it sits under."""
    return await session.scalar(
        select(Connection)
        .where(Connection.user_id == user_id, Connection.origin == origin)
        .order_by(Connection.created_at, Connection.id)
        .limit(1)
    )


async def ensure_space(
    session: AsyncSession, user_id: str, university_id: str | None
) -> tuple[Space, bool]:
    """The student's space of a university (one per university), created on first use.
    Without a university: "Мои материалы" for documents that belong to no university."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"university-space:{user_id}:{university_id or '-'}", 0)
            )
        )
    )
    space = await session.scalar(
        select(Space)
        .where(
            Space.user_id == user_id,
            Space.university_id == university_id
            if university_id
            else Space.university_id.is_(None),
        )
        .order_by(Space.created_at, Space.id)
        .limit(1)
        .with_for_update()
    )
    if space is not None:
        return space, False
    if university_id is None:
        space = Space(user_id=user_id, university_id=None, title=MY_MATERIALS)
    else:
        uni = await session.get(University, university_id)
        if uni is None:
            raise LookupError(university_id)
        space = Space(
            user_id=user_id, university_id=uni.id, title=uni.title, accent_hue=uni.accent_hue
        )
    session.add(space)
    await session.flush([space])
    return space, True


@dataclass(slots=True)
class Candidate:
    university: University
    match: str  # exact | alias | part
    aliases: tuple[str, ...]


async def candidates(session: AsyncSession, query: str, *, limit: int = 5) -> list[Candidate]:
    """Universities (learned registry and the name catalog) that the query may mean."""
    needle = catalog.name_key(query)
    if not needle:
        return []
    found: list[Candidate] = []
    for uni in await universities.search(session, query, limit=limit * 2):
        entry = catalog.identify(uni.title, uni.short_title, uni.id)
        names = catalog.names(entry) if entry else (uni.title, uni.short_title)
        keys = {catalog.name_key(n) for n in names}
        if needle in {catalog.name_key(uni.title), catalog.name_key(uni.short_title)}:
            match = "exact"
        elif needle in keys:
            match = "alias"
        else:
            match = "part"
        aliases = tuple(dict.fromkeys(n for n in names if n not in {uni.title, uni.short_title}))
        found.append(Candidate(uni, match, aliases))
    order = {"exact": 0, "alias": 1, "part": 2}
    found.sort(key=lambda c: order[c.match])
    return found[:limit]
