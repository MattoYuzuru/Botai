"""Tools for universities and sources: what is connected, connecting, repairing, logging in.

Every action starts a task in the current thread (`jobs.thread_id`), or reports the thread where
the same work already runs, as a `botai://threads/{id}` link. A source is never connected twice:
the same origin yields its status and links without starting anything. The tools take no
password, code or cookie; those are entered in the protected fields of the app.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.env import (
    JOB_STATE_RU,
    STATUS_RU,
    ChatEnv,
    clean,
    connection_link,
    data_text,
    document_link,
    fmt_updated,
    spec,
    thread_link,
)
from botai.agent.tasks import TaskView, active_tasks
from botai.contracts.agent_ui import TaskBlock
from botai.contracts.specs import Kind
from botai.core.calendar_link import is_calendar_service_page
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.db.models import Connection, Document, Job, Space, University
from botai.jobs.events import IN_FLIGHT
from botai.jobs.queue import TASK_KINDS, enqueue_in_session
from botai.jobs.tasks import task_title
from botai.learn.intent import LearningIntent, merge_intent
from botai.sources import calendar
from botai.sources import service as documents_service
from botai.sources.ingest import check_url
from botai.sources.schemas import DocumentFromUrl
from botai.sources.vocab import KIND_LABELS
from botai.spaces import repo, service, universities, views
from botai.spaces.conversations import (
    intent_of,
    normalize_context,
    reject_secret_links,
    safe_url,
)
from botai.spaces.views import SourceView

NO_REPAIR_STATUSES = {"needs_reauth": "нужен повторный вход человеком", "disabled": "отключено"}
EXACT = 1.0  # universities.search: the name or a known short name matched whole
TITLE_CHARS = 120
_CALENDAR_SERVICE = (
    "Результат: не запущено. Это страница календаря в браузере (Яндекс, Google, Outlook, iCloud), "
    "а не кабинет вуза: входить в неё через браузер нельзя, источник из неё не делается. Календарь "
    "подключается личной ссылкой экспорта (iCal): без пароля и входа на сайт, ссылку студент вводит "
    "в защищённое поле. Скажите студенту, где её взять:\n"
    + calendar.GUIDE_STEPS
    + "\nЗатем, когда студент согласится, вызовите connect_calendar (не connect_source): поле "
    "для ссылки откроется в этом чате. Саму ссылку просить в чат нельзя."
)
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


# --- universities -------------------------------------------------------------------------------


async def list_universities(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        universities_ = await service.list_sources(session, env.user_id)
        tasks = {
            t.connection_id: t
            for t in reversed(await active_tasks(session, env.user_id))
            if t.connection_id
        }
    if not universities_:
        return "У студента пока нет вузов и источников."
    lines = [
        f"Вузы студента: {len(universities_)}. Ссылки botai://… можно давать студенту; "
        "university_id и connection_id служебные, студенту их не показывайте."
    ]
    for entry in universities_:
        zone = await env.tz_for(entry.space.id)
        head = f"«{data_text(entry.space.title)}»"
        if entry.short_title and entry.short_title != entry.space.title:
            head += f" ({data_text(entry.short_title, 40)})"
        if entry.space.university_id:
            head += f" university_id={entry.space.university_id}"
        lines.append(head + f", источников: {len(entry.sources)}")
        for source in entry.sources:
            lines.append(_source_line(env, source, tasks.get(source.connection.id), zone))
    return "\n".join(lines)


def _source_line(env: ChatEnv, view: SourceView, task: TaskView | None, zone: Any) -> str:
    conn = view.connection
    key = env.sources.add(
        title=view.title,
        url=connection_link(conn.id),
        kind="connection",
        fetched_at=conn.last_sync_at,
    )
    parts = [
        f"«{data_text(view.title)}»",
        f"вид: {conn.kind}",
        f"адрес: {conn.origin}",
        f"статус: {STATUS_RU.get(conn.status, conn.status)}",
        f"обновлено: {fmt_updated(conn.last_sync_at, zone)}",
        f"код ошибки: {conn.error_code}" if conn.error_code else "",
        f"connection_id={conn.id}",
        f"ссылка {connection_link(conn.id)}",
    ]
    line = "  - " + " | ".join(p for p in parts if p) + f" [{key}]"
    if task is not None:
        where = f", чат {thread_link(task.thread_id)}" if task.thread_id else ""
        wait = f"; ждёт: {task.question}" if task.question else ""
        line += f"\n    задача: {data_text(task.title)} — {JOB_STATE_RU.get(task.state, task.state)}{wait}{where}"
    return line


async def find_university(env: ChatEnv, args: dict[str, Any]) -> str:
    query = clean(str(args.get("query", "")), 120)
    if not query:
        return "Ошибка аргументов: нужен непустой query."
    async with env.services.db() as session:
        found = await universities.search(session, query, limit=5)
        owned = {
            s.university_id for s in await repo.list_spaces(session, env.user_id) if s.university_id
        }
        rows = {
            u.id: u
            for u in await session.scalars(
                select(University).where(University.id.in_([c.id for c in found]))
            )
        }
    if not found:
        return (
            f"По запросу «{query}» в справочнике и среди выученных вузов совпадений нет. "
            "Спросите студента полное название вуза; connect_source создаст новый вуз по названию."
        )
    lines = [f"Кандидаты по запросу «{query}»: {len(found)}."]
    for cand in found:
        known = rows.get(cand.id)
        parts = [
            f"«{cand.title}»",
            f"сокращение: {cand.short_title}" if cand.short_title != cand.title else "",
            f"также: {', '.join(cand.aliases[:4])}" if cand.aliases else "",
            f"известные адреса: {', '.join(known.origins[:3])}" if known and known.origins else "",
            "уже есть у студента" if cand.id in owned else "у студента пока нет",
            "совпадение: точное (название или сокращение)"
            if cand.score >= EXACT
            else "совпадение: по части названия",
            f"university_id={cand.id}",
        ]
        lines.append("- " + " | ".join(p for p in parts if p))
    if not any(c.score >= EXACT for c in found):
        lines.append("Точного совпадения нет: подтвердите вуз у студента, прежде чем подключать.")
    return "\n".join(lines)


# --- connect ------------------------------------------------------------------------------------


def _human_title(raw: object) -> str | None:
    title = " ".join(str(raw or "").split())[:TITLE_CHARS]
    if not title or views.looks_like_address(title):
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
            return (
                "Результат: не запущено. Вуза с таким university_id нет: "
                "возьмите id из find_university."
            )
    if query:
        if views.looks_like_address(query):
            return "Результат: не запущено. Назовите вуз словами, а не адресом сайта."
        found = await universities.search(session, query, limit=5)
        sure = [c for c in found if c.score >= EXACT]
        if len(sure) == 1:
            return await universities.find_or_create(
                session, title=sure[0].title, university_id=sure[0].id
            )
        if sure or found:
            names = "; ".join(f"«{c.title}» ({c.id})" for c in found)
            return (
                f"Результат: нужно уточнение. Запросу «{query}» подходит несколько вузов или только "
                f"по части названия: {names}. Спросите студента, какой вуз имеется в виду "
                "(offer_choices), и повторите с university_id."
            )
        return await universities.find_or_create(session, title=query)
    spaces = [s for s in await repo.list_spaces(session, env.user_id) if s.university_id]
    own = next((s for s in spaces if s.id == env.space_id), None)
    if own is None and len(spaces) == 1:
        own = spaces[0]
    if own is not None and own.university_id:
        return await universities.find_or_create(
            session, title=own.title, university_id=own.university_id
        )
    return (
        "Результат: нужно уточнение. Неясно, для какого вуза источник: назовите вуз "
        "(university_query) или возьмите university_id из list_universities / find_university. "
        "Спросите студента, если он не называл вуз."
    )


# A source that never finished connecting (cancelled, failed or abandoned before its recipe): the
# student asking again means «finish it», not «choose what to do with it».
_UNFINISHED = frozenset({"draft", "learning", "error"})


async def _previous_intent(session: AsyncSession, conn: Connection) -> LearningIntent | None:
    """What the newest connect task of this source was asked to learn, as the student and the
    research left it (the pages and kinds named in the dialogue, what the student excluded)."""
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
    try:
        stored = service.same_origin_intent(
            previous.payload.get("learning_intent") if previous else None, conn.origin
        )
    except BotaiError:
        return None  # an earlier intent that no longer passes the checks is not worth keeping
    return LearningIntent.restore(stored) if stored is not None else None


def _wanted_text(intent: LearningIntent) -> str:
    """What is being connected, in the words of the student."""
    if intent.discovery_mode != "requested":
        return "то, что найдётся в кабинете"
    return ", ".join(KIND_LABELS[kind].lower() for kind in intent.requested_kinds)


async def _exists_or_resume(
    env: ChatEnv,
    session: AsyncSession,
    conn: Connection,
    intent: dict[str, Any],
    url: str | None,
) -> str:
    busy = any(t.connection_id == conn.id for t in await active_tasks(session, env.user_id))
    if busy or conn.spec_id is not None or conn.status not in _UNFINISHED:
        return await _exists(env, session, conn)
    # The task continues what the student wants NOW: this ask is merged into what the earlier
    # attempt was told, so a new wish (tasks as well as the timetable) is never lost to an old note
    # and a page or an exclusion from the earlier dialogue is not forgotten either.
    fresh = LearningIntent.restore(intent)
    stale = await _previous_intent(session, conn)
    merged = merge_intent(stale, fresh) if stale is not None else fresh
    payload: dict[str, Any] = {
        "learning_intent": {**merged.checkpoint(), "discovery_mode": merged.discovery_mode},
        "source": "chat",
    }
    if url is not None:
        payload["url"] = url
    job = await enqueue_in_session(
        session,
        JobKind.CONNECT,
        user_id=env.user_id,
        connection_id=conn.id,
        thread_id=env.thread_id,
        payload=payload,
        now=env.services.clock.now(),
        dedupe=True,
    )
    await session.commit()
    title = views.display_title(conn)
    env.forget_meta()
    _task_card(env, job, "connect", task_title("connect", title), conn.id)
    return (
        f"Результат: продолжено. Источник «{data_text(title)}» уже был добавлен, но подключение "
        f"не завершилось — оно запущено снова {_where(env, job)} {connection_link(conn.id)}. "
        f"Читается: {_wanted_text(merged)}. Скажите студенту, что продолжаете прежнее "
        "подключение и что именно читается; вход и вопросы появятся в чате."
    )


async def _exists(env: ChatEnv, session: AsyncSession, conn: Connection) -> str:
    space = await session.scalar(select(Space).where(Space.id == conn.space_id))
    task = next(
        (t for t in await active_tasks(session, env.user_id) if t.connection_id == conn.id), None
    )
    title = views.display_title(conn)
    key = env.sources.add(
        title=title,
        url=connection_link(conn.id),
        kind="connection",
        fetched_at=conn.last_sync_at or conn.created_at,
    )
    zone = await env.tz_for(conn.space_id)
    lines = [
        f"Результат: уже подключено, ничего не запущено. «{data_text(title)}»"
        + (f" в «{data_text(space.title)}»" if space else "")
        + f": статус {STATUS_RU.get(conn.status, conn.status)}, обновлено "
        f"{fmt_updated(conn.last_sync_at, zone)} {connection_link(conn.id)} [{key}]."
    ]
    if task is not None:
        where = f"чат {thread_link(task.thread_id)}" if task.thread_id else "без чата"
        lines.append(
            f"Задача сейчас: {data_text(task.title)} — {JOB_STATE_RU.get(task.state, task.state)}, "
            f"{where}."
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


def _kinds_arg(raw: object) -> list[Kind] | str:
    """The kinds of data the agent says the student asked for, or a message for the model."""
    if raw is None or raw == "":
        return []
    values = raw if isinstance(raw, list) else [raw]
    kinds: list[Kind] = []
    for value in values:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(value, str) or value not in KIND_LABELS:
            return (
                "Результат: не запущено. kinds — список видов данных из: "
                + ", ".join(KIND_LABELS)
                + "."
            )
        if value not in kinds:
            kinds.append(value)  # type: ignore[arg-type]  # a key of KIND_LABELS
    return kinds


async def connect_source(env: ChatEnv, args: dict[str, Any]) -> str:
    raw_url = args.get("url")
    url: str | None = None
    if raw_url and is_calendar_service_page(str(raw_url)):
        return _CALENDAR_SERVICE
    if raw_url:
        try:
            url = safe_url(raw_url)
        except BotaiError as exc:
            return f"Результат: не запущено. {exc.detail}."
    title = _human_title(args.get("title"))
    if title is None:
        return _NEEDS_TITLE
    kind = views.normalize_kind(args.get("kind_hint"))
    note = clean(str(args.get("note") or ""), 500)
    kinds = _kinds_arg(args.get("kinds"))
    if isinstance(kinds, str):
        return kinds
    try:
        context: dict[str, Any] = {"entry_urls": [url] if url else [], "user_note": note}
        if kinds:
            # What the student named is a request, not a hint: discovery does not widen it.
            context.update(requested_kinds=kinds, discovery_mode="requested")
        intent = intent_of(normalize_context(context))
    except BotaiError as exc:
        return f"Результат: не запущено. {exc.detail}."
    origin = universities.origin_of(url) if url else None

    async with env.services.db() as session:
        if origin is not None:
            existing = await service.find_source(session, env.user_id, origin)
            if existing is not None:
                return await _exists_or_resume(env, session, existing, intent, url)
        chosen = await _university(
            session,
            env,
            clean(str(args.get("university_id") or ""), 50),
            clean(str(args.get("university_query") or ""), 120),
        )
        if isinstance(chosen, str):
            return chosen
        # The space first, the site's lock second: the order every creator of a source keeps.
        space = await service.ensure_space(session, env.user_id, chosen.id)
        if url is not None and origin is not None:
            await service.lock_origin(session, env.user_id, origin)
            # Two turns asking for one site at once end with one source: the second finds the first.
            existing = await service.find_source(session, env.user_id, origin)
            if existing is not None:
                return await _exists_or_resume(env, session, existing, intent, url)
            conn, _ = await service.ensure_source(
                session, env.user_id, space.id, url=url, kind=kind, title=title
            )
        else:
            # No address yet: the connection finds the entry page itself (public search, then the
            # student chooses) and fills the address in. A second ask for the same name waits for it.
            conn = await session.scalar(
                select(Connection).where(
                    Connection.user_id == env.user_id,
                    Connection.space_id == space.id,
                    Connection.origin == "",
                    Connection.title == title,
                )
            )
            if conn is not None:
                return await _exists(env, session, conn)
            conn = Connection(
                user_id=env.user_id,
                space_id=space.id,
                origin="",
                kind=kind,
                title=title,
                status="draft",
                allow_hosts=[],
            )
            session.add(conn)
            await session.flush()
        payload: dict[str, Any] = {"learning_intent": intent, "source": "chat"}
        if url is not None:
            payload["url"] = url
        job = await enqueue_in_session(
            session,
            JobKind.CONNECT,
            user_id=env.user_id,
            connection_id=conn.id,
            thread_id=env.thread_id,
            payload=payload,
            now=env.services.clock.now(),
            dedupe=True,
        )
        await session.commit()  # the source and its task appear together
        space_title, connection_id, warning = space.title, conn.id, chosen.third_party_warning
    env.forget_meta()
    _task_card(env, job, "connect", task_title("connect", title), connection_id)
    lines = [
        f"Результат: запущено. Источник «{title}» добавлен в «{data_text(space_title)}» и "
        f"подключается {_where(env, job)}: ход работы, вопросы и вход появятся там "
        f"{connection_link(connection_id)}.",
        "Пароль и коды студент вводит только в защищённых полях приложения, не в чат.",
    ]
    if url is None:
        lines.append(
            "Адреса не было: подключение само поищет входную страницу и спросит студента в чате."
        )
    if warning:
        lines.append(
            f"Скажите студенту дословно: «{clean(warning, 500)}» Подключение можно продолжить."
        )
    return "\n".join(lines)


async def connect_calendar(env: ChatEnv, args: dict[str, Any]) -> str:
    """The calendar of a university, by the export link of its owner: the task in this thread asks
    for the link in a protected field. The link never passes through the model."""
    title = _human_title(args.get("title")) or calendar.CALENDAR_TITLE
    replace_link = args.get("replace_link") is True
    async with env.services.db() as session:
        chosen = await _university(
            session,
            env,
            clean(str(args.get("university_id") or ""), 50),
            clean(str(args.get("university_query") or ""), 120),
        )
        if isinstance(chosen, str):
            return chosen
        space = await service.ensure_space(session, env.user_id, chosen.id)
        started = await calendar.start_calendar_source(
            session,
            user_id=env.user_id,
            thread_id=env.thread_id,
            space=space,
            title=title,
            replace_link=replace_link,
            now=env.services.clock.now(),
        )
        await session.commit()
        conn, job, space_title = started.connection, started.job, space.title
        shown = views.display_title(conn)
        if started.outcome == "exists":
            return await _calendar_exists(env, conn, shown, space_title)
    env.forget_meta()
    _task_card(env, job, "connect", task_title("connect", shown), conn.id)
    link = connection_link(conn.id)
    if started.outcome == "running":
        return (
            f"Результат: уже идёт, второй раз не запущено. Календарь «{data_text(shown)}» "
            f"ждёт ссылку {_where(env, job)} {link}. Скажите студенту, что защищённое поле для "
            "ссылки открыто там."
        )
    return (
        f"Результат: запущено. Календарь «{data_text(shown)}» добавлен в «{data_text(space_title)}» "
        f"и подключается {_where(env, job)} {link}: там покажут, где взять ссылку на экспорт "
        "календаря, и откроют защищённое поле для неё. Ссылку студент вводит только в это поле: "
        "в чат её присылать нельзя, и вам она не передаётся. Скажите студенту одной-двумя "
        "фразами, что нужна личная ссылка экспорта календаря и что поле откроется в чате."
    )


async def _calendar_exists(env: ChatEnv, conn: Connection, title: str, space_title: str) -> str:
    key = env.sources.add(
        title=title,
        url=connection_link(conn.id),
        kind="connection",
        fetched_at=conn.last_sync_at or conn.created_at,
    )
    zone = await env.tz_for(conn.space_id)
    if conn.status == "disabled":
        return (
            f"Результат: календарь «{data_text(title)}» в «{data_text(space_title)}» отключён "
            "студентом, ничего не запущено. Он включается в списке источников приложения."
        )
    return (
        f"Результат: календарь уже подключён, ничего не запущено. «{data_text(title)}» в "
        f"«{data_text(space_title)}»: статус {STATUS_RU.get(conn.status, conn.status)}, обновлено "
        f"{fmt_updated(conn.last_sync_at, zone)} {connection_link(conn.id)} [{key}]. Обновить "
        "данные сейчас — sync_source. Если ссылка перестала работать или студент хочет заменить "
        "её, вызовите connect_calendar с replace_link=true (спросите студента, offer_choices)."
    )


def _by_link(title: str) -> str:
    return (
        f"«{data_text(title)}» — календарь: он читается по личной ссылке экспорта, вход на сайт "
        "не нужен. Новая ссылка (старая не работает или её нужно заменить) — connect_calendar с "
        "replace_link=true: студент введёт её в защищённом поле. Обновить данные сейчас — "
        "sync_source."
    )


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
                f"Результат: уже добавлено, ничего не запущено. «{data_text(known.title)}» "
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
        try:
            if university_id:
                space = await service.ensure_space(session, env.user_id, university_id)
            elif env.space_id:
                space = await repo.get_space(session, env.user_id, env.space_id)
            else:
                space = await service.ensure_space(session, env.user_id, None)
            started = await documents_service.start_url(
                session,
                env.user_id,
                space.id,
                DocumentFromUrl(url=url, title=title or None),
                thread_id=env.thread_id,  # the task is the thread's from its first block
                now=env.services.clock.now(),
            )
        except BotaiError as exc:
            return f"Результат: не запущено. {exc.detail}."
        job = await _load_job(session, env.user_id, started.job_id)
        space_title = space.title
    env.forget_meta()
    _task_card(env, job, "ingest", task_title("ingest", title or None), started.connection_id)
    return (
        f"Результат: запущено. Документ по ссылке читается {_where(env, job)}; он попадёт в "
        f"«{data_text(space_title)}». Если в нём таблица, вопросы по ней придут в чат."
    )


# --- tasks --------------------------------------------------------------------------------------


async def task_status(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        mine = await active_tasks(session, env.user_id, thread_id=env.thread_id)
        everywhere = await active_tasks(session, env.user_id)
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
        if row.adapter == calendar.ADAPTER and row.spec_id is None:
            return (
                f"Обновление «{conn.title}» не запущено: ссылка на календарь ещё не передана. "
                "Откройте поле для неё: connect_calendar."
            )
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
        if row.adapter == calendar.ADAPTER:
            return _by_link(conn.title)
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
    # Native fields of BotAI by default (e-mail, password, code); the screen only when the
    # student asked to log in on the site themselves.
    on_screen = args.get("on_screen") is True
    payload: dict[str, Any] = {"source": "chat"}
    if on_screen:
        payload["manual_login"] = True
    async with env.services.db() as session:
        row = await repo.get_connection(session, env.user_id, connection_id)
        if row.adapter == calendar.ADAPTER:
            return _by_link(conn.title)
        running = {
            t.job_id for t in await active_tasks(session, env.user_id) if t.connection_id == conn.id
        }
        try:
            job_id = await service.enqueue_for_connection(
                session,
                env.services.queue,
                env.user_id,
                connection_id,
                JobKind.CONNECT,
                thread_id=env.thread_id,
                payload=payload,
                now=env.services.clock.now(),
            )
        except BotaiError as exc:
            return f"Вход в «{conn.title}» не запущен: {exc.detail}."
        job = await _load_job(session, env.user_id, job_id)
    _task_card(env, job, "connect", task_title("connect", conn.title), conn.id)
    if job is not None and job.id in running:
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
        "Нужен человеческий title источника; url — если адрес известен (без него подключение само "
        "поищет входную страницу и спросит студента); вуз — university_id или university_query. "
        "Что именно читать, передавайте в kinds, если студент это назвал. Источник, подключение "
        "которого не завершилось, этот вызов продолжает с новой просьбой: она дополняет прежнюю, "
        "а не теряется.",
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
                "enum": list(views.CONNECTION_KINDS),
                "description": "Вид источника, если ясен из слов студента",
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": list(KIND_LABELS)},
                "description": (
                    "Что студент просит читать из источника, если он это назвал: scheduleEvents — "
                    "расписание, deadlines — задания со сроками (дедлайны), assignments — условия "
                    "и описания заданий, courses — курсы, grades — оценки, news — новости, "
                    "people — преподаватели, locations — аудитории. «Расписание и задания» — "
                    "scheduleEvents и deadlines. Не называл — не передавайте: подключение само "
                    "найдёт, что есть в кабинете"
                ),
            },
            "note": {**_TEXT, "description": "Пожелание студента: что именно нужно из источника"},
        },
        ["title"],
    ),
    "connect_calendar": spec(
        "connect_calendar",
        "Подключить календарь вуза (Яндекс, Google, Outlook, любой iCal) по личной ссылке "
        "экспорта: расписание, где у вуза пары и разовые события живут в календаре. Запускает "
        "задачу в ЭТОМ чате: она покажет, где взять ссылку, и откроет защищённое поле для неё. "
        "Ссылка в чат не приходит и до вас не доходит. Календарь вуза один: если он уже "
        "подключён, ничего не запускает и возвращает статус; replace_link=true — спросить "
        "новую ссылку (старая не работает или студент хочет заменить). Вуз — university_id "
        "или university_query; без них берётся вуз этого чата или единственный вуз студента.",
        {
            "university_id": {**_TEXT, "description": "id из find_university / list_universities"},
            "university_query": {**_TEXT, "description": "Название вуза, если id неизвестен"},
            "title": {**_TEXT, "description": "Название источника; по умолчанию «Календарь»"},
            "replace_link": {
                "type": "boolean",
                "description": "Спросить новую ссылку у уже подключённого календаря",
            },
        },
        [],
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
        "Войти в источник заново или продолжить незавершённое подключение со входом: студент "
        "вводит почту, пароль и код в защищённых полях BotAI в этом чате. on_screen=true — "
        "только если студент сам просит войти на сайте на экране браузера. Только известный "
        "connection_id своего подключения; при неоднозначности сначала уточните. Существующее "
        "активное задание продолжается без второго браузера. Пароли, коды, URL и cookie сюда "
        "не передаются.",
        {"connection_id": _TEXT, "on_screen": {"type": "boolean"}},
        ["connection_id"],
    ),
}
STAGES = {
    "list_universities": "Смотрю вузы и источники",
    "find_university": "Ищу вуз",
    "connect_source": "Подключаю источник",
    "connect_calendar": "Подключаю календарь",
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
    "connect_calendar": connect_calendar,
    "add_document_url": add_document_url,
    "task_status": task_status,
    "sync_source": sync_source,
    "request_repair": request_repair,
    "request_login": request_login,
}
