"""Source titles of the action journal: batching, ownership, v3 fallback, scope fallback (task-14)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from botai.actions.engine import JournalEntry
from botai.actions.journal import related_source_id, resolve_source_titles, source_title_of
from botai.db.models import ActionLog
from tests.actions.support import add_v4_source
from tests.agent.conftest import add_user
from tests.agent.seed import add_connection, add_space

pytestmark = pytest.mark.db


async def _journal_row(
    sm,  # type: ignore[no-untyped-def]
    *,
    user_id: str,
    scope_kind: str,
    scope_id: str | None = None,
    source_id: str | None = None,
) -> JournalEntry:
    row = ActionLog(
        user_id=user_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        source_id=source_id,
        action_class="write_soft",
        mode="ask",
        decision="asked",
        intent="Заявка",
    )
    async with sm() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return JournalEntry.of(row)


async def test_resolver_batches_the_users_sources(api) -> None:  # type: ignore[no-untyped-def]
    user = api.user_id
    space = await add_space(api.sm, user)
    first = await add_v4_source(api.sm, user_id=user, space_id=space, title="Кабинет ВШЭ")
    second = await add_v4_source(api.sm, user_id=user, space_id=space, title="Расписание ЦУ")

    other = await add_user(api.sm, "other")
    foreign_space = await add_space(api.sm, other)
    foreign = await add_v4_source(
        api.sm, user_id=other, space_id=foreign_space, title="Чужой кабинет"
    )

    async with api.sm() as session:
        titles = await resolve_source_titles(
            session,
            user_id=user,
            source_ids=[first, second, foreign, None, "01M68Z00000000000000000NOPE"],
        )
    assert titles == {first: "Кабинет ВШЭ", second: "Расписание ЦУ"}
    assert foreign not in titles


async def test_resolver_falls_back_to_v3_connections(api) -> None:  # type: ignore[no-untyped-def]
    user = api.user_id
    space = await add_space(api.sm, user)
    connection = await add_connection(api.sm, user, space, "Расписание (v3)")

    other = await add_user(api.sm, "other")
    other_space = await add_space(api.sm, other)
    foreign = await add_connection(api.sm, other, other_space, "Чужое подключение")

    async with api.sm() as session:
        titles = await resolve_source_titles(
            session, user_id=user, source_ids=[connection, foreign]
        )
    assert titles == {connection: "Расписание (v3)"}


async def test_resolver_without_ids_does_not_query(api) -> None:  # type: ignore[no-untyped-def]
    async with api.sm() as session:
        assert await resolve_source_titles(session, user_id=api.user_id, source_ids=[]) == {}
        assert await resolve_source_titles(session, user_id=api.user_id, source_ids=[None]) == {}


async def test_related_source_id_prefers_explicit_then_scope(api) -> None:  # type: ignore[no-untyped-def]
    user = api.user_id
    space = await add_space(api.sm, user)
    source = await add_v4_source(api.sm, user_id=user, space_id=space, title="Источник")

    explicit = await _journal_row(api.sm, user_id=user, scope_kind="global", source_id=source)
    assert related_source_id(explicit) == source

    scoped = await _journal_row(api.sm, user_id=user, scope_kind="source", scope_id=source)
    assert related_source_id(scoped) == source

    bare = await _journal_row(api.sm, user_id=user, scope_kind="site", scope_id="fakeuni.test")
    assert related_source_id(bare) is None

    titles = {source: "Источник"}
    assert source_title_of(explicit, titles) == "Источник"
    assert source_title_of(scoped, titles) == "Источник"
    assert source_title_of(bare, titles) is None


async def test_journal_rows_are_the_only_input(api) -> None:  # type: ignore[no-untyped-def]
    """No title is invented for an id that is not a source of this user."""
    user = api.user_id
    other = await add_user(api.sm, "other")
    other_space = await add_space(api.sm, other)
    foreign = await add_v4_source(api.sm, user_id=other, space_id=other_space, title="Чужой")
    entry = await _journal_row(api.sm, user_id=user, scope_kind="source", scope_id=foreign)
    async with api.sm() as session:
        titles = await resolve_source_titles(session, user_id=user, source_ids=[foreign])
        assert (
            await session.scalar(select(ActionLog.id).where(ActionLog.id == entry.id)) is not None
        )
    assert source_title_of(entry, titles) is None
