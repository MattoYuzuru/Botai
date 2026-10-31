"""Per-kind priority: order is kept, the first live source is primary, scope is enforced."""

from __future__ import annotations

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Source
from botai.sources.v4 import priorities, service
from tests.auth.support import Sm
from tests.sources.v4.support import Account, add_space

pytestmark = pytest.mark.db


async def make_source(sm: Sm, account: Account, *, title: str, url: str) -> Source:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            entry_url=url,
        )
        await session.commit()
        return source


async def set_active(sm: Sm, account: Account, source: Source) -> None:
    async with sm() as session:
        await service.set_status(
            session, user_id=account.user_id, source_id=source.id, status="active"
        )
        await session.commit()


async def test_order_is_kept_deduplicated_and_primary_is_the_first_live(
    tx_sessionmaker: Sm, account: Account
) -> None:
    schedule = await make_source(
        tx_sessionmaker, account, title="Расписание", url="https://lk.example.org/schedule"
    )
    calendar = await make_source(
        tx_sessionmaker, account, title="Календарь", url="https://lk.example.org/calendar"
    )
    grades = await make_source(
        tx_sessionmaker, account, title="Оценки", url="https://lk.example.org/grades"
    )
    async with tx_sessionmaker() as session:
        await priorities.set_priority(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            kind="schedule",
            source_ids=[schedule.id, schedule.id, calendar.id],
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        order = await priorities.get_priority(
            session, user_id=account.user_id, space_id=account.space_id, kind="schedule"
        )
    assert order == [schedule.id, calendar.id]

    # Draft sources are not live yet: only active/syncing/stale can be primary.
    async with tx_sessionmaker() as session:
        assert (
            await priorities.primary_source(
                session, user_id=account.user_id, space_id=account.space_id, kind="schedule"
            )
            is None
        )
    await set_active(tx_sessionmaker, account, calendar)
    await set_active(tx_sessionmaker, account, schedule)
    async with tx_sessionmaker() as session:
        primary = await priorities.primary_source(
            session, user_id=account.user_id, space_id=account.space_id, kind="schedule"
        )
    assert primary is not None
    assert primary.id == schedule.id

    # A disabled source is skipped; the next live one becomes primary.
    async with tx_sessionmaker() as session:
        await service.disable_source(session, user_id=account.user_id, source_id=schedule.id)
        await session.commit()
    async with tx_sessionmaker() as session:
        primary = await priorities.primary_source(
            session, user_id=account.user_id, space_id=account.space_id, kind="schedule"
        )
    assert primary is not None
    assert primary.id == calendar.id

    async with tx_sessionmaker() as session:
        await priorities.set_priority(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            kind="grades",
            source_ids=[grades.id],
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        assert await priorities.get_priority(
            session, user_id=account.user_id, space_id=account.space_id, kind="grades"
        ) == [grades.id]
        # An untouched kind has no order.
        assert (
            await priorities.get_priority(
                session, user_id=account.user_id, space_id=account.space_id, kind="news"
            )
            == []
        )


async def test_clearing_the_order(tx_sessionmaker: Sm, account: Account) -> None:
    source = await make_source(
        tx_sessionmaker, account, title="Источник", url="https://lk.example.org/x"
    )
    async with tx_sessionmaker() as session:
        await priorities.set_priority(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            kind="schedule",
            source_ids=[source.id],
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        await priorities.set_priority(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            kind="schedule",
            source_ids=[],
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        assert (
            await priorities.get_priority(
                session, user_id=account.user_id, space_id=account.space_id, kind="schedule"
            )
            == []
        )


async def test_priority_rejects_foreign_sources_and_unknown_kinds(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    own = await make_source(tx_sessionmaker, account, title="Свой", url="https://lk.example.org/a")
    foreign = await make_source(
        tx_sessionmaker, second_account, title="Чужой", url="https://lk.example.org/b"
    )
    second_space = await add_space(tx_sessionmaker, account, title="Третий вуз")
    async with tx_sessionmaker() as session:
        other_space_source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=second_space,
            title="Другой вуз",
            entry_url="https://other.example.org/x",
        )
        await session.commit()
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await priorities.set_priority(
                session,
                user_id=account.user_id,
                space_id=account.space_id,
                kind="schedule",
                source_ids=[foreign.id],
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await priorities.set_priority(
                session,
                user_id=account.user_id,
                space_id=account.space_id,
                kind="schedule",
                source_ids=[other_space_source.id],
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await priorities.set_priority(
                session,
                user_id=account.user_id,
                space_id=account.space_id,
                kind="telepathy",
                source_ids=[own.id],
            )
    assert exc.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await priorities.get_priority(
                session, user_id=account.user_id, space_id=second_account.space_id, kind="schedule"
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
