"""Chat tools (docs/architecture/agent-harness.md §4, security.md T5).

The agent's reading tools: schedule, deadlines, courses, grades, people and news (through
`DomainReader`), and full-text search over documents and saved facts. The tools of the other
areas live next to them and are assembled here into one registry: universities and tasks
(`tools_sources`), live pages and documents (`tools_web`), the conversation itself
(`tools_thread`). Nothing here writes to a university: the only writes are tasks that the
student's own thread asks for, and notes about the student.

Every result is plain text with source keys (`[S1]`). Dates are shown in the space's time zone.
The harness wraps the text in `<source_data>`; what these functions return is untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from botai.agent import tools_sources, tools_thread, tools_web
from botai.agent.env import (
    DEFAULT_TZ,
    STATUS_RU,
    ChatEnv,
    clean,
    fmt_day,
    fmt_dt,
    fmt_updated,
    parse_date,
    spec,
)
from botai.agent.harness import HarnessTool
from botai.core.llm import LlmTool
from botai.db.models import Chunk, Document, Fact

MAX_RANGE_DAYS = 31
MAX_ROWS = 60
CHUNK_CHARS = 1200
KIND_RU = {
    "lecture": "лекция",
    "seminar": "семинар",
    "lab": "лабораторная",
    "exam": "экзамен",
    "consultation": "консультация",
    "other": "занятие",
}
FORMAT_RU = {"offline": "очно", "online": "онлайн", "hybrid": "смешанно", "unknown": ""}
STATE_RU = {
    "open": "не сдано",
    "submitted": "сдано",
    "graded": "оценено",
    "missed": "пропущено",
    "unknown": "статус неизвестен",
}

DATA_TOOLS = frozenset(
    {
        "get_schedule",
        "get_deadlines",
        "get_courses",
        "get_grades",
        "get_people",
        "get_news",
        "list_universities",
        "task_status",
        "browse_source",
        "site_map",
    }
)
DOCUMENT_TOOLS = frozenset(
    {
        "search_documents",
        "search_facts",
        "search_public_web",
        "fetch_public_page",
        "read_document",
    }
)


# Whatever these return was written by someone else (a teacher in a timetable or a news item, a
# document, a page, a search hit): after any of them `remember` takes only what the student said.
OUTSIDE_TEXT = frozenset(
    {
        "get_schedule",
        "get_deadlines",
        "get_courses",
        "get_grades",
        "get_people",
        "get_news",
        "search_documents",
        "search_facts",
        "search_public_web",
        "fetch_public_page",
        "read_document",
        "browse_source",
    }
)


async def _empty_note(env: ChatEnv, what: str) -> str:
    """Honest empty result: say what was checked, with the connections as citable sources."""
    conns, tz = await env.meta()
    if not conns:
        return f"{what}: данных нет. Подключённых источников нет."
    lines = [f"{what}: записей нет. Проверенные подключения:"]
    for c in conns:
        zone = ZoneInfo(tz.get(c.space_id) or DEFAULT_TZ)
        key = env.sources.add(
            title=c.title,
            url=f"botai://connections/{c.id}",
            kind="connection",
            fetched_at=c.last_sync_at or c.created_at,
        )
        lines.append(
            f"- [{key}] «{c.title}» ({c.space_title}): {STATUS_RU.get(c.status, c.status)}, "
            f"обновлено {fmt_updated(c.last_sync_at, zone)}"
        )
    return "\n".join(lines)


# --- domain tools -------------------------------------------------------------------------------


async def get_schedule(env: ChatEnv, args: dict[str, Any]) -> str:
    tz = await env.tz_for(None)
    today = env.services.clock.now().astimezone(tz).date()
    start_d = parse_date(args["from_date"]) if args.get("from_date") else today
    end_d = parse_date(args["to_date"]) if args.get("to_date") else start_d
    if start_d is None or end_d is None:
        return "Ошибка аргументов: даты нужны в формате YYYY-MM-DD."
    end_d = max(end_d, start_d)
    if (end_d - start_d).days >= MAX_RANGE_DAYS:
        end_d = start_d + timedelta(days=MAX_RANGE_DAYS - 1)
    start = datetime.combine(start_d, time.min, tzinfo=tz)
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=tz)
    async with env.services.db() as session:
        events = await env.reader.events(
            session,
            env.user_id,
            start=start,
            end=end,
            space_id=env.space_id,
            now=env.services.clock.now(),
        )
    title = f"Расписание с {start_d} по {end_d}"
    if not events:
        return await _empty_note(env, title)
    _, tzs = await env.meta()
    lines = [f"{title} (время в часовом поясе пространства). Занятий: {len(events)}."]
    for e in sorted(events, key=lambda x: x.starts_at)[:MAX_ROWS]:
        zone = ZoneInfo(tzs.get(e.provenance.space_id) or DEFAULT_TZ)
        key = await env.cite(e.provenance)
        s, f = e.starts_at.astimezone(zone), e.ends_at.astimezone(zone)
        parts = [
            f"{fmt_day(e.starts_at, zone)}, {s:%H:%M}–{f:%H:%M}",
            e.title,
            KIND_RU.get(e.kind, e.kind),
            f"ауд./место: {e.location}" if e.location else "",
            e.teacher or "",
            FORMAT_RU.get(e.format, ""),
            "ИЗМЕНЕНО" if e.changed else "",
            "пересекается с занятием другого вуза" if e.conflicts_with else "",
            "данные устарели" if e.provenance.stale else "",
        ]
        lines.append("- " + " | ".join(p for p in parts if p) + f" [{key}]")
    if len(events) > MAX_ROWS:
        lines.append(f"…и ещё {len(events) - MAX_ROWS}: сузьте диапазон дат.")
    return "\n".join(lines)


async def get_deadlines(env: ChatEnv, args: dict[str, Any]) -> str:
    include_done = bool(args.get("include_done", False))
    tz = await env.tz_for(None)
    now = env.services.clock.now()
    async with env.services.db() as session:
        rows = await env.reader.deadlines(
            session,
            env.user_id,
            space_id=env.space_id,
            include_done=include_done,
            now=env.services.clock.now(),
        )
    if not rows:
        return await _empty_note(env, "Дедлайны")
    far = datetime.max.replace(tzinfo=tz)
    lines = [f"Дедлайны ({len(rows)}). Сейчас {fmt_dt(now, tz)}."]
    for d in sorted(rows, key=lambda x: x.effective_due_at or x.due_at or far)[:MAX_ROWS]:
        due = d.effective_due_at or d.due_at
        overdue = bool(due and due < now and d.state == "open")
        key = await env.cite(d.provenance)
        parts = [
            f"срок {fmt_dt(due, tz)}" if due else "срок не указан",
            d.title,
            d.course_title or "",
            STATE_RU.get(d.state, d.state),
            "ПРОСРОЧЕНО" if overdue else "",
            "данные устарели" if d.provenance.stale else "",
        ]
        lines.append("- " + " | ".join(p for p in parts if p) + f" [{key}]")
    return "\n".join(lines)


async def get_courses(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        rows = await env.reader.courses(
            session, env.user_id, space_id=env.space_id, now=env.services.clock.now()
        )
    if not rows:
        return await _empty_note(env, "Курсы")
    lines = [f"Курсы ({len(rows)})."]
    for c in rows[:MAX_ROWS]:
        key = await env.cite(c.provenance)
        parts = [c.title, f"код {c.code}" if c.code else "", c.term or ""]
        lines.append("- " + " | ".join(p for p in parts if p) + f" [{key}]")
    return "\n".join(lines)


async def get_grades(env: ChatEnv, args: dict[str, Any]) -> str:
    needle = clean(str(args.get("course", "")), 100).lower()
    async with env.services.db() as session:
        rows = await env.reader.grades(
            session, env.user_id, space_id=env.space_id, now=env.services.clock.now()
        )
    if needle:
        rows = [g for g in rows if needle in (g.course_title or "").lower()]
    if not rows:
        return await _empty_note(env, "Оценки")
    tz = await env.tz_for(None)
    lines = [f"Оценки ({len(rows)})."]
    for g in rows[:MAX_ROWS]:
        key = await env.cite(g.provenance)
        parts = [
            g.course_title or "",
            g.title or "",
            f"оценка {g.value}" + (f" по шкале {g.scale}" if g.scale else ""),
            f"вес {g.weight:g}" if g.weight is not None else "",
            f"выставлена {g.graded_at.astimezone(tz):%d.%m.%Y}" if g.graded_at else "",
        ]
        lines.append("- " + " | ".join(p for p in parts if p) + f" [{key}]")
    return "\n".join(lines)


async def get_people(env: ChatEnv, args: dict[str, Any]) -> str:
    needle = clean(str(args.get("query", "")), 100).lower()
    async with env.services.db() as session:
        rows = await env.reader.people(
            session, env.user_id, space_id=env.space_id, now=env.services.clock.now()
        )
    if needle:
        rows = [p for p in rows if needle in p.name.lower() or needle in p.role.lower()]
    if not rows:
        return await _empty_note(env, "Преподаватели и сотрудники")
    lines = [f"Люди ({len(rows)})."]
    for p in rows[:MAX_ROWS]:
        key = await env.cite(p.provenance)
        parts = [p.name, p.role, p.email or ""]
        lines.append("- " + " | ".join(x for x in parts if x) + f" [{key}]")
    return "\n".join(lines)


async def get_news(env: ChatEnv, args: dict[str, Any]) -> str:
    limit = max(1, min(int(args.get("limit", 5) or 5), 10))
    tz = await env.tz_for(None)
    async with env.services.db() as session:
        rows = await env.reader.news(
            session, env.user_id, space_id=env.space_id, now=env.services.clock.now()
        )
    if not rows:
        return await _empty_note(env, "Новости")
    lines = [f"Новости (показано {min(limit, len(rows))} из {len(rows)})."]
    for n in sorted(rows, key=lambda x: x.published_at, reverse=True)[:limit]:
        key = await env.cite(n.provenance)
        body = clean(n.body, 300)
        lines.append(
            f"- {n.published_at.astimezone(tz):%d.%m.%Y} | {n.title}"
            + (f" | {body}" if body else "")
            + f" [{key}]"
        )
    return "\n".join(lines)


# --- search -------------------------------------------------------------------------------------


def _or_query(text: str) -> str:
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2][:12]
    return " | ".join(dict.fromkeys(words))


def _tsqueries(query: str) -> list[Any]:
    """Strict first (all words), then lenient (any word) so a wordy question still finds text."""
    queries: list[Any] = [func.websearch_to_tsquery("russian", query)]
    if lenient := _or_query(query):
        queries.append(func.to_tsquery("russian", lenient))
    return queries


async def search_documents(env: ChatEnv, args: dict[str, Any]) -> str:
    query = clean(str(args.get("query", "")), 300)
    if not query:
        return "Ошибка аргументов: нужен непустой query."
    limit = max(1, min(int(args.get("limit", 5) or 5), 8))
    async with env.services.db() as session:
        rows: list[Any] = []
        for tsquery in _tsqueries(query):
            rank = func.ts_rank_cd(Chunk.tsv, tsquery)
            stmt = (
                select(
                    Chunk.text,
                    Chunk.ord,
                    rank.label("rank"),
                    Document.id.label("doc_id"),
                    Document.title,
                    Document.url,
                    Document.kind,
                    Document.fetched_at,
                )
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.user_id == env.user_id,
                    Document.user_id == env.user_id,
                    Chunk.tsv.op("@@")(tsquery),
                )
                .order_by(rank.desc(), Chunk.ord)
                .limit(limit)
            )
            if env.space_id:
                stmt = stmt.where(Document.space_id == env.space_id)
            rows = list((await session.execute(stmt)).all())
            if rows:
                break
    if not rows:
        return f"По запросу «{query}» в документах и страницах ничего не найдено."
    tz = await env.tz_for(None)
    lines = [f"Найдено фрагментов: {len(rows)} по запросу «{query}»."]
    for r in rows:
        key = env.sources.add(
            title=r.title,
            url=r.url or f"botai://documents/{r.doc_id}",
            kind="page" if r.kind == "page" else "document",
            fetched_at=r.fetched_at,
        )
        lines.append(
            f"[{key}] «{r.title}», фрагмент {r.ord + 1}, получен {fmt_updated(r.fetched_at, tz)}:\n"
            f"{r.text[:CHUNK_CHARS]}\n---"
        )
    return "\n".join(lines)


async def search_facts(env: ChatEnv, args: dict[str, Any]) -> str:
    query = clean(str(args.get("query", "")), 300)
    if not query:
        return "Ошибка аргументов: нужен непустой query."
    now = env.services.clock.now()
    async with env.services.db() as session:
        rows: list[Fact] = []
        for tsquery in _tsqueries(query):
            doc = func.to_tsvector("russian", Fact.question + " " + Fact.answer)
            stmt = (
                select(Fact)
                .where(Fact.user_id == env.user_id, doc.op("@@")(tsquery))
                .order_by(func.ts_rank_cd(doc, tsquery).desc())
                .limit(3)
            )
            if env.space_id:
                stmt = stmt.where((Fact.space_id == env.space_id) | Fact.space_id.is_(None))
            rows = list((await session.execute(stmt)).scalars())
            if rows:
                break
    if not rows:
        return f"Сохранённых подтверждённых ответов по запросу «{query}» нет."
    tz = await env.tz_for(None)
    lines = [f"Сохранённые ответы: {len(rows)}."]
    for f in rows:
        stale = f.stale_after < now
        keys: list[str] = []
        for s in f.sources:
            fetched = _parse_iso(s.get("fetchedAt"))
            keys.append(
                env.sources.add(
                    title=str(s.get("title", "Документ"))[:200],
                    url=str(s.get("url", "")),
                    kind=str(s.get("kind", "document")),
                    fetched_at=fetched,
                    stale=stale,
                )
            )
        verdict = (
            "УСТАРЕЛ: перепроверьте по документам (search_documents) перед ответом"
            if stale
            else "актуален"
        )
        lines.append(
            f"- Вопрос: {clean(f.question, 300)}\n  Ответ: {clean(f.answer, 1500)}\n"
            f"  Подтверждён {fmt_updated(f.confirmed_at, tz)} ({verdict}). Источники: "
            + ", ".join(f"[{k}]" for k in keys)
        )
    return "\n".join(lines)


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


_DATE = {"type": "string", "description": "Дата YYYY-MM-DD"}
READ_SPECS: dict[str, LlmTool] = {
    "get_schedule": spec(
        "get_schedule",
        "Занятия студента за период. Без аргументов — сегодня. Диапазон не больше 31 дня.",
        {"from_date": _DATE, "to_date": {**_DATE, "description": "Последний день, включительно"}},
        [],
    ),
    "get_deadlines": spec(
        "get_deadlines",
        "Сроки сдачи работ студента, ближайшие первыми. По умолчанию без сданных.",
        {"include_done": {"type": "boolean", "description": "Показать и сданные"}},
        [],
    ),
    "get_courses": spec("get_courses", "Курсы студента.", {}, []),
    "get_grades": spec(
        "get_grades",
        "Оценки студента; можно сузить по части названия курса.",
        {"course": {"type": "string", "description": "Часть названия курса"}},
        [],
    ),
    "get_people": spec(
        "get_people",
        "Преподаватели и сотрудники из данных студента; поиск по части имени или роли.",
        {"query": {"type": "string"}},
        [],
    ),
    "get_news": spec(
        "get_news",
        "Последние новости вуза студента.",
        {"limit": {"type": "integer", "description": "От 1 до 10, по умолчанию 5"}},
        [],
    ),
    "search_documents": spec(
        "search_documents",
        "Полнотекстовый поиск по подключённым документам и страницам студента. "
        "Запрос — ключевые слова, как они встречаются в документах.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "От 1 до 8, по умолчанию 5"},
        },
        ["query"],
    ),
    "search_facts": spec(
        "search_facts",
        "Поиск среди ранее подтверждённых ответов на вопросы о правилах и процедурах.",
        {"query": {"type": "string"}},
        ["query"],
    ),
}
READ_STAGES: dict[str, str] = {
    "get_schedule": "Ищу в расписании",
    "get_deadlines": "Смотрю дедлайны",
    "get_courses": "Смотрю курсы",
    "get_grades": "Смотрю оценки",
    "get_people": "Ищу преподавателей",
    "get_news": "Читаю новости",
    "search_documents": "Ищу в документах",
    "search_facts": "Ищу в сохранённых ответах",
}
READ_IMPLS: dict[str, Callable[[ChatEnv, dict[str, Any]], Awaitable[str]]] = {
    "get_schedule": get_schedule,
    "get_deadlines": get_deadlines,
    "get_courses": get_courses,
    "get_grades": get_grades,
    "get_people": get_people,
    "get_news": get_news,
    "search_documents": search_documents,
    "search_facts": search_facts,
}

TOOL_SPECS: dict[str, LlmTool] = {
    **READ_SPECS,
    **tools_sources.SPECS,
    **tools_web.SPECS,
    **tools_thread.SPECS,
}
STAGES: dict[str, str] = {
    **READ_STAGES,
    **tools_sources.STAGES,
    **tools_web.STAGES,
    **tools_thread.STAGES,
}
_IMPLS: dict[str, Callable[[ChatEnv, dict[str, Any]], Awaitable[str]]] = {
    **READ_IMPLS,
    **tools_sources.IMPLS,
    **tools_web.IMPLS,
    **tools_thread.IMPLS,
}


# Tools that change something: a read repeated after them is not a repeat.
MUTATING = frozenset(
    {
        "connect_source",
        "add_document_url",
        "sync_source",
        "request_repair",
        "request_login",
        "remember",
        "forget",
    }
)


def build_chat_tools(env: ChatEnv) -> list[HarnessTool]:
    """The agent's tools bound to one turn."""

    def bind(name: str) -> HarnessTool:
        impl = _IMPLS[name]

        async def run(args: dict[str, Any]) -> str:
            try:
                text = await impl(env, args)
            except (ValueError, TypeError, KeyError) as exc:
                return f"Ошибка аргументов вызова {name}: {type(exc).__name__}."
            env.used.add(name)
            if name in OUTSIDE_TEXT:
                env.outside_read = True
            return text

        return HarnessTool(
            spec=TOOL_SPECS[name], run=run, stage=STAGES[name], mutates=name in MUTATING
        )

    return [bind(name) for name in TOOL_SPECS]
