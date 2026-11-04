"""Panels service: default panels, the agent's proposals, the student's decisions (D-039)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import Panel
from botai.panels import service
from botai.panels.schemas import parse_spec
from tests.auth.support import Sm
from tests.panels.support import (
    G,
    add_grades,
    add_profile,
    add_ranking,
    at,
    make_source,
)
from tests.sources.v4.support import Account, create_job

pytestmark = pytest.mark.db

KPI: dict[str, Any] = {
    "v": 1,
    "type": "kpi",
    "title": "Мои оценки",
    "icon": "grade",
    "dataset": {"kind": "grades"},
}


async def _keys(sm: Sm, space_id: str, *, status: str | None = None) -> list[str]:
    async with sm() as session:
        stmt = select(Panel.key).where(Panel.space_id == space_id).order_by(Panel.position)
        if status:
            stmt = stmt.where(Panel.status == status)
        return list(await session.scalars(stmt))


async def _ensure(sm: Sm, account: Account) -> list[str]:
    async with sm() as session:
        made = await service.ensure_default_panels(
            session, user_id=account.user_id, space_id=account.space_id
        )
        await session.commit()
        return made


async def _panel(sm: Sm, space_id: str, key: str) -> Panel:
    async with sm() as session:
        return (
            await session.scalars(select(Panel).where(Panel.space_id == space_id, Panel.key == key))
        ).one()


async def test_a_university_without_data_gets_no_panels(
    tx_sessionmaker: Sm, account: Account
) -> None:
    assert await _ensure(tx_sessionmaker, account) == []
    assert await _keys(tx_sessionmaker, account.space_id) == []


async def test_default_panels_follow_the_data_and_are_made_once(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_grades(
        sm,
        user_id=account.user_id,
        source_id=source,
        grades=[
            G("Коллоквиум", "8", "10 баллов", 8.0, 10.0, at(12)),
            G("Зачёт", "зачтено", "зачёт/незачёт"),
        ],
    )
    await add_profile(
        sm,
        user_id=account.user_id,
        source_id=source,
        fields=[("group", "Группа", "ПМ-31", "Обучение")],
    )
    for board in ("Рейтинг потока", "Рейтинг группы"):
        await add_ranking(
            sm,
            user_id=account.user_id,
            source_id=source,
            board=board,
            places=[(1, "Участник 1", "90", False), (2, "Иванова А.", "80", True)],
        )

    made = await _ensure(sm, account)
    assert made[:3] == ["grades-overview", "grades-chart", "student-profile"]
    assert len(made) == 5
    assert all(key.startswith("ranking:") for key in made[3:])
    assert len(set(made)) == 5

    again = await _ensure(sm, account)  # idempotent
    assert again == []
    assert await _keys(sm, account.space_id) == made

    async with sm() as session:
        rows = (
            await session.scalars(select(Panel).where(Panel.space_id == account.space_id))
        ).all()
    assert {r.origin for r in rows} == {"system"}
    assert {r.status for r in rows} == {"active"}
    assert all(r.visible and not r.hidden_in_all for r in rows)
    assert [r.position for r in sorted(rows, key=lambda r: r.position)] == [0, 1, 2, 3, 4]
    chart = next(r for r in rows if r.key == "grades-chart")
    assert chart.spec["view"] == {"variant": "line", "x": "date", "limit": 24}
    boards = {r.spec["dataset"]["board"] for r in rows if r.type == "ranking"}
    assert boards == {"Рейтинг потока", "Рейтинг группы"}
    for row in rows:
        assert parse_spec(row.spec).type == row.type  # what is stored is a valid spec


async def test_grades_without_numbers_give_an_overview_but_no_chart(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_grades(
        sm, user_id=account.user_id, source_id=source, grades=[G("Зачёт", "зачтено", "зачёт")]
    )
    assert await _ensure(sm, account) == ["grades-overview"]
    # a later sync brings a numeric grade without dates: the chart appears, by course
    await add_grades(
        sm,
        user_id=account.user_id,
        source_id=source,
        grades=[G("Тест", "7", "10", 7.0, 10.0)],
        prefix="later",
    )
    assert await _ensure(sm, account) == ["grades-chart"]
    assert (await _panel(sm, account.space_id, "grades-chart")).spec["view"]["x"] == "course"


async def test_the_chart_is_by_course_when_numeric_grades_have_no_dates(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_grades(
        sm,
        user_id=account.user_id,
        source_id=source,
        grades=[G("Тест", "7", "10", 7.0, 10.0)],
    )
    assert await _ensure(sm, account) == ["grades-overview", "grades-chart"]
    chart = await _panel(sm, account.space_id, "grades-chart")
    assert chart.spec["view"]["x"] == "course"
    assert chart.spec["view"]["variant"] == "bar"


async def test_the_students_choices_are_never_undone_by_ensure(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_profile(
        sm, user_id=account.user_id, source_id=source, fields=[("group", "Группа", "ПМ-31", None)]
    )
    await _ensure(sm, account)
    panel = await _panel(sm, account.space_id, "student-profile")
    async with sm() as session:
        await service.update_panel(
            session, user_id=account.user_id, panel_id=panel.id, visible=False, position=9
        )
        await service.set_panel_status(
            session, user_id=account.user_id, panel_id=panel.id, status="dismissed"
        )
        await session.commit()
    assert await _ensure(sm, account) == []
    after = await _panel(sm, account.space_id, "student-profile")
    assert (after.visible, after.status, after.position) == (False, "dismissed", 9)


async def test_a_board_with_odd_characters_still_gets_a_valid_panel(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_ranking(
        sm,
        user_id=account.user_id,
        source_id=source,
        board="<b>Топ</b> **лучших** https://evil.test",
        places=[(1, "Иванова А.", "99", True)],
    )
    made = await _ensure(sm, account)
    assert len(made) == 1
    panel = await _panel(sm, account.space_id, made[0])
    spec = parse_spec(panel.spec)
    assert "<" not in spec.title
    assert "*" not in spec.title
    assert "://" not in spec.title
    assert spec.dataset.board == "<b>Топ</b> **лучших** https://evil.test"  # the selector is exact


async def test_a_flood_of_rating_boards_gets_a_bounded_number_of_panels(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    for i in range(service.MAX_SYSTEM_BOARDS + 4):
        await add_ranking(
            sm,
            user_id=account.user_id,
            source_id=source,
            board=f"Список {i:02d}",
            places=[(1, "Иванова А.", "90", True)],
        )
    made = await _ensure(sm, account)
    assert len(made) == service.MAX_SYSTEM_BOARDS
    assert len(service.valid_key(made[0])) <= 60


async def test_ensure_refuses_a_foreign_space(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    async with tx_sessionmaker() as session:
        with pytest.raises(BotaiError) as caught:
            await service.ensure_default_panels(
                session, user_id=account.user_id, space_id=second_account.space_id
            )
    assert caught.value.code == ErrorCode.NOT_FOUND


# --- proposals -------------------------------------------------------------------------------


async def _propose(sm: Sm, account: Account, key: str = "my-grades", **kw: Any) -> service.Proposal:
    async with sm() as session:
        result = await service.propose_panel(
            session,
            user_id=account.user_id,
            space_id=kw.pop("space_id", account.space_id),
            key=key,
            spec=kw.pop("spec", KPI),
            **kw,
        )
        await session.commit()
        return result


async def test_a_proposal_is_stored_proposed_invisible_and_owned_by_the_agent(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    source = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    job_id = await create_job(sm, account.user_id)
    result = await _propose(sm, account, source_id=source, job_id=job_id)
    assert result.outcome == "created"
    panel = await _panel(sm, account.space_id, "my-grades")
    assert (panel.status, panel.visible, panel.origin) == ("proposed", False, "agent")
    assert (panel.source_id, panel.proposed_by_job_id, panel.type) == (source, job_id, "kpi")
    assert panel.spec["title"] == "Мои оценки"
    assert panel.spec["view"] == {"metrics": ["count", "latest"]}  # defaults are stored


async def test_a_proposal_is_idempotent_by_key(tx_sessionmaker: Sm, account: Account) -> None:
    sm = tx_sessionmaker
    assert (await _propose(sm, account)).outcome == "created"
    assert (await _propose(sm, account)).outcome == "exists"  # the same spec again
    better = {**KPI, "title": "Оценки получше", "view": {"metrics": ["count", "best"]}}
    changed = await _propose(sm, account, spec=better)
    assert changed.outcome == "updated"  # still proposed: the agent may refine it
    assert changed.panel.spec["title"] == "Оценки получше"
    assert await _keys(sm, account.space_id) == ["my-grades"]


async def test_a_dismissed_key_is_not_proposed_again_unless_forced(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    first = await _propose(sm, account)
    async with sm() as session:
        await service.set_panel_status(
            session, user_id=account.user_id, panel_id=first.panel.id, status="dismissed"
        )
        await session.commit()
    again = await _propose(sm, account)
    assert again.outcome == "dismissed"
    assert (await _panel(sm, account.space_id, "my-grades")).status == "dismissed"
    forced = await _propose(sm, account, force=True)
    assert forced.outcome == "updated"
    panel = await _panel(sm, account.space_id, "my-grades")
    assert (panel.status, panel.visible) == ("proposed", False)


async def test_a_live_panel_is_not_overwritten_by_a_proposal(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    first = await _propose(sm, account)
    async with sm() as session:
        await service.set_panel_status(
            session, user_id=account.user_id, panel_id=first.panel.id, status="active"
        )
        await session.commit()
    result = await _propose(sm, account, spec={**KPI, "title": "Другое"})
    assert result.outcome == "exists"
    assert (await _panel(sm, account.space_id, "my-grades")).spec["title"] == "Мои оценки"


@pytest.mark.parametrize(
    ("key", "spec"),
    [
        ("Bad Key", KPI),
        ("", KPI),
        ("ok", {**KPI, "title": "<b>x</b>"}),
        ("ok", {**KPI, "icon": "rocket"}),
        ("ok", {**KPI, "dataset": {"kind": "ranking"}}),
        ("ok", "kpi"),
    ],
)
async def test_an_invalid_proposal_is_refused_and_nothing_is_stored(
    tx_sessionmaker: Sm, account: Account, key: str, spec: Any
) -> None:
    with pytest.raises(BotaiError) as caught:
        await _propose(tx_sessionmaker, account, key=key, spec=spec)
    assert caught.value.code == ErrorCode.VALIDATION
    assert await _keys(tx_sessionmaker, account.space_id) == []


async def test_a_proposal_cannot_use_a_foreign_space_source_or_job(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    sm = tx_sessionmaker
    theirs = await make_source(
        sm, user_id=second_account.user_id, space_id=second_account.space_id, title="Чужой"
    )
    for kw in (
        {"space_id": second_account.space_id},
        {"source_id": theirs},
        {"source_id": new_id()},
        {"job_id": new_id()},
    ):
        with pytest.raises(BotaiError) as caught:
            await _propose(sm, account, **kw)
        assert caught.value.code == ErrorCode.NOT_FOUND, kw
    assert await _keys(sm, account.space_id) == []
    assert await _keys(sm, second_account.space_id) == []


async def test_a_university_holds_a_bounded_number_of_panels(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    for i in range(service.MAX_PANELS_PER_SPACE):
        await _propose(sm, account, key=f"p{i}")
    with pytest.raises(BotaiError) as caught:
        await _propose(sm, account, key="one-more")
    assert caught.value.code == ErrorCode.VALIDATION
    # a dismissed panel frees its place
    first = await _panel(sm, account.space_id, "p0")
    async with sm() as session:
        await service.set_panel_status(
            session, user_id=account.user_id, panel_id=first.id, status="dismissed"
        )
        await session.commit()
    assert (await _propose(sm, account, key="one-more")).outcome == "created"


# --- decisions -------------------------------------------------------------------------------


async def test_status_moves_and_switches(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    sm = tx_sessionmaker
    panel_id = (await _propose(sm, account)).panel.id
    async with sm() as session:
        accepted = await service.set_panel_status(
            session, user_id=account.user_id, panel_id=panel_id, status="active"
        )
        assert (accepted.status, accepted.visible) == ("active", True)
        hidden = await service.update_panel(
            session,
            user_id=account.user_id,
            panel_id=panel_id,
            visible=False,
            hidden_in_all=True,
            position=4,
        )
        assert (hidden.visible, hidden.hidden_in_all, hidden.position) == (False, True, 4)
        # omitted switches stay
        kept = await service.update_panel(
            session, user_id=account.user_id, panel_id=panel_id, visible=True
        )
        assert (kept.visible, kept.hidden_in_all, kept.position) == (True, True, 4)
        dismissed = await service.set_panel_status(
            session, user_id=account.user_id, panel_id=panel_id, status="dismissed"
        )
        assert (dismissed.status, dismissed.visible) == ("dismissed", False)
        with pytest.raises(
            BotaiError
        ) as caught:  # a dismissed panel is switched on only by accepting
            await service.update_panel(
                session, user_id=account.user_id, panel_id=panel_id, visible=True
            )
        assert caught.value.code == ErrorCode.CONFLICT
        back = await service.set_panel_status(
            session, user_id=account.user_id, panel_id=panel_id, status="active"
        )
        assert back.status == "active"
        with pytest.raises(BotaiError) as moved:  # an active panel is never "proposed" again
            await service.set_panel_status(
                session, user_id=account.user_id, panel_id=panel_id, status="proposed"
            )
        assert moved.value.code == ErrorCode.CONFLICT
        # a foreign id is not found, whatever the call
        for call in (
            service.get_panel(session, user_id=second_account.user_id, panel_id=panel_id),
            service.set_panel_status(
                session, user_id=second_account.user_id, panel_id=panel_id, status="active"
            ),
            service.update_panel(session, user_id=second_account.user_id, panel_id=panel_id),
        ):
            with pytest.raises(BotaiError) as nope:
                await call
            assert nope.value.code == ErrorCode.NOT_FOUND


async def test_list_panels_is_ordered_by_space_then_position(
    tx_sessionmaker: Sm, account: Account
) -> None:
    sm = tx_sessionmaker
    for key in ("b", "a", "c"):
        await _propose(sm, account, key=key)
    async with sm() as session:
        rows = {
            p.key: p
            for p, _, _ in await service.list_panels(
                session, user_id=account.user_id, status="proposed"
            )
        }
        await service.update_panel(
            session, user_id=account.user_id, panel_id=rows["a"].id, position=0
        )
        await service.update_panel(
            session, user_id=account.user_id, panel_id=rows["c"].id, position=1
        )
        await service.update_panel(
            session, user_id=account.user_id, panel_id=rows["b"].id, position=2
        )
        await session.commit()
    async with sm() as session:
        listed = await service.list_panels(session, user_id=account.user_id, status="proposed")
    assert [p.key for p, _, _ in listed] == ["a", "c", "b"]
    count = await _count(sm)
    assert count == 3


async def _count(sm: Sm) -> int:
    async with sm() as session:
        return int(await session.scalar(select(func.count()).select_from(Panel)) or 0)
