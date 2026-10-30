"""Tools that read outside the database: a connected source (live), a public page, documents.

`browse_source` is a read-only GET to a page or JSON answer of a source the student connected,
with the student's own session from the Vault, only to hosts of that source's allowlist. The
session never leaves the Vault: the tool sees the page, not the cookie. A POST cannot be made
here at all, and a GET is made only to an address the agent was shown or told: the source's own
entry page and declared endpoints, its saved site map, links of pages it has already opened in
this thread, and addresses the student wrote. `fetch_public_page` opens a public HTTPS page
without any cookies, again only for a link that the student wrote or that a public result
returned, so the model cannot be talked into sending private data in a made-up address.
Everything fetched is data inside `<source_data>`, with credential-shaped values hidden.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import re
import socket
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
from sqlalchemy import func, select

from botai.agent import webtext
from botai.agent.env import ChatEnv, clean, document_link, spec
from botai.attachments import service as attachments
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import AdapterSpecRow, Attachment, Chunk, Connection, Document
from botai.learn.addresses import url_origin
from botai.learn.sitemap import get_site_map
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
# What the address itself says it does: leaving, joining, confirming, cancelling, deleting,
# voting, marking. A GET to such an address can change something on a careless site, so it is
# never made. The address (decoded, path and query) is cut into whole words at case changes and at
# `_ - . / & = ?`, so `/api/deleteItem`, `/doLogout`, `/log_out` and `/cancelBooking/5` are caught
# and `/address` or `/exam` are not.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_ACTION_WORDS = frozenset(
    {
        "logout", "signout", "signoff", "logoff", "exit", "quit", "delete", "remove", "destroy",
        "unsubscribe", "subscribe", "enrol", "enroll", "enrolment", "enrollment", "unenrol",
        "unenroll", "join", "leave", "confirm", "accept", "approve", "reject", "decline",
        "submit", "cancel", "revoke", "deactivate", "activate", "disable", "toggle", "mark",
        "vote", "register", "signup", "reset", "terminate", "drop", "withdraw", "send", "pay",
        "purchase", "buy", "publish", "ban", "kick", "purge", "wipe", "sesskey", "act",
        "action", "do",
    }
)  # fmt: skip
_ACTION_PAIRS = frozenset(
    {
        ("log", "out"), ("sign", "out"), ("log", "off"), ("sign", "off"), ("sign", "up"),
        ("opt", "out"), ("check", "out"), ("un", "subscribe"), ("un", "enrol"), ("un", "enroll"),
    }
)  # fmt: skip
# A verb glued to the rest in lower case (`logoutall`, `unsubscribeme`) is still that verb.
_ACTION_PREFIXES = ("logout", "signout", "logoff", "signoff", "unsubscribe", "unenrol")
_URL_IN_TEXT = re.compile(r"https://[^\s<>\"'`)\]]+")
_TRAILING = ".,;:!?»”…"


def question_urls(text: str) -> set[str]:
    """Links the student wrote in the current question."""
    return {
        webtext.normalize_url(m.group(0).rstrip(_TRAILING)) for m in _URL_IN_TEXT.finditer(text)
    }


def _count(env: ChatEnv, name: str, limit: int) -> bool:
    env.counters[name] = env.counters.get(name, 0) + 1
    return env.counters[name] <= limit


def _links_block(links: list[tuple[str, str]]) -> str:
    if not links:
        return ""
    return "\nСсылки на странице:\n" + "\n".join(
        f"- {webtext.mask_text(label)} — {url}" for label, url in links
    )


def _decoded(url: str) -> str:
    parts = urlsplit(url)
    text = f"{parts.path}?{parts.query}"
    for _ in range(3):  # a double-encoded verb is still a verb
        unquoted = unquote(text)
        if unquoted == text:
            break
        text = unquoted
    return text


def looks_like_action(url: str) -> bool:
    """Whether the address names an action rather than a page (see `_ACTION_WORDS`)."""
    words = [w.lower() for w in _WORD.findall(_decoded(url))]
    return any(w in _ACTION_WORDS or w.startswith(_ACTION_PREFIXES) for w in words) or any(
        pair in _ACTION_PAIRS for pair in itertools.pairwise(words)
    )


def _exact_key(url: str) -> str:
    """The address with its query, the order of parameters aside: what a page showed as a link."""
    parts = urlsplit(url)
    query = "&".join(sorted(f"{k}={v}" for k, v in parse_qsl(parts.query, keep_blank_values=True)))
    return f"{_path_key(url)}?{query}" if query else _path_key(url)


def _path_key(url: str) -> str:
    parts = urlsplit(url)
    path = unquote(parts.path).rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc.lower()}{path}"


def _route_matches(route: str, origin: str, url: str) -> bool:
    """A saved route (`/courses/{id}` or `/schedule#/week`) against the path of an address."""
    path = route.partition("#")[0] or "/"
    pattern = "".join(
        "[^/]+" if part == "{id}" else re.escape(part) for part in re.split(r"(\{id\})", path)
    )
    return re.fullmatch(pattern, _path_key(url).removeprefix(origin).rstrip("/") or "/") is not None


async def _declared_pages(
    env: ChatEnv, connection_id: str, url: str
) -> tuple[bool, Connection | None]:
    """Whether the source itself points at this address: its entry page, its declared endpoints,
    its saved site map. Query values are free (another week, another page number)."""
    async with env.services.db() as session:
        conn = await session.scalar(
            select(Connection).where(
                Connection.id == connection_id, Connection.user_id == env.user_id
            )
        )
        if conn is None:
            return False, None
        want = _path_key(url)
        known = {_path_key(conn.origin + "/")}
        if conn.entry_url:
            known.add(_path_key(conn.entry_url))
        recipe_paths: list[str] = []
        if conn.spec_id:
            row = await session.get(AdapterSpecRow, conn.spec_id)
            recipe = (row.spec if row else {}).get("recipe") or {}
            for endpoint in recipe.get("endpoints", []):
                request = endpoint.get("request") or {}
                if request.get("method", "GET") == "GET" and isinstance(request.get("path"), str):
                    recipe_paths.append(request["path"])
                ui = endpoint.get("uiUrl") or {}
                if isinstance(ui.get("path"), str):
                    recipe_paths.append(ui["path"])
        known.update(_path_key(conn.origin + p.partition("?")[0]) for p in recipe_paths)
        if want in known:
            return True, conn
        sitemap = await get_site_map(session, conn.origin)
    routes = [
        *(r for r in (sitemap or {}).get("routes", []) if isinstance(r, str)),
        *(
            n["path"]
            for n in (sitemap or {}).get("navigation", [])
            if isinstance(n, dict) and isinstance(n.get("path"), str)
        ),
        *(
            e["path"]
            for e in (sitemap or {}).get("endpoints", [])
            if isinstance(e, dict) and isinstance(e.get("path"), str)
        ),
    ]
    return any(_route_matches(r, conn.origin, url) for r in routes), conn


# --- browse_source ------------------------------------------------------------------------------


def _needs_login(title: str) -> str:
    return (
        f"Результат: needs_login. Сессия «{title}» истекла или не подходит для этой страницы. "
        "Предложите студенту войти заново и вызовите request_login, если он согласен."
    )


async def _refuse_action_hop(request: httpx.Request) -> None:
    """A page may answer with a redirect to an address that does something (`/logout`): every hop
    is judged like the address the agent asked for, before it is requested."""
    if looks_like_action(str(request.url)):
        raise BotaiError(
            ErrorCode.EGRESS_BLOCKED,
            "Источник перенаправил на адрес, похожий на действие (выход, запись, удаление); "
            "такие адреса не открываются",
        )


async def browse_source(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn_meta = (await env.titles()).get(connection_id)
    if conn_meta is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    url = webtext.normalize_url(str(args.get("url", "")))
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return "Результат: не выполнено. Нужен обычный HTTPS-адрес страницы источника."
    if looks_like_action(url):
        return (
            "Результат: не выполнено. Адрес похож на действие (выход, запись, подтверждение, "
            "удаление), а не на чтение страницы; такие адреса не открываются."
        )
    if webtext.has_secret_address(url):
        return "Результат: не выполнено. В адресе есть ключ сессии или токен; так страницы не открываются."
    if not _count(env, "browse", MAX_BROWSES):
        return (
            f"Результат: не выполнено. За один ответ можно открыть не больше {MAX_BROWSES} страниц."
        )
    async with env.services.db() as session:
        info = await load_conn_info(session, env.user_id, connection_id)
    host = parts.hostname.lower()
    if host not in {h.lower() for h in info.allow_hosts}:
        return (
            f"Результат: не выполнено. Адрес {host} не входит в разрешённые для "
            f"«{conn_meta.title}»; открываются только страницы самого источника."
        )
    declared, _ = await _declared_pages(env, connection_id, url)
    opened = (
        declared
        or _exact_key(url) in {_exact_key(u) for u in env.seen.source}
        or url in env.question_links
    )
    if not opened:
        return (
            "Результат: не выполнено. Открываются страницы, на которые указывает сам источник: "
            "его входная страница и карта разделов (site_map), ссылки уже открытых страниц "
            "и адреса, которые написал студент. Начните с входной страницы источника."
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
        async with SourceClient(
            sctx, auth_type="session", request_guard=_refuse_action_hop
        ) as client:
            response = await client.request("GET", url, auth_binding=binding)
    except SessionExpired:
        return _needs_login(conn_meta.title)
    except BotaiError as exc:
        return f"Результат: ошибка. {exc.detail}."
    if response.status_code >= 400:
        return f"Результат: ошибка. Источник ответил HTTP {response.status_code}."
    content_type = response.headers.get("content-type", "").lower()
    links: list[tuple[str, str]] = []
    # JSON is JSON whatever the server calls it: an answer sent as text/html or text/plain still
    # gets the outline with its credentials hidden, not the raw text.
    outline = webtext.json_text(response.content, declared="json" in content_type)
    if outline is not None:
        body, title = outline, "JSON"
    elif "json" in content_type:
        return "Результат: ошибка. Ответ объявлен как JSON, но не разобран."
    elif "html" in content_type or response.content.lstrip()[:15].lower().startswith(b"<!doctype"):
        if webtext.has_login_form(response.content):
            return _needs_login(conn_meta.title)  # a site without 401 answers an expired session so
        extracted = await asyncio.to_thread(extract, response.content, "html")
        body = webtext.data_body(extracted.text)
        title = extracted.title or parts.path or host
        allowed = {h.lower() for h in info.allow_hosts}
        links = [
            (label, link)
            for label, link in webtext.page_links(response.content, url)
            if (urlsplit(link).hostname or "").lower() in allowed
        ]
        env.seen.remember("source", (webtext.normalize_url(link) for _, link in links))
    elif content_type.startswith("text/"):
        body = webtext.data_body(response.text)
        title = parts.path or host
    else:
        return (
            f"Результат: не показано. Тип ответа {content_type or 'неизвестен'} читается не здесь."
        )
    env.outside_read = True
    key = env.sources.add(
        title=f"{conn_meta.title}: {clean(title, 120)}", url=url, kind="page", fetched_at=now
    )
    return (
        f"Страница источника «{conn_meta.title}» {url} [{key}], получена сейчас сессией студента. "
        f"Название: {webtext.mask_text(clean(title, 160))}\n{body}{_links_block(links)}"
    )


# --- site_map -----------------------------------------------------------------------------------


async def site_map(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    async with env.services.db() as session:
        row = await session.scalar(
            select(Connection).where(
                Connection.id == connection_id, Connection.user_id == env.user_id
            )
        )
        sitemap = await get_site_map(session, row.origin) if row else None
    if row is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    lines = [
        f"Карта разделов «{conn.title}» ({row.origin}). Адреса можно открыть через browse_source."
    ]
    if row.entry_url:
        lines.append(f"Входная страница данных: {row.entry_url}")
    if not sitemap:
        lines.append("Карта пока не собрана: исследование источника её ещё не сохранило.")
        return "\n".join(lines)
    navigation = sitemap.get("navigation") or []
    if navigation:
        lines.append("Разделы:")
        lines.extend(
            f"- {webtext.mask_text(str(n.get('label', '')))} — {row.origin}{n.get('path', '')}"
            for n in navigation
            if isinstance(n, dict)
        )
    routes = [r for r in sitemap.get("routes") or [] if isinstance(r, str)]
    if routes:
        lines.append("Маршруты (`{id}` — подставляется нужное значение): " + ", ".join(routes[:40]))
    endpoints = sitemap.get("endpoints") or []
    if endpoints:
        lines.append("Поиск и FAQ:")
        lines.extend(
            f"- {e.get('kind')}: {row.origin}{e.get('path', '')}"
            + (f" (параметры: {', '.join(e.get('queryKeys') or [])})" if e.get("queryKeys") else "")
            for e in endpoints
            if isinstance(e, dict)
        )
    env.sources.add(
        title=f"{conn.title}: карта разделов",
        url=f"botai://connections/{connection_id}",
        kind="connection",
        fetched_at=None,
    )
    return "\n".join(lines)


# --- fetch_public_page --------------------------------------------------------------------------


def _www_pair(host: str) -> tuple[str, ...]:
    """A site and its `www.` twin: the redirect between them stays on the same site."""
    return (host, host[4:]) if host.startswith("www.") else (host, f"www.{host}")


def _private_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    return not ip.is_global or ip.is_multicast


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _resolves_to_public_hosts(hosts: tuple[str, ...]) -> bool:
    """Without the egress proxy (a development box) nothing else keeps a page from naming a
    private address: resolve first and refuse if any of the site's hosts is not a public one."""
    for name in hosts:
        try:
            addresses = await _resolve(name)
        except OSError:
            continue  # it does not resolve: the fetch itself will say so
        if any(_private_address(address) for address in addresses):
            return False
    return True


async def fetch_public_page(env: ChatEnv, args: dict[str, Any]) -> str:
    raw = str(args.get("url", "")).strip()
    try:
        host = check_url(raw)
    except BotaiError as exc:
        return f"Результат: не выполнено. {exc.detail}."
    url = webtext.normalize_url(raw)
    if url not in env.question_links and url not in {
        webtext.normalize_url(u) for u in env.seen.public
    }:
        return (
            "Результат: не выполнено. Открываются только ссылки, которые написал студент в этом "
            "вопросе, или ссылки из публичной выдачи и открытых публичных страниц этого чата."
        )
    if not _count(env, "public_page", MAX_PUBLIC_PAGES):
        return (
            f"Результат: не выполнено. За один ответ можно открыть не больше "
            f"{MAX_PUBLIC_PAGES} публичных страниц."
        )
    source, _ = source_for_url(raw)
    hosts = _www_pair(host)
    proxied = bool(env.services.settings.egress_proxy_url)
    if not proxied and env.transport is None and not await _resolves_to_public_hosts(hosts):
        return "Результат: не выполнено. Адрес ведёт во внутреннюю сеть, а не на публичный сайт."
    now = env.services.clock.now()
    sctx = SyncContext(
        user_id=env.user_id,
        connection_id="",
        origin=f"https://{host}",
        allow_hosts=hosts,
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
    env.outside_read = True
    links: list[tuple[str, str]] = []
    if fmt == "html":
        links = webtext.page_links(data, raw)
        env.seen.remember("public", (webtext.normalize_url(link) for _, link in links))
    title = extracted.title or urlsplit(raw).path or host
    key = env.sources.add(title=clean(title, 160), url=raw, kind="page", fetched_at=now)
    return (
        f"Недоверенная публичная страница {raw} [{key}], открыта без входа. "
        f"Название: {webtext.mask_text(clean(title, 160))}\n"
        f"{webtext.data_body(extracted.text)}{_links_block(links)}"
    )


# --- read_document ------------------------------------------------------------------------------


def _slice(full: str, offset: int, limit: int) -> tuple[str, str]:
    piece = full[offset : offset + limit]
    end = offset + len(piece)
    more = f" Дальше: offset={end}." if end < len(full) else " Это конец документа."
    return piece, f"показано {offset}–{end}.{more}"


async def read_document(env: ChatEnv, args: dict[str, Any]) -> str:
    """A saved document of the student, or a file of a chat message (its text, once indexed or
    straight from the file)."""
    document_id = str(args.get("document_id", "")).strip()
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(500, min(int(args.get("max_chars", READ_CHARS) or READ_CHARS), READ_CHARS_MAX))
    async with env.services.db() as session:
        doc = await session.scalar(
            select(Document).where(Document.id == document_id, Document.user_id == env.user_id)
        )
        att = (
            None
            if doc is not None
            else await session.scalar(
                select(Attachment).where(
                    Attachment.id == document_id, Attachment.user_id == env.user_id
                )
            )
        )
        if doc is None and att is None:
            return "Такого документа у студента нет. Возьмите document_id из результата поиска."
        if doc is None and att is not None:
            if att.kind != "document":
                what = (
                    "изображение"
                    if att.kind == "image"
                    else "аудио"
                    if att.kind == "audio"
                    else "файл"
                )
                return (
                    f"Вложение «{clean(att.name, 120)}» — {what}: распознавание пока недоступно, "
                    "прочитать содержимое я не могу. Скажите об этом студенту честно."
                )
            extract_ = await attachments.extract_text(session, env.user_id, att.id, 400_000)
            if extract_.error:
                return f"Вложение не прочитано: {extract_.error}."
            full = extract_.text
            if not full:
                return f"В вложении «{clean(att.name, 120)}» нет текста."
            piece, where = _slice(full, offset, limit)
            key = env.sources.add(
                title=att.name,
                url=f"botai://attachments/{att.id}",
                kind="document",
                fetched_at=att.created_at,
            )
            env.outside_read = True
            return (
                f"Вложение «{clean(att.name, 160)}» botai://attachments/{att.id} [{key}]: "
                f"{where}\n---\n{webtext.mask_text(piece)}"
            )
        assert doc is not None
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
    if not texts:
        return (
            f"У документа «{clean(doc.title, 120)}» пока нет прочитанного текста: "
            "индексация ещё идёт или не удалась."
        )
    full = "\n\n".join(texts)
    piece, where = _slice(full, offset, limit)
    key = env.sources.add(
        title=doc.title,
        url=doc.url or document_link(doc.id),
        kind="page" if doc.kind == "page" else "document",
        fetched_at=doc.fetched_at,
    )
    env.outside_read = True
    return (
        f"Документ «{clean(doc.title, 160)}» {document_link(doc.id)} [{key}]: "
        f"знаков {int(total or 0)}, {where}\n---\n{webtext.mask_text(piece)}"
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
    env.seen.remember("public", (webtext.normalize_url(hit.url) for hit in response.results))
    for hit in response.results:
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
        "только чтение (GET) и только страницы, на которые указывает сам источник — его входная "
        "страница, карта разделов (site_map), ссылки уже открытых страниц и адреса из сообщения "
        "студента. Для другой недели расписания или страницы меняйте значения в запросе страницы "
        "из карты разделов или входной страницы. Результат — недоверенный текст страницы со ссылками. Если сессия истекла, "
        "вернёт needs_login: предложите войти (request_login).",
        {
            "connection_id": {"type": "string"},
            "url": {"type": "string", "description": "HTTPS-адрес на хосте этого источника"},
        },
        ["connection_id", "url"],
    ),
    "site_map": spec(
        "site_map",
        "Сохранённая карта разделов подключённого источника: навигация, маршруты приложения, "
        "поиск и FAQ. Адреса оттуда можно открыть через browse_source.",
        {"connection_id": {"type": "string"}},
        ["connection_id"],
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
        "Читать текст документа или вложения сообщения кусками: document_id из результата поиска, "
        "ссылки botai://documents/… или id вложения из контекста; offset и max_chars (до 20000) — "
        "для продолжения.",
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
    "site_map": "Смотрю карту разделов",
    "fetch_public_page": "Открываю публичную страницу",
    "read_document": "Читаю документ",
    "search_public_web": "Ищу публичные источники",
}
IMPLS = {
    "browse_source": browse_source,
    "site_map": site_map,
    "fetch_public_page": fetch_public_page,
    "read_document": read_document,
    "search_public_web": search_public_web,
}
