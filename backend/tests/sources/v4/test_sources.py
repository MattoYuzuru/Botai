"""The v4 source service: identity, kinds, CRUD, scoping, switching off and on."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Source
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

URL = "https://lk.example.org/schedule"


async def make_source(
    sm: Sm, account: Account, *, title: str = "Личный кабинет", **kwargs: Any
) -> Source:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            **kwargs,
        )
        await session.commit()
        return source


async def test_create_normalizes_identity_kinds_and_title(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(
        tx_sessionmaker,
        account,
        title="  Личный   кабинет ",
        entry_url=f"{URL}/?token=FIXTURE-SECRET&view=week",
        kinds=["site", "schedule", "schedule"],
        config={"group": "БПМ-231"},
    )
    assert source.title == "Личный кабинет"
    assert source.identity_key == "https://lk.example.org/schedule?view=week"
    assert source.entry_url == "https://lk.example.org/schedule?view=week"
    assert source.kinds == ["site", "schedule"]
    assert source.status == "draft"
    assert source.trust == "normal"
    assert source.priority == 0
    assert source.playbook_id is None
    assert source.config == {"group": "БПМ-231"}
    assert source.space_id == account.space_id
    assert source.user_id == account.user_id
    assert source.university_id == account.university_id
    assert "FIXTURE-SECRET" not in source.identity_key


async def test_two_cabinets_of_one_host_are_two_sources(
    tx_sessionmaker: Sm, account: Account
) -> None:
    first = await make_source(tx_sessionmaker, account, title="Расписание", entry_url=f"{URL}")
    second = await make_source(
        tx_sessionmaker,
        account,
        title="Задания",
        entry_url="https://lk.example.org/tasks",
    )
    assert first.identity_key != second.identity_key
    async with tx_sessionmaker() as session:
        rows = await service.list_sources(session, user_id=account.user_id)
    assert {row.id for row in rows} == {first.id, second.id}


async def test_the_same_address_twice_is_a_conflict(tx_sessionmaker: Sm, account: Account) -> None:
    await make_source(tx_sessionmaker, account, title="Первый", entry_url=URL)
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, title="Второй", entry_url=f"{URL}/")
    assert exc.value.code is ErrorCode.CONFLICT
    assert "Первый" in exc.value.detail


async def test_a_concurrent_duplicate_becomes_a_clean_conflict(
    monkeypatch: pytest.MonkeyPatch, tx_sessionmaker: Sm, account: Account
) -> None:
    """A writer that wins the unique index after the pre-check must still answer 409, not 500."""
    first = await make_source(tx_sessionmaker, account, title="Первый", entry_url=URL)
    original = service.store.source_by_identity
    calls = {"pre_check": 0}

    async def race(session: Any, space_id: str, identity_key: str) -> Any:
        calls["pre_check"] += 1
        if calls["pre_check"] == 1:
            return None  # another request inserted the same identity right after our pre-check
        return await original(session, space_id, identity_key)

    monkeypatch.setattr(service.store, "source_by_identity", race)
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, title="Второй", entry_url=URL)
    assert exc.value.code is ErrorCode.CONFLICT
    assert first.title in exc.value.detail


async def test_a_calendar_keeps_only_a_digest_of_its_private_link(
    tx_sessionmaker: Sm, account: Account
) -> None:
    link = "webcal://calendar.example.org/private/fixture-key/export.ics"
    source = await make_source(
        tx_sessionmaker, account, title="Календарь", link=link, kinds=["calendar", "schedule"]
    )
    assert source.identity_key.startswith("link:")
    assert "fixture-key" not in source.identity_key
    assert source.entry_url is None
    async with tx_sessionmaker() as session:
        row = await session.scalar(select(Source).where(Source.id == source.id))
    assert row is not None
    assert "fixture-key" not in row.identity_key

    with pytest.raises(BotaiError) as exc:
        await make_source(
            tx_sessionmaker,
            account,
            title="Тот же календарь по https",
            link="https://calendar.example.org/private/fixture-key/export.ics",
        )
    assert exc.value.code is ErrorCode.CONFLICT


async def test_a_file_and_a_mailbox_have_their_own_identities(
    tx_sessionmaker: Sm, account: Account
) -> None:
    digest = "b" * 64
    file_source = await make_source(
        tx_sessionmaker,
        account,
        title="Методичка",
        file_sha256=digest.upper(),
        kinds=["file", "documents"],
    )
    assert file_source.identity_key == f"file:{digest}"
    mail_source = await make_source(
        tx_sessionmaker, account, title="Почта", mailbox=" Student@Example.ORG ", kinds=["mail"]
    )
    assert mail_source.identity_key == "mail:student@example.org"


async def test_a_draft_without_address_gets_a_provisional_identity_then_a_real_one(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account, title="Календарь", kinds=["calendar"])
    assert source.identity_key == f"draft:{source.id}"
    async with tx_sessionmaker() as session:
        updated = await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(entry_url="https://lk.example.org/calendar"),
        )
        await session.commit()
    assert updated.identity_key == "https://lk.example.org/calendar"
    assert updated.entry_url == "https://lk.example.org/calendar"


async def test_only_explicitly_named_identity_sources_are_accepted(
    tx_sessionmaker: Sm, account: Account
) -> None:
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, entry_url="https://a.example.org/x", link="x")
    assert exc.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, identity_key="not-an-identity")
    assert exc.value.code is ErrorCode.VALIDATION


async def test_a_foreign_space_and_a_foreign_source_look_missing(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    foreign = await make_source(tx_sessionmaker, second_account, title="Чужой", entry_url=URL)
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await service.create_source(
                session,
                user_id=account.user_id,
                space_id=second_account.space_id,
                title="Нельзя",
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await service.get_source(session, user_id=account.user_id, source_id=foreign.id)
    assert exc.value.code is ErrorCode.NOT_FOUND
    async with tx_sessionmaker() as session:
        assert await service.list_sources(session, user_id=account.user_id) == []


async def test_update_changes_only_what_was_sent(tx_sessionmaker: Sm, account: Account) -> None:
    source = await make_source(tx_sessionmaker, account, entry_url=URL)
    async with tx_sessionmaker() as session:
        updated = await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(
                title="Новое название",
                kinds=["lms", "schedule"],
                status="active",
                trust="high",
                priority=3,
                config={"faculty": "ФКН"},
            ),
        )
        await session.commit()
    assert updated.title == "Новое название"
    assert updated.kinds == ["lms", "schedule"]
    assert updated.status == "active"
    assert updated.trust == "high"
    assert updated.priority == 3
    assert updated.config == {"faculty": "ФКН"}
    assert updated.identity_key == "https://lk.example.org/schedule"


async def test_unknown_vocabulary_values_are_rejected(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account, entry_url=URL)
    cases = (
        SourcePatch(status="teleported"),
        SourcePatch(trust="absolute"),
        SourcePatch(kinds=["telepathy"]),
        SourcePatch(priority=None),
    )
    for patch in cases:
        with pytest.raises(BotaiError) as exc:
            async with tx_sessionmaker() as session:
                await service.update_source(
                    session, user_id=account.user_id, source_id=source.id, patch=patch
                )
        assert exc.value.code is ErrorCode.VALIDATION


async def test_disable_remembers_the_status_and_enable_restores_it(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account, entry_url=URL)
    async with tx_sessionmaker() as session:
        await service.set_status(
            session, user_id=account.user_id, source_id=source.id, status="needs_reauth"
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        disabled = await service.disable_source(
            session, user_id=account.user_id, source_id=source.id
        )
        await session.commit()
    assert disabled.status == "disabled"
    assert disabled.disabled_from == "needs_reauth"
    async with tx_sessionmaker() as session:
        enabled = await service.enable_source(session, user_id=account.user_id, source_id=source.id)
        await session.commit()
    assert enabled.status == "needs_reauth"
    assert enabled.disabled_from is None


async def test_a_playbook_can_be_attached_only_if_visible(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    async with tx_sessionmaker() as session:
        own = await playbooks.create_playbook(session, user_id=account.user_id, title="Свой")
        foreign = await playbooks.create_playbook(
            session, user_id=second_account.user_id, title="Чужой"
        )
        await session.commit()
    source = await make_source(tx_sessionmaker, account, playbook_id=own.id)
    assert source.playbook_id == own.id
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, title="Ещё", playbook_id=foreign.id)
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_list_filters_by_space_status_and_kind(tx_sessionmaker: Sm, account: Account) -> None:
    schedule = await make_source(
        tx_sessionmaker, account, title="Расписание", entry_url=URL, kinds=["site", "schedule"]
    )
    grades = await make_source(
        tx_sessionmaker,
        account,
        title="Оценки",
        entry_url="https://lk.example.org/grades",
        kinds=["site", "grades"],
    )
    async with tx_sessionmaker() as session:
        await service.update_source(
            session,
            user_id=account.user_id,
            source_id=schedule.id,
            patch=SourcePatch(status="active"),
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        by_kind = await service.list_sources(
            session, user_id=account.user_id, space_id=account.space_id, kind="grades"
        )
        active = await service.list_sources(
            session, user_id=account.user_id, space_id=account.space_id, status="active"
        )
    assert [row.id for row in by_kind] == [grades.id]
    assert [row.id for row in active] == [schedule.id]


async def test_empty_title_is_rejected(tx_sessionmaker: Sm, account: Account) -> None:
    with pytest.raises(BotaiError) as exc:
        await make_source(tx_sessionmaker, account, title="   ")
    assert exc.value.code is ErrorCode.VALIDATION
