"""`changed_at` of grades and tasks (docs/v4/27 §5.5): news, not every write.

Set when a row appears after the source's first sync (and after the first sync that brought the
kind), when a grade's value or a task's state changes; never when the same data is read better.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from botai.db.models import Deadline, Grade, Source
from botai.sources.v4 import domain, service
from botai.sources.v4.runner import NormalizedRecord
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

T1 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
T2 = T1 + timedelta(hours=3)


def g(ext: str, **fields: Any) -> NormalizedRecord:
    return NormalizedRecord(kind="grades", external_id=ext, fields=fields)


def d(ext: str, state: str, state_text: str | None = None) -> NormalizedRecord:
    return NormalizedRecord(
        kind="deadlines",
        external_id=ext,
        fields={"title": ext, "due_at": T1, "state": state, "state_text": state_text},
    )


async def _write(sm: Sm, account: Account, source_id: str, records: list[Any], **kw: Any) -> None:
    async with sm() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        await domain.write_records(
            session, user_id=account.user_id, source=source, records=records, **kw
        )
        await session.commit()


async def _changed(sm: Sm, model: Any, source_id: str) -> dict[str, datetime | None]:
    async with sm() as session:
        rows = await session.scalars(select(model).where(model.source_id == source_id))
        return {row.external_id: row.changed_at for row in rows}


async def test_changed_at_marks_news_only(tx_sessionmaker: Sm, account: Account) -> None:
    sm = tx_sessionmaker
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="ЛК",
            entry_url="https://fakeuni.test/learn",
            kinds=["site", "grades", "deadlines"],
        )
        await session.commit()
    first = [
        g("g-value", value="7", value_num=7.0, scale_max=10.0),
        g("g-number", value="8", value_num=None, scale_max=None),
    ]
    await _write(sm, account, source.id, first, now=T1, synced_before=False)
    assert set((await _changed(sm, Grade, source.id)).values()) == {None}

    second = [
        g("g-value", value="9", value_num=9.0, scale_max=10.0),  # the value changed: news
        g("g-number", value="8", value_num=8.0, scale_max=10.0),  # the number found: not news
        g("g-new", value="5"),  # appeared after the first sync: news
    ]
    # deadlines are read for the first time in this sync: their rows are history, not news
    tasks = [d("d-words", "unknown"), d("d-state", "open", "Задано")]
    await _write(sm, account, source.id, [*second, *tasks], now=T2, synced_before=True)
    assert await _changed(sm, Grade, source.id) == {"g-value": T2, "g-number": None, "g-new": T2}
    assert set((await _changed(sm, Deadline, source.id)).values()) == {None}

    t3 = T2 + timedelta(hours=3)
    third = [
        d("d-words", "submitted", "Отправлено"),  # words kept for the first time: not news
        d("d-state", "submitted", "Отправлено"),  # the state changed: news
        d("d-new", "open", "Задано"),  # a new task: news
    ]
    await _write(sm, account, source.id, third, now=t3, synced_before=True)
    assert await _changed(sm, Deadline, source.id) == {"d-words": None, "d-state": t3, "d-new": t3}
    # an unchanged re-read touches nothing
    await _write(sm, account, source.id, third, now=t3 + timedelta(hours=3), synced_before=True)
    assert (await _changed(sm, Deadline, source.id))["d-state"] == t3
