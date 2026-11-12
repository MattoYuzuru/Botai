"""A v4 sync of grades, a student card and rating boards from fakeuni makes rows and panels (L1),
and deleting a source, a university or an account takes both away (D-039)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.db.models import (
    Connection,
    Grade,
    Panel,
    ProfileField,
    RankingEntry,
    Source,
    Space,
)
from botai.sources.v4 import playbooks, service
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import FrozenClock, Sm, make_space, signup
from tests.panels.support import BASE, NOW, G, add_grades, add_profile, add_ranking, at, make_source
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {
            "name": "grades",
            "kind": "grades",
            "url": f"{BASE}/api/v2/grades",
            "format": "json",
            "items_path": "/items",
            "fields": {
                "externalId": "/id",
                "title": "/name",
                "courseId": "/course",
                "value": "/mark",
                "scale": "/scale",
                "gradedAt": "/date",
            },
        },
        {
            "name": "profile",
            "kind": "profile",
            "url": f"{BASE}/api/v2/profile",
            "format": "json",
            "items_path": "/items",
            "fields": {
                "externalId": "/key",
                "field": "/key",
                "label": "/caption",
                "value": "/text",
                "group": "/section",
            },
        },
        {
            "name": "ratings",
            "kind": "ranking",
            "url": f"{BASE}/api/v2/ratings",
            "format": "json",
            "items_path": "/items",
            "fields": {
                "externalId": "/id",
                "board": "/board",
                "position": "/place",
                "label": "/who",
                "score": "/score",
                "isMe": "/me",
                "total": "/of",
                "period": "/period",
            },
        },
    ],
    "timeout_s": 10,
}


async def _source_with_playbook(sm: Sm, account: Account) -> str:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="ЛК Fakeuni",
            entry_url=f"{BASE}/app",
            kinds=["site", "grades", "profile", "ranking"],
        )
        playbook = await playbooks.create_playbook(
            session, user_id=account.user_id, title="Fakeuni", university_id=account.university_id
        )
        version = await playbooks.add_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, spec=SPEC
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=version.version,
            canary={"ok": True},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=version.version
        )
        await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()
        return source.id


def _runner(sm: Sm, account: Account, transport: Any, cookies: dict[str, str]) -> SourceRunner:
    return SourceRunner(
        sm(), user_id=account.user_id, transport=transport, cookies=cookies, env="test"
    )


async def _count(sm: Sm, model: type, **where: Any) -> int:
    async with sm() as session:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return int(await session.scalar(stmt) or 0)


async def test_a_sync_reads_grades_card_and_ratings_and_makes_the_panels_once(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    sm = tx_sessionmaker
    source_id = await _source_with_playbook(sm, account)
    report = await _runner(sm, account, fakeuni_transport, fakeuni_cookies).sync(source_id)
    assert report.ok, report.errors

    # grades: the placeholder "None" is not a grade; numbers only where the source gave both
    assert report.domain["grades"]["inserted"] == 5, report.domain
    async with sm() as session:
        grades = {
            g.external_id: g
            for g in await session.scalars(select(Grade).where(Grade.source_id == source_id))
        }
    assert set(grades) == {"g-1", "g-2", "g-3", "g-4", "g-6"}  # g-5 had the value "None"
    assert "None" not in {g.value for g in grades.values()}
    assert (grades["g-1"].value, grades["g-1"].value_num, grades["g-1"].scale_max) == (
        "8",
        8.0,
        10.0,
    )
    assert (grades["g-2"].value, grades["g-2"].value_num, grades["g-2"].scale_max) == (
        "8,5",
        8.5,
        10.0,
    )
    assert (grades["g-3"].value_num, grades["g-3"].scale_max) == (92.0, 100.0)
    assert (grades["g-4"].value, grades["g-4"].value_num, grades["g-4"].scale_max) == (
        "зачтено",
        None,
        None,
    )
    assert (grades["g-6"].scale, grades["g-6"].value_num) == (
        None,
        None,
    )  # a number without a scale
    assert grades["g-1"].graded_at is not None
    assert grades["g-6"].graded_at is None

    # the card: the value as printed; the field that holds a secret is not stored
    assert report.domain["profile"]["inserted"] == 4
    async with sm() as session:
        fields = {
            f.field: f
            for f in await session.scalars(
                select(ProfileField).where(ProfileField.source_id == source_id)
            )
        }
    assert set(fields) == {"group", "programme", "year", "status"}
    assert fields["year"].value == "3"
    assert (fields["group"].label, fields["group"].group_text) == ("Группа", "Обучение")
    assert fields["programme"].group_text is None

    # the boards
    assert report.domain["ranking"]["inserted"] == 6
    async with sm() as session:
        entries = list(
            await session.scalars(select(RankingEntry).where(RankingEntry.source_id == source_id))
        )
    assert {e.board for e in entries} == {"Рейтинг потока", "Рейтинг группы"}
    mine = sorted((e.board, e.position) for e in entries if e.is_me)
    assert mine == [("Рейтинг группы", 1), ("Рейтинг потока", 14)]
    flow_me = next(e for e in entries if e.is_me and e.board == "Рейтинг потока")
    assert (flow_me.score, flow_me.score_num, flow_me.total, flow_me.period) == (
        "81,2",
        81.2,
        120,
        "Осень 2026",
    )

    # the domain write made the default panels, once
    async with sm() as session:
        panels = list(
            await session.scalars(
                select(Panel).where(Panel.space_id == account.space_id).order_by(Panel.position)
            )
        )
    keys = [p.key for p in panels]
    assert keys[:3] == ["grades-overview", "grades-chart", "student-profile"]
    assert len(keys) == 6
    assert sum(k.startswith("ranking:") for k in keys) == 2
    assert {p.origin for p in panels} == {"system"}

    second = await _runner(sm, account, fakeuni_transport, fakeuni_cookies).sync(source_id)
    assert second.ok
    assert second.domain["grades"] == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 5,
        "skipped": 0,
        "deleted": 0,
    }
    assert second.domain["ranking"]["inserted"] == 0
    assert await _count(sm, Panel, space_id=account.space_id) == 6  # no duplicates, nothing reset
    assert await _count(sm, Grade, source_id=source_id) == 5


async def test_a_sync_keeps_the_students_panel_choices(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    sm = tx_sessionmaker
    source_id = await _source_with_playbook(sm, account)
    await _runner(sm, account, fakeuni_transport, fakeuni_cookies).sync(source_id)
    async with sm() as session:
        profile = (
            await session.scalars(
                select(Panel).where(
                    Panel.space_id == account.space_id, Panel.key == "student-profile"
                )
            )
        ).one()
        profile.visible, profile.position = False, 42
        await session.commit()
    await _runner(sm, account, fakeuni_transport, fakeuni_cookies).sync(source_id)
    async with sm() as session:
        after = (
            await session.scalars(
                select(Panel).where(
                    Panel.space_id == account.space_id, Panel.key == "student-profile"
                )
            )
        ).one()
    assert (after.visible, after.position) == (False, 42)


async def test_v3_rows_are_untouched_by_the_new_columns_and_kinds(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    async with sm() as session:
        session.add(
            Connection(
                id="v3-conn-0000000000000009",
                user_id=account.user_id,
                space_id=account.space_id,
                origin=BASE,
                kind="lms",
                title="Старый кабинет",
                status="active",
                allow_hosts=[],
            )
        )
        await session.flush()
        session.add(
            Grade(
                id="v3-grade-000000000000009",
                user_id=account.user_id,
                connection_id="v3-conn-0000000000000009",
                external_id="old",
                source_url=BASE,
                fetched_at=NOW,
                seen_at=NOW,
                value="4",
                scale="5",
            )
        )
        await session.commit()
    async with sm() as session:
        old = await session.get(Grade, "v3-grade-000000000000009")
    assert old is not None
    assert (old.value, old.scale, old.value_num, old.scale_max) == ("4", "5", None, None)
    assert old.connection_id == "v3-conn-0000000000000009"
    assert old.source_id is None


# --- deletion ------------------------------------------------------------------------------------


async def _seed(sm: Sm, account: Account, *, title: str = "ЛК", url: str = f"{BASE}/app") -> str:
    source_id = await make_source(
        sm, user_id=account.user_id, space_id=account.space_id, title=title, url=url
    )
    await add_grades(
        sm,
        user_id=account.user_id,
        source_id=source_id,
        grades=[G("Тест", "7", "10", 7.0, 10.0, at(12))],
    )
    await add_profile(
        sm,
        user_id=account.user_id,
        source_id=source_id,
        fields=[("group", "Группа", "ПМ-31", None)],
    )
    await add_ranking(
        sm,
        user_id=account.user_id,
        source_id=source_id,
        board="Рейтинг потока",
        places=[(1, "Иванова А.", "90", True)],
    )
    return source_id


async def _make_panels(sm: Sm, account: Account) -> list[str]:
    async with sm() as session:
        made = await _ensure(session, account)
        await session.commit()
    return made


async def _ensure(session: Any, account: Account) -> list[str]:
    from botai.panels import service as panels

    return await panels.ensure_default_panels(
        session, user_id=account.user_id, space_id=account.space_id
    )


async def test_deleting_a_source_deletes_its_new_kind_rows_and_its_panels(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user = await signup(v4_client, sm, clock)
    space_id = await make_space(sm, user.id)
    account = Account(user_id=user.id, space_id=space_id, university_id="")
    doomed = await _seed(sm, account, title="Удаляемый")
    kept = await _seed(sm, account, title="Остающийся", url="https://other.test/app")
    assert (
        len(await _make_panels(sm, account)) == 5
    )  # overview, chart, profile, one ranking, latest

    # an agent's panel built for the doomed source, and one for the other source
    from botai.panels import service as panels

    spec = {
        "v": 1,
        "type": "list",
        "title": "Последние",
        "icon": "star",
        "dataset": {"kind": "grades"},
    }
    async with sm() as session:
        await panels.propose_panel(
            session,
            user_id=user.id,
            space_id=space_id,
            key="about-doomed",
            spec=spec,
            source_id=doomed,
        )
        await panels.propose_panel(
            session, user_id=user.id, space_id=space_id, key="about-kept", spec=spec, source_id=kept
        )
        await session.commit()

    response = await v4_client.delete(f"/api/v1/sources/{doomed}")
    assert response.status_code == 204, response.text
    for model in (Grade, ProfileField, RankingEntry):
        assert await _count(sm, model, source_id=doomed) == 0, model.__name__
        assert await _count(sm, model, source_id=kept) == 1, model.__name__
    async with sm() as session:
        keys = set(await session.scalars(select(Panel.key).where(Panel.space_id == space_id)))
    # the other source still has data for every default panel; only the doomed source's own panel goes
    assert "about-doomed" not in keys
    assert "about-kept" in keys
    assert {"grades-overview", "grades-chart", "student-profile"} <= keys

    response = await v4_client.delete(f"/api/v1/sources/{kept}")
    assert response.status_code == 204, response.text
    async with sm() as session:
        left = set(await session.scalars(select(Panel.key).where(Panel.space_id == space_id)))
    # no source has data any more: the system panels go; the agent's panel about the deleted source went
    assert left == set()


async def test_a_system_panel_the_student_hid_survives_while_its_data_does(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user = await signup(v4_client, sm, clock)
    space_id = await make_space(sm, user.id)
    account = Account(user_id=user.id, space_id=space_id, university_id="")
    doomed = await _seed(sm, account, title="Удаляемый")
    await _seed(sm, account, title="Остающийся", url="https://other.test/app")
    await _make_panels(sm, account)
    panels = (await v4_client.get("/api/v1/panels")).json()
    overview = next(p for p in panels if p["key"] == "grades-overview")
    await v4_client.patch(f"/api/v1/panels/{overview['id']}", json={"visible": False})
    assert (await v4_client.delete(f"/api/v1/sources/{doomed}")).status_code == 204
    after = {p["key"]: p for p in (await v4_client.get("/api/v1/panels")).json()}
    assert after["grades-overview"]["visible"] is False  # the choice is kept


async def test_deleting_a_university_deletes_its_rows_and_panels_and_only_its_own(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user = await signup(v4_client, sm, clock)
    doomed_space = await make_space(sm, user.id, "Удаляемый вуз")
    kept_space = await make_space(sm, user.id, "Остающийся вуз")
    doomed = await _seed(sm, Account(user.id, doomed_space, ""))
    kept = await _seed(sm, Account(user.id, kept_space, ""), url="https://other.test/app")
    await _make_panels(sm, Account(user.id, doomed_space, ""))
    await _make_panels(sm, Account(user.id, kept_space, ""))

    response = await v4_client.delete(f"/api/v1/spaces/{doomed_space}")
    assert response.status_code == 204, response.text
    assert await _count(sm, Space, id=doomed_space) == 0
    assert await _count(sm, Panel, space_id=doomed_space) == 0
    assert await _count(sm, Source, id=doomed) == 0
    for model in (Grade, ProfileField, RankingEntry):
        # not left behind with a NULL provenance, and the other university is untouched
        assert await _count(sm, model, user_id=user.id) == 1, model.__name__
        assert await _count(sm, model, source_id=kept) == 1, model.__name__
    assert await _count(sm, Panel, space_id=kept_space) == 5


async def test_resetting_an_account_clears_panels_and_new_kind_rows_of_that_account_only(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    from botai.ops import reset_account

    sm = tx_sessionmaker
    for who in (account, second_account):
        await _seed(sm, who, url=f"{BASE}/{who.user_id}")
        await _make_panels(sm, who)
    async with sm() as session:
        counts, _ = await reset_account.reset_account(
            session, account.user_id, shared=False, apply=True
        )
        await session.commit()
    assert counts.get("panels", 0) >= 4
    assert counts.get("profile_fields", 0) == 1
    assert counts.get("ranking_entries", 0) == 1
    for model in (Panel, ProfileField, RankingEntry, Grade):
        assert await _count(sm, model, user_id=account.user_id) == 0, model.__name__
        assert await _count(sm, model, user_id=second_account.user_id) > 0, model.__name__
