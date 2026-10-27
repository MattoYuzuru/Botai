"""Self-refreshing facts (FR-017, ADR-0006): a confirmed answer to a procedural question is kept.

A fact is saved only when the answer was built from documents or pages: the final text cites at
least one of them and the run used `search_documents`. It is never saved for "I don't know", for
answers from structured data, or for a run that hit its budget. A fact lives `FACT_TTL`; the
`search_facts` tool marks an expired one as stale, the skill tells the model to re-check it against
the documents, and a re-checked answer lands here again through the same upsert, which renews
`confirmed_at` and `stale_after` (one row per normalised question).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select

from botai.agent.sources import DOCUMENT_KINDS, SourceRef
from botai.core.services import Services
from botai.db.models import Fact

log = logging.getLogger("botai.agent.facts")

FACT_TTL = timedelta(days=30)
MAX_QUESTION = 500
MAX_ANSWER = 3_000
_NUMBERED_CITE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
_UNKNOWN = re.compile(r"не\s+знаю", re.IGNORECASE)


def normalize_question(question: str) -> str:
    """Case, spaces and edge punctuation do not make a different question."""
    return re.sub(r"\s+", " ", question).strip(" \t\n.?!…").lower()


def strip_citations(text: str) -> str:
    """Stored answers keep no `[1]` markers: the numbers mean nothing in a later conversation."""
    return _NUMBERED_CITE.sub("", text).strip()


def is_unknown(text: str) -> bool:
    return _UNKNOWN.search(text) is not None


async def remember(
    services: Services,
    *,
    user_id: str,
    space_id: str | None,
    question: str,
    answer: str,
    cited: Sequence[SourceRef],
    now: datetime,
) -> str | None:
    """Save or renew the fact. Returns its id, or None when the answer does not qualify."""
    docs = [r for r in cited if r.kind in DOCUMENT_KINDS]
    clean = strip_citations(answer)
    key = normalize_question(question)
    if not docs or not key or not clean or is_unknown(answer):
        return None
    sources = [
        {
            "title": r.title,
            "url": r.url,
            "kind": r.kind,
            "fetchedAt": r.fetched_at.isoformat() if r.fetched_at else now.isoformat(),
        }
        for r in docs
    ]
    async with services.db() as session:
        stmt = select(Fact).where(Fact.user_id == user_id)
        stmt = stmt.where(
            Fact.space_id.is_(None) if space_id is None else Fact.space_id == space_id
        )
        existing = next(
            (
                f
                for f in (await session.execute(stmt)).scalars()
                if normalize_question(f.question) == key
            ),
            None,
        )
        fact = existing or Fact(
            user_id=user_id, space_id=space_id, question=question[:MAX_QUESTION]
        )
        fact.answer = clean[:MAX_ANSWER]
        fact.sources = sources
        fact.confirmed_at = now
        fact.stale_after = now + FACT_TTL
        fact.confidence = "confirmed"
        if existing is None:
            session.add(fact)
        await session.commit()
        return fact.id
