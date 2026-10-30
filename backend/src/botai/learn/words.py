"""What research found, tried and could not do, in plain words for the student.

The executor and the checks speak in codes (`evidence_query_mismatch`, `empty_result`,
`step_budget`). A student is told what was seen, what was tried, why it did not work out and what
can be done, never a code and never a value of the source. Every phrase here is static: nothing
from a page, an answer of the cabinet or the model reaches the text except page titles that the
observation already redacted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from botai.contracts.specs import Kind
from botai.sources.vocab import KIND_LABELS

# Why the look around the cabinet stopped before it was done (`Observations.stop_reason`).
STOPS: dict[str, str] = {
    "step_budget": "закончились шаги просмотра за один проход",
    "page_budget": "достигнут предел страниц за один проход",
    "request_budget": "достигнут предел запросов за один проход",
    "timeout": "вышло время, отведённое на просмотр",
    "token_budget": "закончился лимит на разбор этого кабинета",
    "planner_unavailable": (
        "помощник, который выбирает страницы, сейчас недоступен, поэтому открыла их по порядку меню"
    ),
    "provider_failed": "помощник, который выбирает страницы, не ответил",
    "invalid_plan": "помощник выбрал следующий шаг неразборчиво, и я остановилась",
    "no_progress": "последние шаги не открыли ничего нового",
    "navigation_failed": "часть страниц не открылась",
    "network_log_failed": "не удалось записать, какие запросы делает страница",
    "data_request_failed": "часть запросов данных кабинет отклонил или не выполнил",
    "authentication": "кабинет снова попросил войти",
}

# Why a kind of data was not connected: the code of the check that refused it, in words.
REASONS: dict[str, str] = {
    # the description does not repeat what the page itself sent
    "evidence_query_mismatch": (
        "описание запроса не совпало с тем, что отправляла сама страница (параметры адреса)"
    ),
    "evidence_path_mismatch": "адрес запроса в описании не совпал с адресом, который открывала страница",
    "evidence_path_literal_replaced": (
        "в адрес запроса подставили постороннее значение вместо слова, по которому кабинет "
        "сам узнаёт вас"
    ),
    "evidence_unknown": "описание опиралось на запрос, которого страница не отправляла",
    # the check of the description itself
    "schema": "описание не соответствует формату",
    "field.unknown": "в описании поля, которых нет в словаре этого вида данных",
    "field.missing_required": "в описании нет обязательных полей (название, время)",
    "identity.missing": "в описании нет способа отличить одну запись от другой",
    "observed.value": "в описание попало значение из ваших данных, а оно должно быть общим",
    "data.invalid": "части описания противоречат друг другу",
    # the trial read on your session
    "response_status": "кабинет ответил на запрос ошибкой или «не найдено»",
    "response_format": "ответ кабинета оказался не такой формы, как в описании",
    "items_path_missing": "в ответе нет списка, который описан",
    "selector_missing": "на странице нет описанных элементов",
    "required_fields_missing": "в записях ответа нет обязательных полей",
    "normalization_rejected": "записи прочитались, но в них нет названия или времени",
    "empty_result": "запрос выполнился, но записей в ответе нет",
    "limit": "ответ оказался слишком большим для одного чтения",
    "no_progress": "кабинет повторял один и тот же ответ",
    "unsupported_capability": "такое чтение страницы пока не поддерживается",
    "session_expired": "кабинет не принял вход для этого запроса",
    "auth_missing": "кабинет не принял вход для этого запроса",
    "auth_rejected": "кабинет не принял вход для этого запроса",
    "auth_scope_mismatch": "кабинет не принял вход для этого запроса",
    "login_redirect": "кабинет не принял вход для этого запроса",
    "transport_timeout": "кабинет не ответил вовремя",
    "rate_limited": "кабинет ограничил частоту запросов",
    # the model
    "no_answer": "модель не вернула пригодный разбор",
    "not_found": "в просмотренных страницах и ответах эти данные не видны",
    "not_reached": "просмотр закончился раньше, чем дошла до этого вида данных",
    "token_budget": "закончился лимит на разбор",
}
# A family of codes with one meaning for the student.
FAMILIES: tuple[tuple[str, str], ...] = (
    ("evidence_", "описание опиралось на запрос страницы, который не подтверждён"),
    ("window_", "обход по неделям описан неверно"),
    ("browser_", "чтение отрисованной страницы описано неверно"),
    ("factory_", "в описании поля, которые задаёт только сервер"),
    ("template.", "в адресе запроса неверный шаблон"),
    ("param.", "в параметрах запроса недопустимое имя"),
    ("pointer.", "описание ведёт к полю, которое читать нельзя"),
    ("personal.", "в описание попало личное значение, а оно должно быть общим"),
    ("url.", "адрес запроса недопустим"),
    ("pagination.", "обход страниц описан неверно"),
)
GENERIC = "описание не прошло проверку"
MAX_REASONS = 2


def reason(code: str) -> str:
    if text := REASONS.get(code):
        return text
    for prefix, family in FAMILIES:
        if code.startswith(prefix):
            return family
    return GENERIC


def why(codes: Iterable[str]) -> str:
    """Up to two different reasons, the latest first (a later attempt knew more). "No records" is
    what every failed read ends in, so it is told only when nothing more specific is known."""
    distinct = list(dict.fromkeys(codes))
    distinct = [code for code in distinct if code != "empty_result"] or distinct
    seen: list[str] = []
    for code in reversed(distinct):
        text = reason(code)
        if text not in seen:
            seen.append(text)
    return "; ".join(seen[:MAX_REASONS]) or "разобрать не удалось"


def stop(stop_reason: str) -> str:
    return STOPS.get(stop_reason, "просмотр остановился раньше, чем всё было просмотрено")


def label(kind: Kind) -> str:
    return KIND_LABELS[kind].lower()


def labels(kinds: Iterable[Kind]) -> str:
    return ", ".join(label(kind) for kind in kinds)


_ERROR_PAGE = re.compile(r"(?i)not found|\b404\b|\b50\d\b|forbidden|ошибка|не найден")


def page_name(url: str, title: str) -> str:
    """A page for a human: its title, else the path (no query, no address of the site). A page
    that only said "not found" was not a place to look at."""
    name = " ".join(title.split())[:40] or urlsplit(url).path[:40]
    return f"«{name}»" if name and not _ERROR_PAGE.search(name) else ""


def seen(pages: Sequence[str], kinds: Sequence[Kind]) -> str:
    """What research had in front of it, as one sentence (empty when it saw nothing)."""
    parts: list[str] = []
    if pages:
        parts.append("открывала " + ", ".join(pages[:4]))
    if kinds:
        parts.append("данные видны: " + labels(kinds))
    return ("Что нашла: " + "; ".join(parts) + ".") if parts else ""


def remedy(*, can_continue: bool = False) -> str:
    """What the student can do. Never an instruction to retry what the machine can do itself."""
    tail = ", или написать «подключи ещё раз»" if can_continue else ""
    return f"Можно прислать ссылку на страницу, где это видно{tail}."
