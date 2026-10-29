"""What a student sees of their universities and sources: kinds, human titles, tasks and threads.

A source (`Connection`) belongs to a university (`Space`). Its title is a human name, never a
bare host: when nothing better is known the name of its kind is shown. The thread of a source is
the thread of its latest task; the active task is the one that is queued, running or waiting for
the student.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import ChatThread, Connection, Job, Space, University
from botai.learn.addresses import navigation_url

ConnectionKind = Literal[
    "portal", "lms", "schedule", "site", "sheet", "doc", "document", "calendar", "other"
]
CONNECTION_KINDS: tuple[str, ...] = get_args(ConnectionKind)

KIND_TITLES: dict[str, str] = {
    "portal": "Личный кабинет",
    "lms": "Система обучения",
    "schedule": "Расписание",
    "site": "Сайт",
    "sheet": "Таблица",
    "doc": "Документ",
    "document": "Документ",
    "calendar": "Календарь",
    "other": "Источник",
}
TITLE_MAX = 120

# Jobs that work on a source and are shown to the student as its tasks.
TASK_KINDS = ("connect", "learn", "repair", "sync", "ingest")
IN_FLIGHT = ("queued", "running", "waiting_user")

_ADDRESS = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.I)


def normalize_kind(value: object) -> ConnectionKind:
    """A kind of the current vocabulary; anything unknown (an old `mail`, a typo) is `other`."""
    return value if isinstance(value, str) and value in CONNECTION_KINDS else "other"  # type: ignore[return-value]


def looks_like_address(text: str) -> bool:
    """`lk.example.org` or `https://example.org/x` as a name: a host, not a name."""
    return bool(_ADDRESS.match(text.strip()))


def source_title(*, kind: object, title: str | None = None, page_title: str | None = None) -> str:
    """The human title of a source: the given one, else the title of its page, else its kind."""
    for candidate in (title, page_title):
        cleaned = " ".join((candidate or "").split())[:TITLE_MAX]
        if cleaned and not looks_like_address(cleaned):
            return cleaned
    return KIND_TITLES[normalize_kind(kind)]


def clean_entry_url(value: object) -> str | None:
    """An address of a UI page that is safe to store and show: https, no credentials, no secret."""
    url = navigation_url(value)
    return url[:2000] if url else None


@dataclass(frozen=True, slots=True)
class ActiveTask:
    job_id: str
    kind: Literal["connect", "repair", "sync", "ingest"]
    state: Literal["queued", "running", "waiting_user"]


@dataclass(frozen=True, slots=True)
class TaskInfo:
    thread_id: str | None = None
    active_task: ActiveTask | None = None


@dataclass(frozen=True, slots=True)
class SourceView:
    connection: Connection
    title: str  # human; see `source_title`
    thread_id: str | None
    active_task: ActiveTask | None


@dataclass(frozen=True, slots=True)
class UniversitySources:
    space: Space
    university: University | None
    short_title: str
    aliases: tuple[str, ...]
    sources: list[SourceView]


def display_title(conn: Connection) -> str:
    return source_title(kind=conn.kind, title=conn.title)


async def load_task_info(
    session: AsyncSession, user_id: str, connection_ids: Sequence[str]
) -> dict[str, TaskInfo]:
    """Per source: the thread of its latest task that still has a thread, and its active task."""
    if not connection_ids:
        return {}
    mine = (Job.user_id == user_id, Job.connection_id.in_(connection_ids), Job.kind.in_(TASK_KINDS))
    newest = (Job.connection_id, Job.created_at.desc(), Job.id.desc())
    threads = await session.execute(
        select(Job.connection_id, Job.thread_id)
        .where(
            *mine,
            Job.thread_id.is_not(None),
            exists().where(ChatThread.id == Job.thread_id, ChatThread.user_id == user_id),
        )
        .ext(distinct_on(Job.connection_id))
        .order_by(*newest)
    )
    thread_of = {cid: tid for cid, tid in threads if cid is not None}
    active = await session.scalars(
        select(Job)
        .where(*mine, Job.state.in_(IN_FLIGHT))
        .ext(distinct_on(Job.connection_id))
        .order_by(*newest)
    )
    task_of = {
        job.connection_id: ActiveTask(
            job_id=job.id,
            kind="connect" if job.kind == "learn" else job.kind,  # type: ignore[arg-type]
            state=job.state,  # type: ignore[arg-type]
        )
        for job in active
        if job.connection_id is not None
    }
    return {
        cid: TaskInfo(thread_of.get(cid), task_of.get(cid))
        for cid in {*thread_of, *task_of, *connection_ids}
    }
