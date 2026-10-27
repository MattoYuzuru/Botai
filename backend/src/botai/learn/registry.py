"""The learned university registry: origins and the warning about third-party clients (Q12).

The row itself is created by the space (`spaces.universities`); the learning agent adds what only
it can find out: the origins that belong to the university and, if the site says so, that
third-party access is discouraged. The user is warned and can connect anyway.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.llm import LlmProvider
from botai.db import models as m
from botai.learn import prompting
from botai.learn.author import parse_json
from botai.learn.redact import looks_like_injection
from botai.sources.extract import clean_text, collapse

log = logging.getLogger(__name__)

# Cheap pre-filter: the model is asked only when the pages mention the topic at all.
WARNING_HINTS = re.compile(
    r"(?i)сторонн\w*\s+(?:прилож|клиент|программ|сервис)|third[- ]party|автоматизированн\w+\s+"
    r"(?:сбор|доступ|запрос)|неофициальн\w+\s+(?:прилож|клиент)|парсинг|scraping|"
    r"запрещ[её]н\w*\s+(?:использовани|доступ)"
)
MAX_WARNING = 300
_URL = re.compile(r"(?i)https?://|www\.|@")


def mentions_third_party_rules(text: str) -> bool:
    return WARNING_HINTS.search(text) is not None


def clean_warning(value: object) -> str | None:
    """The model's one-sentence warning, or None if it is empty, long, or tries to instruct."""
    if not isinstance(value, str):
        return None
    text = clean_text(collapse(value), MAX_WARNING)
    if len(text) < 8 or _URL.search(text) or looks_like_injection(text):
        return None
    return text


async def find_warning(llm: LlmProvider, page_text: str) -> tuple[str | None, int]:
    """Returns the warning (if the site has one) and the tokens spent."""
    if not mentions_third_party_rules(page_text):
        return None, 0
    response = await llm.complete(prompting.registry_request(page_text))
    u = response.usage
    spent = u.input_cache_miss + u.input_cache_hit + u.output
    try:
        data = parse_json(response.content)
    except ValueError:
        return None, spent
    return clean_warning(data.get("warning") if isinstance(data, dict) else None), spent  # pyright: ignore[reportUnknownMemberType]


async def update_university(
    session: AsyncSession, university_id: str | None, origin: str, warning: str | None
) -> None:
    """Add the origin to the university and, once, the warning. Never overwrites a warning."""
    if not university_id:
        return
    uni = await session.get(m.University, university_id)
    if uni is None:
        return
    if origin and origin not in (uni.origins or []):
        uni.origins = [*(uni.origins or []), origin]
    if warning and not uni.third_party_warning:
        uni.third_party_warning = warning
