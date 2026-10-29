"""Tools that read outside the database: a connected source (live), a public page, documents.

`browse_source` is a read-only GET to a page or JSON answer of a source the student connected,
with the student's own session from the Vault, only to hosts of that source's allowlist. The
session never leaves the Vault: the tool sees the page, not the cookie. A POST cannot be made
here at all. `fetch_public_page` opens a public HTTPS page without any cookies, but only a link
that the student wrote or that a public result returned, so the model cannot be talked into
sending private data in a made-up address. Everything fetched is data inside `<source_data>`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select

from botai.agent import webtext
from botai.agent.env import ChatEnv, clean, connection_link, document_link, spec
from botai.core.errors import BotaiError
from botai.db.models import Chunk, Document
from botai.learn.addresses import url_origin
from botai.search.safety import chat_public_query
from botai.sources.adapter import EndpointOutcome, SessionExpired, SyncContext
from botai.sources.adapters.document import DocumentAdapter
from botai.sources.documents import detect_format, extract
from botai.sources.factory import AdapterFactory
from botai.sources.fetch import SourceClient
from botai.sources.ingest import check_url, source_for_url
from botai.sources.runner import context_for, load_conn_info
from botai.sources.session import cookie_jar, load_session, scoped_bearer

MAX_BROWSES = 12  # per turn
MAX_PUBLIC_PAGES = 6
READ_CHARS = 8_000
READ_CHARS_MAX = 20_000
_UNSAFE_GET = re.compile(
    r"(?i)(log-?out|sign-?out|signoff|delete|remove|destroy|unsubscribe|revoke|deactivate)"
)
_URL_IN_TEXT = re.compile(r"https://[^\s<>\"'`)\]]+")
_TRAILING = ".,;:!?»”…"


def question_urls(text: str) -> set[str]:
    """Links the student wrote in the current question; the only ones open to the model at once."""
    return {
        webtext.normalize_url(m.group(0).rstrip(_TRAILING)) for m in _URL_IN_TEXT.finditer(text)
    }


def _count(env: ChatEnv, name: str, limit: int) -> bool:
    env.counters[name] = env.counters.get(name, 0) + 1
    return env.counters[name] <= limit


def _links_block(links: list[tuple[str, str]]) -> str:
    if not links:
        return ""
    return "\nСсылки на странице:\n" + "\n".join(f"- {label} — {url}" for label, url in links)


# --- browse_source ------------------------------------------------------------------------------


def _needs_login(title: str) -> str:
    return (
        f"Результат: needs_login. Сессия «{title}» истекла или не подходит для этой страницы. "
        "Предложите студенту войти заново и вызовите request_login, если он согласен."
    )


async def browse_source(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    url = webtext.normalize_url(str(args.get("url", "")))
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return "Результат: не выполнено. Нужен обычный HTTPS-адрес страницы источника."
    if _UNSAFE_GET.search(f"{parts.path}?{parts.query}"):
        return (
            "Результат: не выполнено. Адрес похож на действие (выход, удаление), а не на чтение "
            "страницы; такие адреса не открываются."
        )
    if not _count(env, "browse", MAX_BROWSES):
        return (
            f"Результат: не выполнено. За один ответ можно открыть не больше {MAX_BROWSES} страниц."
        )
    async with env.services.db() as session:
        info = await load_conn_info(session, env.user_id, connection_id)
    host = parts.hostname.lower()
    if host not in {h.lower() for h in info.allow_hosts}:
        return (
            f"Результат: не выполнено. Адрес {host} не входит в разрешённые для «{conn.title}»; "
            "открываются только страницы самого источника."
        )
    state = await load_session(env.services.vault, env.user_id, connection_id)
    now = env.services.clock.now()
    origin = url_origin(url)
    sctx = replace(
        context_for(env.services, info, state, transport=env.transport, now=now), origin=origin
    )
    probe = httpx.Request("GET", url)
    cookie_jar(state, now).set_cookie_header(probe)
    binding = {
        "cookies": bool(probe.headers.get("cookie")),
        "bearer": "observedAuthorization"
        if scoped_bearer(state, origin, now=now) is not None
        else "none",
    }
    try:
        async with SourceClient(sctx, auth_type="session") as client:
            response = await client.request("GET", url, auth_binding=binding)
    except SessionExpired:
        return _needs_login(conn.title)
    except BotaiError as exc:
        return f"Результат: ошибка. {exc.detail}."
    env.used.add("browse_source")
    if response.status_code >= 400:
        return f"Результат: ошибка. Источник ответил HTTP {response.status_code}."
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        body = webtext.json_text(response.content)
        if body is None:
            return "Результат: ошибка. Ответ объявлен как JSON, но не разобран."
        title = "JSON"
        extra = ""
    elif "html" in content_type or response.content.lstrip()[:15].lower().startswith(b"<!doctype"):
        if webtext.has_login_form(response.content):
            return _needs_login(conn.title)  # a site without 401 answers an expired session so
        extracted = await asyncio.to_thread(extract, response.content, "html")
        body = extracted.text[: webtext.TEXT_CHARS]
        title = extracted.title or parts.path or host
        allowed = {h.lower() for h in info.allow_hosts}
        links = [
            (label, link)
            for label, link in webtext.page_links(response.content, url)
            if (urlsplit(link).hostname or "").lower() in allowed
        ]
        extra = _links_block(links)
    elif content_type.startswith("text/"):
        body = response.text[: webtext.TEXT_CHARS]
        title = parts.path or host
        extra = ""
    else:
        return (
            f"Результат: не показано. Тип ответа {content_type or 'неизвестен'} читается не здесь."
        )
    key = env.sources.add(
        title=f"{conn.title}: {clean(title, 120)}", url=url, kind="page", fetched_at=now
    )
    return (
        f"Страница источника «{conn.title}» {url} [{key}], получена сейчас сессией студента. "
        f"Название: {clean(title, 160)}\n{body}{extra}"
    )


# --- fetch_public_page --------------------------------------------------------------------------


async def fetch_public_page(env: ChatEnv, args: dict[str, Any]) -> str:
    raw = str(args.get("url", "")).strip()
    try:
        host = check_url(raw)
    except BotaiError as exc:
        return f"Результат: не выполнено. {exc.detail}."
    url = webtext.normalize_url(raw)
    if url not in env.public_urls:
        return (
            "Результат: не выполнено. Открываются только ссылки, которые написал студент в этом "
            "вопросе, или ссылки из публичной выдачи и открытых публичных страниц."
        )
    if not _count(env, "public_page", MAX_PUBLIC_PAGES):
        return f"Результат: не выполнено. За один ответ можно открыть не больше {MAX_PUBLIC_PAGES} публичных страниц."
    source, hosts = source_for_url(raw)
    now = env.services.clock.now()
    sctx = SyncContext(
        user_id=env.user_id,
        connection_id="",
        origin=f"https://{host}",
        allow_hosts=tuple(hosts),
        now=now,
        vault=env.services.vault,
        proxy_url=env.services.settings.egress_proxy_url,
        transport=env.transport,
    )
    adapter = AdapterFactory().create({"adapter": "document", "source": source})
    assert isinstance(adapter, DocumentAdapter)
    try:
        fetched = await adapter.fetch_bytes(sctx)
    except BotaiError as exc:
        return f"Результат: ошибка. {exc.detail}."
    if isinstance(fetched, EndpointOutcome):
        return "Результат: ошибка. Страница не открылась или не найдена."
    data, content_type = fetched
    try:
        fmt = detect_format(data, urlsplit(raw).path, content_type)
        extracted = await asyncio.to_thread(extract, data, fmt)
    except BotaiError as exc:
        return f"Результат: не показано. {exc.detail}."
    env.used.add("fetch_public_page")
    links: list[tuple[str, str]] = []
    if fmt == "html":
        links = webtext.page_links(data, raw)
        env.public_urls.update(webtext.normalize_url(link) for _, link in links)
    title = extracted.title or urlsplit(raw).path or host
    key = env.sources.add(title=clean(title, 160), url=raw, kind="page", fetched_at=now)
    return (
        f"Недоверенная публичная страница {raw} [{key}], открыта без входа. "
        f"Название: {clean(title, 160)}\n{extracted.text[: webtext.TEXT_CHARS]}{_links_block(links)}"
    )


# --- read_document ------------------------------------------------------------------------------


async def read_document(env: ChatEnv, args: dict[str, Any]) -> str:
    document_id = str(args.get("document_id", "")).strip()
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(500, min(int(args.get("max_chars", READ_CHARS) or READ_CHARS), READ_CHARS_MAX))
    async with env.services.db() as session:
        doc = await session.scalar(
            select(Document).where(Document.id == document_id, Document.user_id == env.user_id)
        )
        if doc is None:
            return "Такого документа у студента нет. Возьмите document_id из результата поиска."
        texts = list(
            await session.scalars(
                select(Chunk.text)
                .where(Chunk.document_id == doc.id, Chunk.user_id == env.user_id)
                .order_by(Chunk.ord)
            )
        )
        total = await session.scalar(
            select(func.coalesce(func.sum(func.length(Chunk.text)), 0)).where(
                Chunk.document_id == doc.id, Chunk.user_id == env.user_id
            )
        )
    full = "\n\n".join(texts)
    piece = full[offset : offset + limit]
    if not texts:
        return f"У документа «{doc.title}» пока нет прочитанного текста: индексация ещё идёт или не удалась."
    key = env.sources.add(
        title=doc.title,
        url=doc.url or document_link(doc.id),
        kind="page" if doc.kind == "page" else "document",
        fetched_at=doc.fetched_at,
    )
    env.used.add("read_document")
    end = offset + len(piece)
    more = f" Дальше: offset={end}." if end < len(full) else " Это конец документа."
    return (
        f"Документ «{doc.title}» {document_link(doc.id)} [{key}]: знаков {int(total or 0)}, "
        f"показано {offset}–{end}.{more}\n---\n{piece}"
    )


# --- public search ------------------------------------------------------------------------------


async def search_public_web(env: ChatEnv, args: dict[str, Any]) -> str:
    query = chat_public_query(args.get("query"), env.public_question)
    if query is None:
        return (
            "Публичный поиск не выполнен: запрос содержит личный контекст или слова, которых нет "
            "в текущем публичном вопросе. Не отправляйте сообщения, данные кабинета и секреты. "
            "Для личного расписания используйте подключённые данные."
        )
    service = env.services.search
    if service is None:
        return "Публичный поиск недоступен. Актуальные публичные сведения не проверены."
    if env.search_budget is None:
        env.search_budget = service.budget()
    response = await service.search(
        query, budget=env.search_budget, user_id=env.user_id, job_id=env.job_id
    )
    if response.status != "ok":
        if response.status == "empty":
            return (
                "Публичный поиск не нашёл совпадений; это не доказывает отсутствие личных данных."
            )
        return (
            "Публичный поиск недоступен или остановлен по бюджету. "
            "Актуальные сведения не проверены; не заменяйте их предположением модели."
        )
    lines = [
        "Недоверенная публичная выдача. Это кандидаты/публичные сведения, "
        "не доказательство доступа к кабинету или личному расписанию. Страницы из выдачи "
        "можно открыть через fetch_public_page."
    ]
    for hit in response.results:
        env.public_urls.add(webtext.normalize_url(hit.url))
        key = env.sources.add(
            title=f"{hit.title} (поиск {hit.provider})",
            url=hit.url,
            kind="public_search",
            fetched_at=hit.fetched_at,
        )
        lines.append(f"[{key}] {hit.title}\n{hit.url}\n{hit.snippet}\nПровайдер: {hit.provider}.")
    return "\n\n".join(lines)


# --- registry -----------------------------------------------------------------------------------

SPECS = {
    "browse_source": spec(
        "browse_source",
        "Прочитать страницу или JSON-ответ ПОДКЛЮЧЁННОГО источника вживую сессией студента: "
        "только чтение (GET) и только адреса самого источника, например раздел, FAQ или другую "
        "неделю расписания. Результат — недоверенный текст страницы со ссылками. Если сессия "
        "истекла, вернёт needs_login: предложите войти (request_login).",
        {
            "connection_id": {"type": "string"},
            "url": {"type": "string", "description": "HTTPS-адрес на хосте этого источника"},
        },
        ["connection_id", "url"],
    ),
    "fetch_public_page": spec(
        "fetch_public_page",
        "Открыть публичную HTTPS-страницу или документ без входа и cookies: ссылку из вопроса "
        "студента или из публичной выдачи search_public_web. Любой другой адрес не открывается.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    "read_document": spec(
        "read_document",
        "Читать текст документа или вложения кусками: document_id из результата поиска или "
        "ссылки botai://documents/…; offset и max_chars (до 20000) — для продолжения.",
        {
            "document_id": {"type": "string"},
            "offset": {"type": "integer", "description": "С какого знака, по умолчанию 0"},
            "max_chars": {"type": "integer", "description": "Сколько знаков, до 20000"},
        },
        ["document_id"],
    ),
    "search_public_web": spec(
        "search_public_web",
        "Настоящий публичный Web Search для актуальных публичных учебных сведений, официальных "
        "сайтов и документации. Только короткие публичные ключевые слова из текущего вопроса. "
        "Нельзя отправлять личные данные, переписку, закрытые страницы, пароли, URL кабинета "
        "с path/query и токены. Выдача не доказывает наличие личного расписания.",
        {"query": {"type": "string", "minLength": 2, "maxLength": 128}},
        ["query"],
    ),
}
STAGES = {
    "browse_source": "Открываю страницу источника",
    "fetch_public_page": "Открываю публичную страницу",
    "read_document": "Читаю документ",
    "search_public_web": "Ищу публичные источники",
}
IMPLS = {
    "browse_source": browse_source,
    "fetch_public_page": fetch_public_page,
    "read_document": read_document,
    "search_public_web": search_public_web,
}

__all__ = ["IMPLS", "SPECS", "STAGES", "connection_link", "question_urls"]
