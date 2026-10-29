"""The environment of one agent turn: who is asking, in which thread, and what tools collect.

`ChatEnv` is created per CHAT job and handed to every tool. Besides the identifiers it carries
what a turn accumulates on the way to the answer: the citation book, the cards of tasks it
started, quick replies and the plan. Formatting helpers shared by the tool modules live here too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select

from botai.agent.reader import DomainReader, RepoReader
from botai.agent.sources import SourceBook
from botai.contracts.agent_ui import AgentUiBlock
from botai.core.jobs import JobContext
from botai.core.llm import LlmTool
from botai.core.services import Services
from botai.db.models import Connection, Space
from botai.domain.schemas import Provenance
from botai.search import SearchBudget

DEFAULT_TZ = "Europe/Moscow"
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
STATUS_RU = {
    "draft": "не завершено",
    "learning": "изучается",
    "active": "работает",
    "syncing": "обновляется",
    "stale": "данные устарели",
    "needs_reauth": "нужен повторный вход",
    "waiting_user": "ждёт ответа студента",
    "repairing": "чинится",
    "error": "ошибка",
    "disabled": "отключено",
}
JOB_STATE_RU = {
    "queued": "в очереди",
    "running": "выполняется",
    "waiting_user": "ждёт ответа студента",
    "done": "завершено",
    "failed": "завершено с ошибкой",
    "cancelled": "остановлено",
}


def thread_link(thread_id: str) -> str:
    return f"botai://threads/{thread_id}"


def connection_link(connection_id: str) -> str:
    return f"botai://connections/{connection_id}"


def document_link(document_id: str) -> str:
    return f"botai://documents/{document_id}"


@dataclass(slots=True)
class ConnectionMeta:
    id: str
    title: str
    kind: str
    status: str
    space_id: str
    space_title: str
    last_sync_at: datetime | None
    created_at: datetime
    error_code: str | None


@dataclass(slots=True)
class ChatEnv:
    """Everything the tools of one chat turn need. Created per CHAT job."""

    services: Services
    user_id: str
    thread_id: str
    job_id: str | None
    space_id: str | None
    sources: SourceBook = field(default_factory=SourceBook)
    reader: DomainReader = field(default_factory=RepoReader)
    used: set[str] = field(default_factory=set)
    repairs: list[str] = field(default_factory=list)  # titles of connections sent to repair
    public_question: str = ""  # only the current typed question; never history or private tool data
    search_budget: SearchBudget | None = None
    ctx: JobContext | None = None  # to publish the plan while the turn is still running
    blocks: list[AgentUiBlock] = field(default_factory=list)  # task cards appended to the reply
    quick_replies: list[str] = field(default_factory=list)
    plan_id: str | None = None
    # Public pages the agent may open: links from the question and from earlier public results.
    public_urls: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=dict)  # per-turn caps on live page reads
    transport: httpx.AsyncBaseTransport | None = None  # tests only
    _meta: tuple[list[ConnectionMeta], dict[str, str]] | None = None

    async def meta(self) -> tuple[list[ConnectionMeta], dict[str, str]]:
        """(connections of the student in scope, time zone by space id)."""
        if self._meta is None:
            async with self.services.db() as session:
                rows = (
                    await session.execute(
                        select(Connection, Space)
                        .join(Space, Space.id == Connection.space_id)
                        .where(Connection.user_id == self.user_id, Space.user_id == self.user_id)
                        .order_by(Connection.created_at)
                    )
                ).all()
                spaces = (
                    await session.execute(select(Space).where(Space.user_id == self.user_id))
                ).scalars()
                tz = {s.id: s.time_zone for s in spaces}
            conns = [
                ConnectionMeta(
                    c.id,
                    c.title,
                    c.kind,
                    c.status,
                    c.space_id,
                    s.title,
                    c.last_sync_at,
                    c.created_at,
                    c.error_code,
                )
                for c, s in rows
                if self.space_id is None or c.space_id == self.space_id
            ]
            self._meta = (conns, tz)
        return self._meta

    def forget_meta(self) -> None:
        """A tool changed connections: the next read must see them."""
        self._meta = None

    async def tz_for(self, space_id: str | None) -> ZoneInfo:
        _, tz = await self.meta()
        name = tz.get(space_id or self.space_id or "", "") or next(iter(tz.values()), DEFAULT_TZ)
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo(DEFAULT_TZ)

    async def titles(self) -> dict[str, ConnectionMeta]:
        conns, _ = await self.meta()
        return {c.id: c for c in conns}

    async def cite(self, prov: Provenance) -> str:
        conn = (await self.titles()).get(prov.connection_id)
        return self.sources.add(
            title=conn.title if conn else "Источник данных",
            url=prov.source_url,
            kind="data",
            fetched_at=prov.fetched_at,
            stale=prov.stale,
        )


# --- formatting ---------------------------------------------------------------------------------


def fmt_day(d: datetime, tz: ZoneInfo) -> str:
    local = d.astimezone(tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m}"


def fmt_dt(d: datetime | None, tz: ZoneInfo) -> str:
    if d is None:
        return "—"
    local = d.astimezone(tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m.%Y %H:%M}"


def fmt_updated(d: datetime | None, tz: ZoneInfo) -> str:
    return f"{d.astimezone(tz):%Y-%m-%d %H:%M}" if d else "ни разу"


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def clean(text: str | None, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> LlmTool:
    return LlmTool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )
