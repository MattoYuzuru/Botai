"""Seeding helpers and an in-memory DomainReader for chat tests (the SQL side is W1's repo)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import Chunk, Connection, Document, Fact, Space
from botai.domain.schemas import (
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    Provenance,
    ScheduleEventOut,
)
from tests.agent.support import NOW

Sm = async_sessionmaker[AsyncSession]


async def add_space(
    sm: Sm,
    user_id: str,
    title: str = "Пространство 1",
    *,
    tz: str = "Europe/Moscow",
    profile: dict[str, Any] | None = None,
) -> str:
    space_id = new_id()
    async with sm() as session:
        session.add(
            Space(id=space_id, user_id=user_id, title=title, time_zone=tz, profile=profile or {})
        )
        await session.commit()
    return space_id


async def add_connection(
    sm: Sm,
    user_id: str,
    space_id: str,
    title: str = "Расписание",
    *,
    kind: str = "schedule",
    status: str = "active",
    last_sync_at: datetime | None = NOW,
    error_code: str | None = None,
    allow_hosts: list[str] | None = None,
) -> str:
    conn_id = new_id()
    async with sm() as session:
        session.add(
            Connection(
                id=conn_id,
                user_id=user_id,
                space_id=space_id,
                origin="https://fakeuni.test",
                kind=kind,
                title=title,
                status=status,
                last_sync_at=last_sync_at,
                error_code=error_code,
                allow_hosts=allow_hosts if allow_hosts is not None else ["fakeuni.test"],
            )
        )
        await session.commit()
    return conn_id


async def add_document(
    sm: Sm,
    user_id: str,
    space_id: str,
    title: str,
    chunks: list[str],
    *,
    url: str | None = "https://fakeuni.test/docs/rules",
    kind: str = "page",
) -> str:
    doc_id = new_id()
    async with sm() as session:
        session.add(
            Document(
                id=doc_id,
                user_id=user_id,
                space_id=space_id,
                kind=kind,
                title=title,
                url=url,
                fetched_at=NOW,
                hash=doc_id,
            )
        )
        await session.flush()
        for i, text in enumerate(chunks):
            session.add(
                Chunk(user_id=user_id, document_id=doc_id, ord=i, text=text, tokens=len(text) // 3)
            )
        await session.commit()
    return doc_id


async def add_fact(
    sm: Sm,
    user_id: str,
    question: str,
    answer: str,
    *,
    space_id: str | None = None,
    confirmed_at: datetime = NOW,
    stale_after: datetime | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> str:
    fact_id = new_id()
    async with sm() as session:
        session.add(
            Fact(
                id=fact_id,
                user_id=user_id,
                space_id=space_id,
                question=question,
                answer=answer,
                sources=sources
                or [
                    {
                        "title": "Положение",
                        "url": "https://fakeuni.test/docs/rules",
                        "kind": "page",
                        "fetchedAt": NOW.isoformat(),
                    }
                ],
                confirmed_at=confirmed_at,
                stale_after=stale_after or confirmed_at + timedelta(days=30),
                confidence="confirmed",
            )
        )
        await session.commit()
    return fact_id


def provenance(connection_id: str, space_id: str, *, stale: bool = False) -> Provenance:
    return Provenance(
        connection_id=connection_id,
        space_id=space_id,
        source_url="https://fakeuni.test/api/timetable",
        fetched_at=NOW,
        stale=stale,
    )


def event(
    prov: Provenance, title: str, starts_at: datetime, *, room: str = "А-101", **kw: Any
) -> ScheduleEventOut:
    return ScheduleEventOut(
        id=new_id(),
        title=title,
        kind=kw.pop("kind", "lecture"),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=80),
        format=kw.pop("format", "offline"),
        location=room,
        teacher=kw.pop("teacher", "Иванов И.И."),
        course_title=title,
        url=None,
        changed=kw.pop("changed", False),
        conflicts_with=kw.pop("conflicts_with", []),
        provenance=prov,
    )


def deadline(
    prov: Provenance, title: str, due_at: datetime | None, *, state: str = "open"
) -> DeadlineOut:
    return DeadlineOut(
        id=new_id(),
        title=title,
        due_at=due_at,
        effective_due_at=due_at,
        state=state,  # type: ignore[arg-type]
        course_title="Алгоритмы",
        url=None,
        provenance=prov,
    )


class FakeReader:
    """In-memory `DomainReader`."""

    def __init__(
        self,
        *,
        events: list[ScheduleEventOut] | None = None,
        deadlines: list[DeadlineOut] | None = None,
        grades: list[GradeOut] | None = None,
        people: list[PersonOut] | None = None,
        news: list[NewsOut] | None = None,
    ) -> None:
        self._events = events or []
        self._deadlines = deadlines or []
        self._grades = grades or []
        self._people = people or []
        self._news = news or []
        self.calls: list[str] = []

    async def events(
        self,
        session: Any,
        user_id: str,
        *,
        start: datetime,
        end: datetime,
        space_id: str | None,
        now: datetime,
    ) -> list[ScheduleEventOut]:
        self.calls.append("events")
        return [e for e in self._events if start <= e.starts_at < end]

    async def deadlines(
        self, session: Any, user_id: str, *, space_id: str | None, include_done: bool, now: datetime
    ) -> list[DeadlineOut]:
        self.calls.append("deadlines")
        return [d for d in self._deadlines if include_done or d.state == "open"]

    async def courses(
        self, session: Any, user_id: str, *, space_id: str | None, now: datetime
    ) -> list[Any]:
        self.calls.append("courses")
        return []

    async def grades(
        self, session: Any, user_id: str, *, space_id: str | None, now: datetime
    ) -> list[GradeOut]:
        self.calls.append("grades")
        return self._grades

    async def people(
        self, session: Any, user_id: str, *, space_id: str | None, now: datetime
    ) -> list[PersonOut]:
        self.calls.append("people")
        return self._people

    async def news(
        self, session: Any, user_id: str, *, space_id: str | None, now: datetime
    ) -> list[NewsOut]:
        self.calls.append("news")
        return self._news
