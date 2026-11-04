"""Deleting a v4 source: the honest counterpart of reversible switching off.

The owner's rule (docs/v4/03-sources-and-ux.md §1.5): a source is *switched off* (data hidden,
nothing deleted) or *deleted* (its data and secrets are deleted). This module implements the
second, and only the second — switching off stays `service.disable_source`.

What goes with the source:

* `source_artifacts` and `source_state` — `ON DELETE CASCADE` on the `sources` row;
* the normalized product rows the source wrote (`schedule_events`, `courses`, `assignments`,
  `deadlines`, `grades`, `people`, `locations`, `news_items`, `profile_fields`,
  `ranking_entries`) — its data must not survive as provenance-less rows that no read can see
  anyway;
* the panels that hang on it: an agent's panel built for this source goes with it, a system panel
  goes when no live source of the space has its data any more (D-039, `panels.service`);
* its id in every `source_priorities` list of the space (the setting keeps its order otherwise).

What stays on purpose:

* `jobs` and `agent_runs`/`agent_run_events` — the run history and traces outlive the source
  (they carry the payload, not a foreign key); a deleted source's runs remain auditable;
* `playbooks`/`playbook_versions` — learned reading knowledge is shared between sources and the
  second account (D-018); deleting a source never destroys what was learned;
* the RAG corpus derived from artifacts (`documents`/`chunks`): embeddings point at the source with
  `ON DELETE SET NULL`, so search keeps its text with the provenance honestly lost. Cleaning the
  derived index is a separate bounded reindex task, not part of a synchronous delete.

A source with an in-flight task (queued/running/waiting `connect`/`sync`/`repair`/`ingest`) is not
deleted: 409 tells the owner to wait instead of pulling the record from under a running job.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    Assignment,
    Course,
    Deadline,
    Grade,
    Location,
    NewsItem,
    Person,
    ProfileField,
    RankingEntry,
    ScheduleEvent,
    Source,
    SourceArtifact,
    SourcePriority,
    SourceState,
)
from botai.panels import service as panels_service
from botai.sources.v4 import service
from botai.sources.v4.sync_jobs import find_in_flight

DOMAIN_MODELS = (
    Assignment,
    Course,
    Deadline,
    Grade,
    Location,
    NewsItem,
    Person,
    ProfileField,
    RankingEntry,
    ScheduleEvent,
)
"""The normalized rows a v4 source writes (`sources/v4/domain.py`); deleted with the source."""

_CASCADE_TABLES = (SourceArtifact, SourceState)
"""Rows that hang on the source by a foreign key and are deleted by `ON DELETE CASCADE`; listed so
the intent is visible and a schema drift is caught by `tests/db/test_schema.py`."""


async def delete_source(session: AsyncSession, *, user_id: str, source_id: str) -> None:
    """Delete one source of the user with its data; a foreign/missing id is 404, a busy one 409."""
    source = await service.get_source(session, user_id=user_id, source_id=source_id)
    running = await find_in_flight(session, user_id=user_id, source=source)
    if running is not None:
        raise BotaiError(
            ErrorCode.CONFLICT,
            "Источник сейчас выполняет задачу: дождитесь её окончания и удалите снова",
            extra={"jobId": running},
        )
    await _clean_priorities(session, source)
    await delete_source_rows(session, user_id=user_id, source_ids=[source.id])
    # Before the row goes: the foreign key of a panel to its source is SET NULL.
    await panels_service.prune_after_source_removal(
        session, user_id=user_id, space_id=source.space_id, source_id=source.id
    )
    # The row itself: artifacts and state go by ON DELETE CASCADE; jobs and traces keep no
    # foreign key to it, playbooks stay as learned knowledge.
    await session.delete(source)
    await session.flush()


async def delete_source_rows(
    session: AsyncSession, *, user_id: str, source_ids: Sequence[str]
) -> None:
    """Delete every normalized row the given sources of the user wrote (all domain tables).

    A source goes by `delete_source`; the sources of a deleted space go through here before the
    space row, whose cascade would otherwise leave their rows behind with `source_id` NULL."""
    if not source_ids:
        return
    for model in DOMAIN_MODELS:
        await session.execute(
            delete(model).where(model.user_id == user_id, model.source_id.in_(list(source_ids)))
        )


async def _clean_priorities(session: AsyncSession, source: Source) -> None:
    """Drop the source from the ordered lists of its space (the rest of the order is untouched)."""
    rows = list(
        await session.scalars(
            select(SourcePriority).where(SourcePriority.space_id == source.space_id)
        )
    )
    for row in rows:
        remaining = [source_id for source_id in row.source_ids if source_id != source.id]
        if len(remaining) != len(row.source_ids):
            row.source_ids = remaining
    await session.flush()


__all__ = ["DOMAIN_MODELS", "delete_source", "delete_source_rows"]
