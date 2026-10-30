"""Where to log in when the student named a university and gave no address (FR-038).

The name comes from the catalog or the registry, never from free text: only a canonical or plainly
institutional title becomes a public query. Two queries are asked, one for the official portal and
one for the schedule and personal cabinet, because a deep link ("/schedule") lets the research
start where the data is. Search hits are untrusted citations: they are offered to the student as
choices and become a source only when the student picks one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from botai.search.safety import institutional_search_query, public_query, safe_result_url
from botai.search.service import SearchResult

MAX_CANDIDATES = 4
MAX_PER_HOST = 2
_SUFFIX = " официальный сайт портал"
_DEEP = re.compile(
    r"(?i)schedule|timetable|calendar|raspis|расписан|cabinet|kabinet|кабинет|student|lms|\blk\b|"
    r"(?:^|[./-])lk(?:$|[./-])"
)


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    url: str
    provider: str

    @property
    def label(self) -> str:
        parts = urlsplit(self.url)
        return (parts.hostname or "") + (parts.path.rstrip("/") if parts.path != "/" else "")


def entry_queries(title: str) -> list[str]:
    """The portal query first, then the schedule/cabinet one; empty for a non-institutional title."""
    portal = institutional_search_query(title)
    if portal is None:
        return []
    name = portal.removesuffix(_SUFFIX)
    cabinet = public_query(f"{name} расписание кабинет студента")
    return [portal, *([cabinet] if cabinet else [])]


def _score(url: str) -> int:
    parts = urlsplit(url)
    deep = any(parts.path.strip("/").split("/")) and parts.path.strip("/") != ""
    return int(deep) + 2 * bool(_DEEP.search((parts.hostname or "") + parts.path))


def rank_entries(results: Iterable[SearchResult]) -> list[EntryCandidate]:
    """Deep links to the schedule or cabinet before site roots; distinct pages, a few per host."""
    seen: set[tuple[str, str]] = set()
    pool: list[EntryCandidate] = []
    for hit in results:
        url = safe_result_url(hit.url)
        if url is None:
            continue
        parts = urlsplit(url)
        key = ((parts.hostname or "").lower(), parts.path.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        pool.append(EntryCandidate(url, hit.provider))
    pool.sort(key=lambda candidate: -_score(candidate.url))  # stable: search order breaks ties
    per_host: dict[str, int] = {}
    chosen: list[EntryCandidate] = []
    for candidate in pool:
        host = urlsplit(candidate.url).hostname or ""
        if per_host.get(host, 0) >= MAX_PER_HOST:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        chosen.append(candidate)
        if len(chosen) == MAX_CANDIDATES:
            break
    return chosen
