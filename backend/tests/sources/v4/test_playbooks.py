"""Playbooks: shared or owned, versioned, one active version for the runner, old versions kept."""

from __future__ import annotations

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.sources.v4 import playbooks
from tests.auth.support import Sm
from tests.sources.v4.support import Account, create_job

pytestmark = pytest.mark.db

SPEC = {"reads": [{"data_kind": "schedule", "request": "/api/schedule"}]}
TESTS = [{"name": "расписание не пустое", "assert": "count > 0"}]


async def test_a_playbook_is_created_owned_and_versioned(
    tx_sessionmaker: Sm, account: Account
) -> None:
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(
            session,
            user_id=account.user_id,
            university_id=account.university_id,
            title="  ЛК ВШЭ  ",
        )
        assert playbook.owner_id == account.user_id
        assert playbook.title == "ЛК ВШЭ"
        assert playbook.language == "declarative"
        assert playbook.current_version == 0
        assert playbook.health == {}
        assert playbook.provenance == {}

        version = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            spec=SPEC,
            tests=TESTS,
        )
        await session.commit()
    assert version.version == 1
    assert version.active is False
    assert version.spec == SPEC
    assert version.code is None
    assert version.tests == TESTS
    assert version.checksum is not None
    assert len(version.checksum) == 64


async def test_activation_moves_one_pointer_and_keeps_the_old_version(
    tx_sessionmaker: Sm, account: Account
) -> None:
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(session, user_id=account.user_id, title="ЛК")
        await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=1,
            canary={"ok": True, "counts": {"schedule": 4}},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=1
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        active = await playbooks.active_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id
        )
        stored = await playbooks.get_playbook(
            session, user_id=account.user_id, playbook_id=playbook.id
        )
        assert active is not None
        assert active.version == 1
        assert stored.current_version == 1
        await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            code="result = {}",
            canary={"ok": True},
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=2,
            canary={"ok": True},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=2
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        versions = await playbooks.list_playbook_versions(
            session, user_id=account.user_id, playbook_id=playbook.id
        )
        assert [(row.version, row.active) for row in versions] == [(2, True), (1, False)]
        assert versions[1].canary == {"ok": True, "counts": {"schedule": 4}}

    # A rollback is the same activation of an older version: nothing is deleted. The owner may
    # waive the canary requirement for a version that was active before canaries existed.
    async with tx_sessionmaker() as session:
        await playbooks.activate_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=1,
            require_canary=False,
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        active = await playbooks.active_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id
        )
    assert active is not None
    assert active.version == 1


async def test_activation_requires_a_successful_canary_unless_waived(
    tx_sessionmaker: Sm, account: Account
) -> None:
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(session, user_id=account.user_id, title="ЛК")
        await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC
        )
        with pytest.raises(BotaiError) as exc:
            await playbooks.activate_playbook_version(
                session, user_id=account.user_id, playbook_id=playbook.id, version=1
            )
        assert exc.value.code is ErrorCode.CONFLICT
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=1,
            canary={"ok": False},
        )
        with pytest.raises(BotaiError) as exc:
            await playbooks.activate_playbook_version(
                session, user_id=account.user_id, playbook_id=playbook.id, version=1
            )
        assert exc.value.code is ErrorCode.CONFLICT
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=1,
            canary={"ok": True},
        )
        activated = await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=1
        )
        await session.commit()
    assert activated.active is True


async def test_checksum_recognizes_identical_content(tx_sessionmaker: Sm, account: Account) -> None:
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(session, user_id=account.user_id, title="ЛК")
        first = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC, tests=TESTS
        )
        second = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC, tests=TESTS
        )
        third = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            spec=SPEC,
            code="x = 1",
            tests=TESTS,
        )
        await session.commit()
    assert first.checksum == second.checksum
    assert third.checksum != first.checksum


async def test_shared_playbook_is_visible_and_editable_by_both(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    async with tx_sessionmaker() as session:
        shared = await playbooks.create_playbook(
            session, user_id=account.user_id, title="Общий", shared=True
        )
        private = await playbooks.create_playbook(session, user_id=account.user_id, title="Личный")
        await session.commit()
    assert shared.owner_id is None
    async with tx_sessionmaker() as session:
        visible = await playbooks.get_playbook(
            session, user_id=second_account.user_id, playbook_id=shared.id
        )
        assert visible.id == shared.id
        version = await playbooks.add_playbook_version(
            session,
            user_id=second_account.user_id,
            playbook_id=shared.id,
            spec=SPEC,
        )
        await session.commit()
    assert version.version == 1
    async with tx_sessionmaker() as session:
        theirs = await playbooks.list_playbooks(session, user_id=second_account.user_id)
        assert [row.id for row in theirs] == [shared.id]
        with pytest.raises(BotaiError) as exc:
            await playbooks.get_playbook(
                session, user_id=second_account.user_id, playbook_id=private.id
            )
        assert exc.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(BotaiError) as exc:
            await playbooks.add_playbook_version(
                session,
                user_id=second_account.user_id,
                playbook_id=private.id,
                spec=SPEC,
            )
        assert exc.value.code is ErrorCode.NOT_FOUND


async def test_versions_require_payload_and_known_targets(
    tx_sessionmaker: Sm, account: Account
) -> None:
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(session, user_id=account.user_id, title="ЛК")
        with pytest.raises(BotaiError) as exc:
            await playbooks.add_playbook_version(
                session, user_id=account.user_id, playbook_id=playbook.id
            )
        assert exc.value.code is ErrorCode.VALIDATION
        with pytest.raises(BotaiError) as exc:
            await playbooks.add_playbook_version(
                session,
                user_id=account.user_id,
                playbook_id=playbook.id,
                spec=SPEC,
                created_by_run="no-such-job",
            )
        assert exc.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(BotaiError) as exc:
            await playbooks.activate_playbook_version(
                session, user_id=account.user_id, playbook_id=playbook.id, version=7
            )
        assert exc.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(BotaiError) as exc:
            await playbooks.create_playbook(
                session, user_id=account.user_id, title="Плохой", university_id="no-such-uni"
            )
        assert exc.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(BotaiError) as exc:
            await playbooks.create_playbook(
                session, user_id=account.user_id, title="Плохой", language="cobol"
            )
        assert exc.value.code is ErrorCode.VALIDATION


async def test_created_by_run_must_be_the_users_job(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    foreign_job = await create_job(tx_sessionmaker, second_account.user_id)
    async with tx_sessionmaker() as session:
        playbook = await playbooks.create_playbook(session, user_id=account.user_id, title="ЛК")
        with pytest.raises(BotaiError) as exc:
            await playbooks.add_playbook_version(
                session,
                user_id=account.user_id,
                playbook_id=playbook.id,
                spec=SPEC,
                created_by_run=foreign_job,
            )
        assert exc.value.code is ErrorCode.NOT_FOUND
        own_job = await create_job(tx_sessionmaker, account.user_id)
        version = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            spec=SPEC,
            created_by_run=own_job,
        )
        await session.commit()
    assert version.created_by_run == own_job


async def test_list_playbooks_filters_by_university(
    tx_sessionmaker: Sm, account: Account, other_space: str
) -> None:
    async with tx_sessionmaker() as session:
        first = await playbooks.create_playbook(
            session, user_id=account.user_id, title="Первый", university_id=account.university_id
        )
        await playbooks.create_playbook(session, user_id=account.user_id, title="Второй")
        await session.commit()
    async with tx_sessionmaker() as session:
        rows = await playbooks.list_playbooks(
            session, user_id=account.user_id, university_id=account.university_id
        )
    assert [row.id for row in rows] == [first.id]
    assert other_space  # a second space must not leak playbooks of the first one
