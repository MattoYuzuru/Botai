"""Benchmark data: seeds a scenario into the database and reads it back without W1's repository.

The benchmark must run whatever state `botai.domain.repo` is in, so it carries a small tenant-scoped
reader of its own (`user_id` in every query, like the repository). Production chat uses `RepoReader`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import (
    Chunk,
    Connection,
    Course,
    Deadline,
    Document,
    Grade,
    NewsItem,
    Person,
    ScheduleEvent,
    Space,
    User,
)
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    Provenance,
    ScheduleEventOut,
)

ORIGIN = "https://alpha.example"
HOST = "alpha.example"
BAD_STATES = frozenset({"stale", "error", "needs_reauth"})

Sm = async_sessionmaker[AsyncSession]


def _random_id(kind: str, key: str) -> str:
    return new_id()


def _t(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(slots=True)
class Seeded:
    user_id: str
    space_id: str
    connections: dict[str, str]  # scenario key -> connection id


async def seed(
    sm: Sm, data: dict[str, Any], *, make_id: Callable[[str, str], str] | None = None
) -> Seeded:
    """Create the student, the space, connections, domain rows and documents of a scenario.

    `make_id(kind, key)` lets the benchmark use stable ids, so recorded model exchanges replay.
    """
    ident = make_id or _random_id
    now = _t(data["now"]) or datetime.now().astimezone()
    user_id, space_id = ident("user", "student"), ident("space", "main")
    spec = data["space"]
    conns: dict[str, str] = {}
    course_ids = {c["title"]: c["id"] for c in data.get("courses", [])}
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=f"bench-{user_id.lower()}@example.test",
                display_name="Бенчмарк",
                password_hash="x",  # noqa: S106 - never verified
            )
        )
        await session.flush()
        session.add(
            Space(
                id=space_id,
                user_id=user_id,
                title=spec["title"],
                time_zone=spec.get("timeZone", "Europe/Moscow"),
                profile=spec.get("profile", {}),
            )
        )
        await session.flush()
        for c in data["connections"]:
            conn_id = ident("connection", c["key"])
            conns[c["key"]] = conn_id
            session.add(
                Connection(
                    id=conn_id,
                    user_id=user_id,
                    space_id=space_id,
                    origin=ORIGIN,
                    kind=c["kind"],
                    title=c["title"],
                    status=c["status"],
                    error_code=c.get("errorCode"),
                    last_sync_at=_t(c.get("lastSyncAt")),
                    allow_hosts=[HOST],
                )
            )
        await session.flush()

        def common(row: dict[str, Any], entity: str) -> dict[str, Any]:
            return {
                "user_id": user_id,
                "connection_id": conns[row["connection"]],
                "external_id": row["id"],
                "source_url": f"{ORIGIN}/{row['connection']}/{entity}",
                "fetched_at": now,
                "seen_at": now,
            }

        for r in data.get("courses", []):
            session.add(Course(title=r["title"], code=r.get("code"), **common(r, "courses")))
        for r in data.get("events", []):
            start = _t(r["start"])
            assert start is not None
            session.add(
                ScheduleEvent(
                    title=r["title"],
                    kind=r["kind"],
                    course_external_id=course_ids.get(r["title"]),
                    starts_at=start,
                    ends_at=start + timedelta(minutes=r.get("minutes", 80)),
                    format=r.get("format", "unknown"),
                    location_text=r.get("room"),
                    teacher_text=r.get("teacher"),
                    **common(r, "timetable"),
                )
            )
        for r in data.get("deadlines", []):
            due = _t(r.get("due"))
            session.add(
                Deadline(
                    title=r["title"],
                    course_external_id=course_ids.get(r.get("course", "")),
                    due_at=due,
                    effective_due_at=due,
                    state=r.get("state", "unknown"),
                    **common(r, "deadlines"),
                )
            )
        for r in data.get("grades", []):
            session.add(
                Grade(
                    title=r.get("title"),
                    course_external_id=course_ids.get(r.get("course", "")),
                    value=r["value"],
                    scale=r.get("scale"),
                    weight=r.get("weight"),
                    **common(r, "grades"),
                )
            )
        for r in data.get("people", []):
            session.add(
                Person(
                    name=r["name"],
                    role=r.get("role", "other"),
                    email=r.get("email"),
                    **common(r, "people"),
                )
            )
        for r in data.get("news", []):
            published = _t(r["published"])
            assert published is not None
            session.add(
                NewsItem(
                    title=r["title"],
                    body=r.get("body"),
                    published_at=published,
                    **common(r, "news"),
                )
            )
        await session.flush()
        for d in data.get("documents", []):
            doc_id = new_id()
            session.add(
                Document(
                    id=doc_id,
                    user_id=user_id,
                    space_id=space_id,
                    kind=d.get("kind", "page"),
                    title=d["title"],
                    url=d.get("url"),
                    fetched_at=now,
                    hash=doc_id,
                )
            )
            await session.flush()
            for i, text in enumerate(d["chunks"]):
                session.add(
                    Chunk(
                        user_id=user_id, document_id=doc_id, ord=i, text=text, tokens=len(text) // 3
                    )
                )
        await session.commit()
    return Seeded(user_id, space_id, conns)


class SqlReader:
    """`DomainReader` over the ORM models with the tenant predicate (benchmark only)."""

    @staticmethod
    async def _connections(
        session: AsyncSession, user_id: str, space_id: str | None
    ) -> dict[str, Connection]:
        stmt = select(Connection).where(Connection.user_id == user_id)
        if space_id:
            stmt = stmt.where(Connection.space_id == space_id)
        return {c.id: c for c in (await session.execute(stmt)).scalars()}

    @staticmethod
    def _prov(row: Any, conns: dict[str, Connection]) -> Provenance:
        conn = conns[row.connection_id]
        return Provenance(
            connection_id=conn.id,
            space_id=conn.space_id,
            source_url=row.source_url,
            fetched_at=row.fetched_at,
            stale=conn.status in BAD_STATES,
        )

    @staticmethod
    async def _course_titles(session: AsyncSession, user_id: str) -> dict[str, str]:
        rows = await session.execute(select(Course).where(Course.user_id == user_id))
        return {c.external_id: c.title for c in rows.scalars()}

    async def _rows(
        self, session: AsyncSession, model: Any, user_id: str, space_id: str | None, *where: Any
    ) -> tuple[list[Any], dict[str, Connection]]:
        conns = await self._connections(session, user_id, space_id)
        stmt = select(model).where(
            model.user_id == user_id, model.connection_id.in_(list(conns)), *where
        )
        return list((await session.execute(stmt)).scalars()), conns

    async def events(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        start: datetime,
        end: datetime,
        space_id: str | None,
    ) -> list[ScheduleEventOut]:
        rows, conns = await self._rows(
            session,
            ScheduleEvent,
            user_id,
            space_id,
            ScheduleEvent.starts_at >= start,
            ScheduleEvent.starts_at < end,
        )
        titles = await self._course_titles(session, user_id)
        return [
            ScheduleEventOut(
                id=r.id,
                title=r.title,
                kind=r.kind,  # type: ignore[arg-type]
                starts_at=r.starts_at,
                ends_at=r.ends_at,
                format=r.format,  # type: ignore[arg-type]
                location=r.location_text,
                teacher=r.teacher_text,
                course_title=titles.get(r.course_external_id or ""),
                url=r.url,
                changed=r.changed,
                conflicts_with=[],
                provenance=self._prov(r, conns),
            )
            for r in rows
        ]

    async def deadlines(
        self, session: AsyncSession, user_id: str, *, space_id: str | None, include_done: bool
    ) -> list[DeadlineOut]:
        extra = () if include_done else (Deadline.state.in_(("open", "unknown", "missed")),)
        rows, conns = await self._rows(session, Deadline, user_id, space_id, *extra)
        titles = await self._course_titles(session, user_id)
        return [
            DeadlineOut(
                id=r.id,
                title=r.title,
                due_at=r.due_at,
                effective_due_at=r.effective_due_at,
                state=r.state,  # type: ignore[arg-type]
                course_title=titles.get(r.course_external_id or ""),
                url=r.url,
                provenance=self._prov(r, conns),
            )
            for r in rows
        ]

    async def courses(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[CourseOut]:
        rows, conns = await self._rows(session, Course, user_id, space_id)
        return [
            CourseOut(
                id=r.id,
                title=r.title,
                code=r.code,
                term=r.term,
                url=r.url,
                provenance=self._prov(r, conns),
            )
            for r in rows
        ]

    async def grades(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[GradeOut]:
        rows, conns = await self._rows(session, Grade, user_id, space_id)
        titles = await self._course_titles(session, user_id)
        return [
            GradeOut(
                id=r.id,
                title=r.title,
                value=r.value,
                scale=r.scale,
                weight=r.weight,
                course_title=titles.get(r.course_external_id or ""),
                graded_at=r.graded_at,
                provenance=self._prov(r, conns),
            )
            for r in rows
        ]

    async def people(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[PersonOut]:
        rows, conns = await self._rows(session, Person, user_id, space_id)
        return [
            PersonOut(
                id=r.id, name=r.name, role=r.role, email=r.email, provenance=self._prov(r, conns)
            )
            for r in rows
        ]

    async def news(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[NewsOut]:
        rows, conns = await self._rows(session, NewsItem, user_id, space_id)
        return [
            NewsOut(
                id=r.id,
                title=r.title,
                body=r.body,
                published_at=r.published_at,
                url=r.url,
                provenance=self._prov(r, conns),
            )
            for r in rows
        ]
