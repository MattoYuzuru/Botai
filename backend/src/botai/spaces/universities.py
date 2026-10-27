"""The learned university registry (FR-032): rows appear when a student names a university.

Nothing here knows a concrete university. A row is found by an explicit id, by a shared origin,
or by the slug of its name; otherwise it is created with a deterministic accent hue (styleguide
§2: the window 235..345 keeps it away from the status colours).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import University

HUE_MIN = 235.0
HUE_MAX = 345.0
SLUG_MAX = 50
SHORT_TITLE_MAX = 16

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}  # fmt: skip


def slugify(title: str) -> str:
    """Stable id of a university name: case, spacing and punctuation do not matter."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in title.casefold())
    slug = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")
    if not slug:
        slug = "university-" + hashlib.sha256(title.encode()).hexdigest()[:8]
    if len(slug) > SLUG_MAX:
        tail = hashlib.sha256(slug.encode()).hexdigest()[:6]
        slug = slug[: SLUG_MAX - 7].rstrip("-") + "-" + tail
    return slug


def accent_hue_for(slug: str) -> float:
    """Deterministic hue in [235.0, 345.0], one decimal, from the slug."""
    steps = int((HUE_MAX - HUE_MIN) * 10) + 1
    h = int.from_bytes(hashlib.sha256(slug.encode()).digest()[:4], "big")
    return HUE_MIN + (h % steps) / 10


def short_title_for(title: str) -> str:
    title = " ".join(title.split())
    if len(title) <= SHORT_TITLE_MAX:
        return title
    initials = "".join(w[0].upper() for w in re.findall(r"[^\W\d_]{3,}", title))
    return initials[:SHORT_TITLE_MAX] if len(initials) >= 2 else title[:SHORT_TITLE_MAX]


def origin_of(url: str) -> str:
    """`scheme://host[:port]` of an http(s) URL; anything else is a validation error.

    The registry is shared between users, so credentials, paths and queries never get in.
    """
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BotaiError(ErrorCode.VALIDATION, "Нужна ссылка вида https://example.org")
    if parts.username or parts.password:
        raise BotaiError(ErrorCode.VALIDATION, "В ссылке не должно быть логина и пароля")
    try:
        port = parts.port
    except ValueError:
        raise BotaiError(ErrorCode.VALIDATION, "В ссылке некорректный порт") from None
    host = parts.hostname.lower()
    default = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port and port != default else ""
    return f"{parts.scheme}://{host}{suffix}"


async def get_university(session: AsyncSession, university_id: str) -> University:
    uni = await session.get(University, university_id)
    if uni is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Вуз не найден")
    return uni


async def find_or_create(
    session: AsyncSession,
    *,
    title: str,
    university_id: str | None = None,
    homepage_url: str | None = None,
) -> University:
    """Registry row for a new space. Never mutates an existing row (it is shared)."""
    if university_id:
        return await get_university(session, university_id)

    origin = origin_of(homepage_url) if homepage_url else None
    if origin:
        by_origin = await session.scalar(
            select(University).where(University.origins.contains([origin])).limit(1)
        )
        if by_origin is not None:
            return by_origin

    clean = " ".join(title.split())
    slug = slugify(clean)
    existing = await session.get(University, slug)
    if existing is not None:
        return existing

    # Two students naming the same university at once: the loser of the race reads the winner's row.
    await session.execute(
        pg_insert(University)
        .values(
            id=slug,
            title=clean,
            short_title=short_title_for(clean),
            origins=[origin] if origin else [],
            accent_hue=accent_hue_for(slug),
            accent_verified=False,
        )
        .on_conflict_do_nothing(index_elements=[University.id])
    )
    created = await session.get(University, slug, populate_existing=True)
    assert created is not None
    return created


async def search(session: AsyncSession, q: str, *, limit: int = 20) -> list[University]:
    """Substring search over the (small) registry. Done in Python: case folding of Cyrillic in
    the database depends on its locale, `str.casefold` does not."""
    rows = (await session.execute(select(University).order_by(University.title))).scalars().all()
    needle = " ".join(q.casefold().split())
    if needle:
        rows = [
            u
            for u in rows
            if needle in u.title.casefold()
            or needle in u.short_title.casefold()
            or needle in u.id
            or any(needle in o for o in u.origins)
        ]
    return list(rows[:limit])
