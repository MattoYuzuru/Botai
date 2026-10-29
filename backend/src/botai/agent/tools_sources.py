"""Tools for universities and sources: what is connected, connecting, repairing, logging in.

Every action starts a task in the current thread (`jobs.thread_id`), or reports the thread where
the same work already runs, as a `botai://threads/{id}` link. A source is never connected twice:
the same origin yields its status and links without starting anything. The tools take no
password, code or cookie; those are entered in the protected fields of the app.
"""

from __future__ import annotations

import re
from typing import Any, get_args
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent import scope
from botai.agent.env import (
    JOB_STATE_RU,
    STATUS_RU,
    ChatEnv,
    clean,
    connection_link,
    document_link,
    fmt_updated,
    spec,
    thread_link,
)
from botai.contracts.agent_ui import TaskBlock
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db.models import Connection, Document, Job, Space, University
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import TASK_KINDS
from botai.jobs.tasks import task_title
from botai.learn.addresses import url_origin
from botai.sources import service as documents_service
from botai.sources.ingest import check_url
from botai.sources.schemas import DocumentFromUrl
from botai.spaces import repo, service, universities
from botai.spaces.conversations import (
    intent_of,
    normalize_context,
    reject_secret_links,
    safe_url,
)
from botai.spaces.schemas import ConnectionKind

NO_REPAIR_STATUSES = {"needs_reauth": "нужен повторный вход человеком", "disabled": "отключено"}
KINDS: frozenset[str] = frozenset(get_args(ConnectionKind))
_HOSTNAME = re.compile(r"^(?:https?://)?[\w-]+(?:\.[\w-]+)+(?::\d+)?/?$", re.IGNORECASE)
TITLE_CHARS = 120
_NEEDS_TITLE = (
    "Результат: не запущено. Укажите человеческое название источника в title "
    "(например «Единый личный кабинет», «Smart LMS», «Список группы»), а не адрес сайта."
)


# --- shared pieces ------------------------------------------------------------------------------


def _task_card(
    env: ChatEnv, job: Job | None, kind: str, title: str, connection_id: str | None
) -> None:
    """A card of the task under the answer; it leads to the thread the task lives in."""
    if job is None or kind not in TASK_KINDS:
        return
    env.blocks.append(
        TaskBlock(
            id=new_id(),
            at=env.services.clock.now(),
            job_id=job.id,
            kind=kind,  # type: ignore[arg-type]  # one of TASK_KINDS
            title=title[:200],
            state=job.state,  # type: ignore[arg-type]
            thread_id=job.thread_id or env.thread_id,
            connection_id=connection_id,
        )
    )


def _where(env: ChatEnv, job: Job | None) -> str:
    """Where a task is shown: this thread, or a link to the thread that owns it."""
    if job is None or job.thread_id in (None, env.thread_id):
        return "в этом чате"
    return f"в другом чате: {thread_link(str(job.thread_id))}"


async def _load_job(session: AsyncSession, user_id: str, job_id: str) -> Job | None:
    return await session.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))


def _source_line(env: ChatEnv, view: scope.SourceView, zone: Any) -> str:
    key = env.sources.add(
        title=view.title,
        url=connection_link(view.id),
        kind="connection",
        fetched_at=view.last_sync_at,
    )
    parts = [
        f"«{view.title}»",
        f"вид: {view.kind}",
        f"адрес: {view.origin}",
        f"статус: {STATUS_RU.get(view.status, view.status)}",
        f"обновлено: {fmt_updated(view.last_sync_at, zone)}",
        f"код ошибки: {view.error_code}" if view.error_code else "",
        f"connection_id={view.id}",
        f"ссылка {connection_link(view.id)}",
    ]
    line = "  - " + " | ".join(p for p in parts if p) + f" [{key}]"
    if view.task is not None:
        t = view.task
        where = f", чат {thread_link(t.thread_id)}" if t.thread_id else ""
        wait = f"; ждёт: {t.question}" if t.question else ""
        line += f"\n    задача: {t.title} — {JOB_STATE_RU.get(t.state, t.state)}{wait}{where}"
    return line


# --- universities -------------------------------------------------------------------------------


async def list_universities(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        spaces = await scope.list_scope(session, env.user_id)
    if not spaces:
        return "У студента пока нет вузов и источников."
    lines = [
        f"Вузы студента: {len(spaces)}. Ссылки botai://… можно давать студенту; "
        "university_id и connection_id служебные, студенту их не показывайте."
    ]
    for space in spaces:
        zone = await env.tz_for(space.space_id)
        head = f"«{space.title}»"
        if space.short_title and space.short_title != space.title:
            head += f" ({space.short_title})"
        if space.university_id:
            head += f" university_id={space.university_id}"
        lines.append(head + f", источников: {len(space.sources)}")
        lines.extend(_source_line(env, view, zone) for view in space.sources)
    return "\n".join(lines)


async def find_university(env: ChatEnv, args: dict[str, Any]) -> str:
    query = clean(str(args.get("query", "")), 120)
    if not query:
        return "Ошибка аргументов: нужен непустой query."
    async with env.services.db() as session:
        found = await scope.candidates(session, query)
        owned = {
            s.university_id
            for s in await session.scalars(select(Space).where(Space.user_id == env.user_id))
            if s.university_id
        }
    if not found:
        return (
            f"По запросу «{query}» в справочнике и среди выученных вузов совпадений нет. "
            "Спросите студента полное название вуза; connect_source создаст новый вуз по названию."
        )
    lines = [f"Кандидаты по запросу «{query}»: {len(found)}."]
    for cand in found:
        uni = cand.university
        match = {"exact": "точное название", "alias": "по сокращению", "part": "по части названия"}[
            cand.match
        ]
        parts = [
            f"«{uni.title}»",
            f"сокращение: {uni.short_title}" if uni.short_title != uni.title else "",
            f"также: {', '.join(cand.aliases[:4])}" if cand.aliases else "",
            f"известные адреса: {', '.join(uni.origins[:3])}" if uni.origins else "",
            "уже есть у студента" if uni.id in owned else "у студента пока нет",
            f"совпадение: {match}",
            f"university_id={uni.id}",
        ]
        lines.append("- " + " | ".join(p for p in parts if p))
    if all(c.match == "part" for c in found):
        lines.append("Точного совпадения нет: подтвердите вуз у студента, прежде чем подключать.")
    return "\n".join(lines)


# --- connect ------------------------------------------------------------------------------------


def _human_title(raw: object) -> str | None:
    title = " ".join(str(raw or "").split())[:TITLE_CHARS]
    if not title or _HOSTNAME.match(title):
        return None
    return title


async def _university(
    session: AsyncSession, env: ChatEnv, university_id: str, query: str
) -> University | str:
    """The university for a new source, or a message for the model when it must ask first."""
    if university_id:
        try:
            return await universities.find_or_create(
                session, title=university_id, university_id=university_id
            )
        except BotaiError:
            return "Результат: не запущено. Вуза с таким university_id нет: возьмите id из find_university."
    if query:
        found = await scope.candidates(session, query)
        sure = [c for c in found if c.match in ("exact", "alias")]
        if len(sure) == 1:
            return await universities.find_or_create(
                session, title=sure[0].university.title, university_id=sure[0].university.id
            )
        if len(sure) > 1 or found:
            names = "; ".join(f"«{c.university.title}» ({c.university.id})" for c in found)
            return (
                f"Результат: нужно уточнение. Запросу «{query}» подходит несколько вузов или только "
                f"по части названия: {names}. Спросите студента, какой вуз имеется в виду "
                "(offer_choices), и повторите с university_id."
            )
        return await universities.find_or_create(session, title=query)
    if env.space_id:
        space = await session.scalar(
            select(Space).where(Space.id == env.space_id, Space.user_id == env.user_id)
        )
        if space is not None and space.university_id:
            return await universities.find_or_create(
                session, title=space.title, university_id=space.university_id
            )
    owned = [
        s
        for s in await session.scalars(select(Space).where(Space.user_id == env.user_id))
        if s.university_id
    ]
    if len(owned) == 1 and owned[0].university_id:
        return await universities.find_or_create(
            session, title=owned[0].title, university_id=owned[0].university_id
        )
    return (
        "Результат: нужно уточнение. Неясно, для какого вуза источник: назовите вуз "
        "(university_query) или возьмите university_id из list_universities / find_university. "
        "Спросите студента, если он не называл вуз."
    )


async def _exists(env: ChatEnv, session: AsyncSession, conn: Connection) -> str:
    space = await session.scalar(select(Space).where(Space.id == conn.space_id))
    task = next(
        iter(
            [
                t
                for t in await scope.active_tasks(session, env.user_id)
                if t.connection_id == conn.id
            ]
        ),
        None,
    )
    key = env.sources.add(
        title=conn.title,
        url=connection_link(conn.id),
        kind="connection",
        fetched_at=conn.last_sync_at or conn.created_at,
    )
    zone = await env.tz_for(conn.space_id)
    lines = [
        f"Результат: уже подключено, ничего не запущено. «{conn.title}»"
        + (f" в «{space.title}»" if space else "")
        + f": статус {STATUS_RU.get(conn.status, conn.status)}, обновлено "
        f"{fmt_updated(conn.last_sync_at, zone)} {connection_link(conn.id)} [{key}]."
    ]
    if task is not None:
        where = f"чат {thread_link(task.thread_id)}" if task.thread_id else "без чата"
        lines.append(
            f"Задача сейчас: {task.title} — {JOB_STATE_RU.get(task.state, task.state)}, {where}."
        )
        job = await _load_job(session, env.user_id, task.job_id)
        _task_card(env, job, task.kind, task.title, conn.id)
    if conn.status == "disabled":
        lines.append(
            "Источник отключён студентом: заново подключить его через агента нельзя, "
            "он включается в списке источников приложения."
        )
        return "\n".join(lines)
    lines.append(
        "Спросите студента, что сделать: перепроверить данные сейчас (sync_source), починить "
        "(request_repair) или войти заново (request_login); предложите варианты через "
        "offer_choices. Connection_id служебный."
    )
    return "\n".join(lines)


async def connect_source(env: ChatEnv, args: dict[str, Any]) -> str:
    raw_url = args.get("url")
    if not raw_url:
        return (
            "Результат: не запущено. Нужна ссылка на источник (url). Если студент её не назвал: "
            "известные адреса вуза есть в find_university, иначе найдите официальный адрес "
            "через search_public_web или спросите студента."
        )
    try:
        url = safe_url(raw_url)
    except BotaiError as exc:
        return f"Результат: не запущено. {exc.detail}."
    title = _human_title(args.get("title"))
    if title is None:
        return _NEEDS_TITLE
    kind = str(args.get("kind_hint") or "other")
    if kind not in KINDS:
        kind = "other"
    note = clean(str(args.get("note") or ""), 500)
    origin = url_origin(url)
    host = urlsplit(url).hostname or ""

    async with env.services.db() as session:
        # Two turns asking for the same site at once must end with one source, not two.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"source-origin:{env.user_id}:{origin}", 0)
                )
            )
        )
        existing = await scope.find_source(session, env.user_id, origin)
        if existing is not None:
            return await _exists(env, session, existing)
        chosen = await _university(
            session,
            env,
            clean(str(args.get("university_id") or ""), 50),
            clean(str(args.get("university_query") or ""), 120),
        )
        if isinstance(chosen, str):
            return chosen
        space, _ = await scope.ensure_space(session, env.user_id, chosen.id)
        warning = chosen.third_party_warning
        intent = intent_of(normalize_context({"entry_urls": [url], "user_note": note}))
        conn = Connection(
            user_id=env.user_id,
            space_id=space.id,
            origin=origin,
            kind=kind,
            title=title,
            status="draft",
            allow_hosts=[host],
        )
        session.add(conn)
        await session.commit()  # the queue works in its own transaction
        space_title, connection_id = space.title, conn.id
    try:
        job_id = await env.services.queue.enqueue(
            JobKind.CONNECT,
            user_id=env.user_id,
            connection_id=connection_id,
            thread_id=env.thread_id,
            payload={"url": url, "learning_intent": intent, "source": "chat"},
            dedupe=True,
        )
    except Exception:
        async with env.services.db() as session:  # no job means nobody would finish this source
            doomed = await session.get(Connection, connection_id)
            if doomed is not None:
                await session.delete(doomed)
                await session.commit()
        raise
    env.forget_meta()
    env.used.add("connect_source")
    async with env.services.db() as session:
        job = await _load_job(session, env.user_id, job_id)
    label = task_title("connect", title)
    _task_card(env, job, "connect", label, connection_id)
    lines = [
        f"Результат: запущено. Источник «{title}» добавлен в «{space_title}» и подключается "
        f"{_where(env, job)}: ход работы, вопросы и вход появятся там "
        f"{connection_link(connection_id)}.",
        "Пароль и коды студент вводит только в защищённых полях приложения, не в чат.",
    ]
    if warning:
        lines.append(
            f"Скажите студенту дословно: «{clean(warning, 500)}» Подключение можно продолжить."
        )
    return "\n".join(lines)


async def add_document_url(env: ChatEnv, args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    try:
        check_url(url)  # plain https to a public host: a Google file id is long, but not a secret
        reject_secret_links(url)
    except BotaiError as exc:
        return f"Результат: не запущено. {exc.detail}."
    title = _human_title(args.get("title")) or ""
    async with env.services.db() as session:
        known = await session.scalar(
            select(Document).where(Document.user_id == env.user_id, Document.url == url)
        )
        if known is not None:
            return (
                f"Результат: уже добавлено, ничего не запущено. «{known.title}» "
                f"{document_link(known.id)}. Текст читается через read_document и search_documents."
            )
        reading = await session.scalar(
            select(Job).where(
                Job.user_id == env.user_id,
                Job.kind == JobKind.INGEST.value,
                Job.state.in_(sorted(IN_FLIGHT)),
                Job.payload["source"]["url"].astext == url,
            )
        )
        if reading is not None:
            _task_card(
                env, reading, "ingest", task_title("ingest", title or None), reading.connection_id
            )
            return (
                f"Результат: уже читается, второй раз не запущено: документ по этой ссылке "
                f"обрабатывается {_where(env, reading)}."
            )
        university_id = clean(str(args.get("university_id") or ""), 50)
        if university_id:
            try:
                uni = await universities.find_or_create(
                    session, title=university_id, university_id=university_id
                )
            except BotaiError:
                return "Результат: не запущено. Вуза с таким university_id нет: возьмите id из find_university."
            space, _ = await scope.ensure_space(session, env.user_id, uni.id)
        elif env.space_id:
            space = await repo.get_space(session, env.user_id, env.space_id)
        else:
            space, _ = await scope.ensure_space(session, env.user_id, None)
        try:
            started = await documents_service.start_url(
                session,
                env.services.queue,
                env.user_id,
                space.id,
                DocumentFromUrl(url=url, title=title or None),
            )
        except BotaiError as exc:
            return f"Результат: не запущено. {exc.detail}."
        job = await _load_job(session, env.user_id, started.job_id)
        if job is not None and job.thread_id is None:
            job.thread_id = env.thread_id
            await session.commit()
        space_title = space.title
    env.forget_meta()
    env.used.add("add_document_url")
    _task_card(env, job, "ingest", task_title("ingest", title or None), started.connection_id)
    return (
        f"Результат: запущено. Документ по ссылке читается {_where(env, job)}; он попадёт в "
        f"«{space_title}». Если в нём таблица, вопросы по ней придут в чат."
    )


# --- tasks --------------------------------------------------------------------------------------


async def task_status(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        mine = await scope.active_tasks(session, env.user_id, thread_id=env.thread_id)
        everywhere = await scope.active_tasks(session, env.user_id)
        recent = list(
            await session.scalars(
                select(Job)
                .where(
                    Job.user_id == env.user_id,
                    Job.thread_id == env.thread_id,
                    Job.kind.in_(TASK_KINDS),
                    Job.state.not_in(sorted(IN_FLIGHT)),
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(3)
            )
        )
        titles = {
            c.id: c.title
            for c in await session.scalars(
                select(Connection).where(
                    Connection.user_id == env.user_id,
                    Connection.id.in_([j.connection_id for j in recent if j.connection_id]),
                )
            )
        }
    lines: list[str] = []
    here = {t.job_id for t in mine}
    if mine:
        lines.append(f"Задачи этого чата: {len(mine)}.")
        for t in mine:
            wait = f"; ждёт: {t.question}" if t.question else ""
            stage = f"; этап: {t.stage}" if t.stage else ""
            lines.append(f"- {t.title} — {JOB_STATE_RU.get(t.state, t.state)}{stage}{wait}")
    elsewhere = [t for t in everywhere if t.job_id not in here]
    if elsewhere:
        lines.append(f"Задачи в других чатах: {len(elsewhere)}.")
        for t in elsewhere:
            where = thread_link(t.thread_id) if t.thread_id else "без чата"
            lines.append(f"- {t.title} — {JOB_STATE_RU.get(t.state, t.state)}, чат {where}")
    if recent:
        lines.append("Недавно завершено в этом чате:")
        for job in recent:
            lines.append(
                f"- {task_title(job.kind, titles.get(job.connection_id or ''))} — "
                f"{JOB_STATE_RU.get(job.state, job.state)}"
            )
    return "\n".join(lines) if lines else "Активных и недавних задач нет."


def _already_running(env: ChatEnv, job: Job, conn_title: str, what: str) -> str:
    return f"{what} «{conn_title}» уже идёт {_where(env, job)}; вторую запускать не нужно."


async def sync_source(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    async with env.services.db() as session:
        row = await repo.get_connection(session, env.user_id, connection_id)
        if row.adapter is None and row.spec_id is None:
            return (
                f"Обновление «{conn.title}» не запущено: источник ещё не настроен. Продолжить "
                "подключение можно через request_repair, войти — через request_login."
            )
        try:
            job_id = await service.enqueue_for_connection(
                session,
                env.services.queue,
                env.user_id,
                connection_id,
                JobKind.SYNC,
                thread_id=env.thread_id,
                payload={"source": "chat"},
            )
        except BotaiError as exc:
            return f"Обновление «{conn.title}» не запущено: {exc.detail}."
        job = await _load_job(session, env.user_id, job_id)
    env.used.add("sync_source")
    _task_card(env, job, "sync", task_title("sync", conn.title), connection_id)
    if job is not None and job.kind != JobKind.SYNC.value:
        return _already_running(env, job, conn.title, "Работа с источником")
    return (
        f"Обновление «{conn.title}» запущено {_where(env, job)}. "
        "Результат появится в чате; не обещайте срок."
    )


async def request_repair(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    reason = clean(str(args.get("reason", "")), 300)
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status in NO_REPAIR_STATUSES:
        return (
            f"Починка «{conn.title}» не запущена: {NO_REPAIR_STATUSES[conn.status]}. "
            "Это действие студента, а не автоматическая починка."
        )
    async with env.services.db() as session:
        row = await repo.get_connection(session, env.user_id, connection_id)
        setup = (
            row.adapter is None
            and row.spec_id is None
            and row.status in {"draft", "learning", "error"}
        )
        expected = JobKind.CONNECT if setup else JobKind.REPAIR
        running = await session.scalar(
            select(Job)
            .where(
                Job.user_id == env.user_id,
                Job.connection_id == connection_id,
                Job.kind == expected.value,
                Job.state.in_(sorted(IN_FLIGHT)),
            )
            .limit(1)
        )
        action = "Подключение" if setup else "Починка"
        if running is not None:
            env.repairs.append(conn.title)
            _task_card(
                env, running, expected.value, task_title(expected.value, conn.title), conn.id
            )
            return _already_running(env, running, conn.title, action)
        job_id = await service.enqueue_for_connection(
            session,
            env.services.queue,
            env.user_id,
            connection_id,
            JobKind.REPAIR,
            thread_id=env.thread_id,
            payload={"reason": reason, "source": "chat"},
        )
        job = await _load_job(session, env.user_id, job_id)
    env.repairs.append(conn.title)
    _task_card(env, job, expected.value, task_title(expected.value, conn.title), conn.id)
    queued = "поставлено" if setup else "поставлена"
    return (
        f"{action} «{conn.title}» {queued} в очередь {_where(env, job)}: ход работы и результат "
        "будут там. Не обещайте срок и не говорите, что уже исправлено."
    )


async def request_login(env: ChatEnv, args: dict[str, Any]) -> str:
    """Request the protected LoginFlow, without exposing a screen ticket or credentials."""
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status == "disabled":
        return "Подключение отключено. Защищённый вход не запущен."
    async with env.services.db() as session:
        try:
            job_id = await service.enqueue_for_connection(
                session,
                env.services.queue,
                env.user_id,
                connection_id,
                JobKind.CONNECT,
                thread_id=env.thread_id,
                payload={"manual_login": True, "source": "chat"},
                now=env.services.clock.now(),
            )
        except BotaiError as exc:
            return f"Вход в «{conn.title}» не запущен: {exc.detail}."
        job = await _load_job(session, env.user_id, job_id)
    env.used.add("request_login")
    _task_card(env, job, "connect", task_title("connect", conn.title), conn.id)
    if job is not None and not (
        job.kind == JobKind.CONNECT.value and job.payload.get("manual_login") is True
    ):
        controls = ""
        pending = (job.payload.get("dialogue") or {}).get("pending")
        if (
            isinstance(pending, dict)
            and pending.get("channel") == "native"
            and pending.get("secret")
        ):
            controls = "В текущем шаге есть защищённые поля; рядом с ними можно открыть браузер. "
        elif isinstance(pending, dict) and pending.get("channel") == "manual":
            controls = "В текущем шаге уже доступен защищённый экран. "
        return (
            f"Для «{conn.title}» уже идёт задание, состояние "
            f"{JOB_STATE_RU.get(job.state, job.state)}, {_where(env, job)}. "
            + controls
            + "Новый вход не запущен: сначала дождитесь текущего задания или остановите его "
            "в приложении, затем попросите войти снова."
        )
    return (
        f"Запрос защищённого входа «{conn.title}» принят {_where(env, job)}. "
        "Защищённые шаги и ход работы будут там. Если задание уже выполняется, продолжается "
        "оно; второй браузер не запускается. Не присылайте пароль и код в чат."
    )


# --- registry -----------------------------------------------------------------------------------

_TEXT = {"type": "string"}
SPECS = {
    "list_universities": spec(
        "list_universities",
        "Вузы студента и их источники: название, вид, адрес, статус, последняя синхронизация, "
        "ошибка, активная задача и ссылка на её чат. Вызывайте перед подключением и когда "
        "студент спрашивает, что подключено.",
        {},
        [],
    ),
    "find_university": spec(
        "find_university",
        "Найти вуз в справочнике и среди выученных: название, сокращения, известные адреса, "
        "есть ли он у студента. Если совпадение только по части названия, подтвердите вуз "
        "у студента.",
        {"query": {**_TEXT, "description": "Название, сокращение или разговорное имя вуза"}},
        ["query"],
    ),
    "connect_source": spec(
        "connect_source",
        "Подключить источник вуза (личный кабинет, LMS, расписание, сайт) в ЭТОМ чате: вход, "
        "вопросы и результат появятся здесь. Сам находит или создаёт пространство вуза. "
        "Если такой адрес уже подключён, ничего не запускает и возвращает статус и ссылки. "
        "Нужны url и человеческий title источника; вуз — university_id или university_query.",
        {
            "url": {
                **_TEXT,
                "description": "HTTPS-адрес; по возможности глубокая ссылка на раздел",
            },
            "title": {**_TEXT, "description": "Человеческое название источника, не адрес"},
            "university_id": {**_TEXT, "description": "id из find_university / list_universities"},
            "university_query": {**_TEXT, "description": "Название вуза, если id неизвестен"},
            "kind_hint": {
                **_TEXT,
                "enum": sorted(KINDS),
                "description": "Вид источника, если ясен из слов студента",
            },
            "note": {**_TEXT, "description": "Пожелание студента: что именно нужно из источника"},
        },
        ["url", "title"],
    ),
    "add_document_url": spec(
        "add_document_url",
        "Добавить публичный документ, таблицу или страницу (Google Таблицы/Документы, PDF, "
        "HTML) как источник. Уже добавленную ссылку повторно не запускает.",
        {
            "url": {**_TEXT, "description": "HTTPS-ссылка на документ"},
            "title": {**_TEXT, "description": "Название документа, если студент его назвал"},
            "university_id": {**_TEXT, "description": "Вуз документа; без него — «Мои материалы»"},
        },
        ["url"],
    ),
    "task_status": spec(
        "task_status",
        "Что сейчас делается: активные и недавние задачи этого чата и других чатов, этап, "
        "чего задача ждёт от студента, ссылки на чаты.",
        {},
        [],
    ),
    "sync_source": spec(
        "sync_source",
        "Обновить данные готового источника сейчас. connection_id — из list_universities.",
        {"connection_id": _TEXT},
        ["connection_id"],
    ),
    "request_repair": spec(
        "request_repair",
        "Запустить починку подключения, когда данные не обновляются или пропали. Только для "
        "подключений из list_universities.",
        {
            "connection_id": _TEXT,
            "reason": {**_TEXT, "description": "Коротко своими словами, что не так"},
        },
        ["connection_id", "reason"],
    ),
    "request_login": spec(
        "request_login",
        "Открыть защищённый ручной вход BotAI по явной просьбе студента войти заново, войти "
        "самому или открыть браузер для входа, и когда сессия источника истекла. Только "
        "известный connection_id своего подключения. При неоднозначности сначала уточните "
        "подключение. Существующее активное задание продолжается без второго браузера. "
        "Пароли, коды, URL и cookie сюда не передаются.",
        {"connection_id": _TEXT},
        ["connection_id"],
    ),
}
STAGES = {
    "list_universities": "Смотрю вузы и источники",
    "find_university": "Ищу вуз",
    "connect_source": "Подключаю источник",
    "add_document_url": "Добавляю документ",
    "task_status": "Проверяю задачи",
    "sync_source": "Обновляю источник",
    "request_repair": "Запускаю починку",
    "request_login": "Запрашиваю защищённый вход",
}
IMPLS = {
    "list_universities": list_universities,
    "find_university": find_university,
    "connect_source": connect_source,
    "add_document_url": add_document_url,
    "task_status": task_status,
    "sync_source": sync_source,
    "request_repair": request_repair,
    "request_login": request_login,
}
