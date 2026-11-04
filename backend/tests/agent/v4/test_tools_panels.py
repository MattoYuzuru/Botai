"""`ui_propose_panel`, `panel_list`, `panel_update` of the v4 agent (D-039, PR 6), tool level.

The tools are driven directly with a `ToolContext` and a scripted ask channel over a real Postgres
transaction: what the student sees as the question, what is stored, and what the model gets back.
The job-level flow (agent finds a rating next to the schedule, the student agrees) lives in
`tests/jobs/test_agent_discovery_v4.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from botai.agent.v4.protocol import (
    AskAnswer,
    AskQuestion,
    ToolContext,
    ToolHandler,
    ToolResult,
    validate_arguments,
)
from botai.agent.v4.tools.panels import PanelToolkit, build_panel_tools
from botai.db.models import Panel
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm
from tests.panels.support import G, add_grades, add_profile, add_ranking, at, make_source
from tests.sources.v4.support import Account, create_account

pytestmark = pytest.mark.db

SECRET = "S3cr3t-FIXTURE-Value-9137"
PEERS = ("Участник 17", "Участник 08")


@pytest.fixture(name="account")
async def _account(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker)


@pytest.fixture(name="second_account")
async def _second_account(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker, title="Другой вуз")


class Asker:
    """A scripted ask channel: answers in order (`None` = the run was cancelled)."""

    def __init__(self, *choices: int | None) -> None:
        self._choices = list(choices)
        self.questions: list[AskQuestion] = []

    async def __call__(self, question: AskQuestion) -> AskAnswer:
        self.questions.append(question)
        choice = self._choices.pop(0)
        if choice is None:
            return AskAnswer(cancelled=True)
        return AskAnswer(choice=choice)


def _context(account: Account, asker: Asker | None = None, **meta: Any) -> ToolContext:
    return ToolContext(run_id="run-1", user_id=account.user_id, metadata=meta, ask=asker)


def _tools(sm: Sm, *, mask: Any = None) -> dict[str, ToolHandler]:
    toolkit = PanelToolkit(sm=sm, mask=mask)
    return {tool.spec.name: tool for tool in build_panel_tools(toolkit)}


def _ranking_args(account: Account, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "space_id": account.space_id,
        "key": "ranking-group",
        "type": "ranking",
        "title": "Рейтинг группы",
        "dataset": {"kind": "ranking", "board": "Рейтинг группы"},
        "rationale": "Видно, на каком вы месте в группе.",
    }
    args.update(over)
    return args


async def _seed_rankings(sm: Sm, account: Account) -> str:
    source_id = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_ranking(
        sm,
        user_id=account.user_id,
        source_id=source_id,
        board="Рейтинг группы",
        places=[(1, PEERS[0], "96,4", False), (2, PEERS[1], "94,0", False), (5, "Я", "81,2", True)],
        total=28,
    )
    return source_id


async def _seed_grades(sm: Sm, account: Account, *, numeric: bool = True) -> str:
    source_id = await make_source(sm, user_id=account.user_id, space_id=account.space_id)
    await add_grades(
        sm,
        user_id=account.user_id,
        source_id=source_id,
        grades=[
            G(
                "Контрольная",
                "8",
                "10",
                8.0 if numeric else None,
                10.0 if numeric else None,
                at(12),
            ),
            G("Зачёт", "зачтено", None, None, None, at(20)),
        ],
    )
    return source_id


async def _panels(sm: Sm, space_id: str) -> list[Panel]:
    async with sm() as session:
        return list(
            await session.scalars(
                select(Panel).where(Panel.space_id == space_id).order_by(Panel.created_at)
            )
        )


async def _count(sm: Sm) -> int:
    async with sm() as session:
        return int(await session.scalar(select(func.count()).select_from(Panel)) or 0)


def _data(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result.data, dict)
    return result.data


# --- accept and decline --------------------------------------------------------------------


async def test_the_student_accepts_and_the_panel_becomes_active(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source_id = await _seed_rankings(tx_sessionmaker, account)
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account, source_id=source_id), _context(account, asker)
    )

    assert result.ok, result.error
    assert _data(result)["answer"] == "accepted"
    assert _data(result)["rows"] == 3
    (panel,) = await _panels(tx_sessionmaker, account.space_id)
    assert (panel.status, panel.visible, panel.origin) == ("active", True, "agent")
    assert panel.source_id == source_id
    assert panel.spec["icon"] == "trophy"  # the icon was chosen by the tool, not the model
    # One choice with the two labels and a short preview with the student's own place.
    (question,) = asker.questions
    assert question.input_kind == "choice"
    assert question.options == ("Добавить панель", "Не сейчас")
    assert len(question.question) <= 500
    assert "Рейтинг группы" in question.question
    assert "3 строки" in question.question
    assert "вы — 5 из 28" in question.question


async def test_the_student_declines_and_the_key_is_not_offered_again(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    tool = _tools(tx_sessionmaker)["ui_propose_panel"]
    asker = Asker(1)
    first = await tool.run(_ranking_args(account), _context(account, asker))
    assert first.ok
    assert _data(first)["answer"] == "declined"
    (panel,) = await _panels(tx_sessionmaker, account.space_id)
    assert (panel.status, panel.visible) == ("dismissed", False)

    # The same key again: no question, nothing changes.
    again = await tool.run(_ranking_args(account), _context(account, asker))
    assert again.ok
    assert _data(again)["outcome"] == "dismissed"
    assert len(asker.questions) == 1
    assert (await _panels(tx_sessionmaker, account.space_id))[0].status == "dismissed"

    # Unless the student asked for it himself.
    asked = Asker(0)
    revived = await tool.run(
        _ranking_args(account, student_requested=True), _context(account, asked)
    )
    assert revived.ok
    assert _data(revived)["answer"] == "accepted"
    assert (await _panels(tx_sessionmaker, account.space_id))[0].status == "active"


async def test_without_an_ask_channel_the_panel_stays_a_proposal(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account), _context(account, None)
    )

    assert result.ok
    assert _data(result)["answer"] == "not_asked"
    (panel,) = await _panels(tx_sessionmaker, account.space_id)
    assert (panel.status, panel.visible) == ("proposed", False)


async def test_a_cancelled_question_keeps_the_proposal_and_a_repeat_is_not_a_duplicate(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    tool = _tools(tx_sessionmaker)["ui_propose_panel"]
    first = await tool.run(_ranking_args(account), _context(account, Asker(None)))
    assert first.ok
    assert _data(first)["answer"] == "no_answer"
    assert (await _panels(tx_sessionmaker, account.space_id))[0].status == "proposed"

    # The same key: the still-proposed panel is asked about again, no second row appears.
    again = await tool.run(_ranking_args(account), _context(account, Asker(0)))
    assert again.ok
    assert _data(again)["answer"] == "accepted"
    assert len(await _panels(tx_sessionmaker, account.space_id)) == 1

    # A live panel is left alone and the student is not asked.
    asker = Asker()
    live = await tool.run(_ranking_args(account), _context(account, asker))
    assert live.ok
    assert _data(live)["status"] == "active"
    assert asker.questions == []


async def test_the_proposal_remembers_the_job_that_made_it(
    tx_sessionmaker: Sm, account: Account
) -> None:
    from tests.sources.v4.support import create_job

    await _seed_rankings(tx_sessionmaker, account)
    job_id = await create_job(tx_sessionmaker, account.user_id)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account), _context(account, Asker(0), job_id=job_id)
    )

    assert result.ok
    assert (await _panels(tx_sessionmaker, account.space_id))[0].proposed_by_job_id == job_id


# --- icons ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "panel_type", "icon"),
    [("grades", "kpi", "grade"), ("grades", "chart", "chart"), ("grades", "table", "table")],
)
async def test_the_icon_is_chosen_by_the_tool_when_the_model_does_not(
    tx_sessionmaker: Sm, account: Account, kind: str, panel_type: str, icon: str
) -> None:
    await _seed_grades(tx_sessionmaker, account)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        {
            "space_id": account.space_id,
            "key": f"mine-{panel_type}",
            "type": panel_type,
            "title": "Мои оценки",
            "dataset": {"kind": kind},
            "rationale": "Оценки под рукой.",
        },
        _context(account, None),
    )
    assert result.ok, result.error
    (panel,) = await _panels(tx_sessionmaker, account.space_id)
    assert panel.spec["icon"] == icon


async def test_a_profile_panel_gets_the_card_icon(tx_sessionmaker: Sm, account: Account) -> None:
    source_id = await make_source(
        tx_sessionmaker, user_id=account.user_id, space_id=account.space_id
    )
    await add_profile(
        tx_sessionmaker,
        user_id=account.user_id,
        source_id=source_id,
        fields=[("group", "Группа", "ПМ-31", "Обучение"), ("year", "Курс", "3", "Обучение")],
    )
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        {
            "space_id": account.space_id,
            "key": "card-short",
            "type": "profile",
            "title": "Карточка",
            "dataset": {"kind": "profile"},
            "view": {"fields": ["group"]},
            "rationale": "Группа и курс.",
        },
        _context(account, asker),
    )
    assert result.ok, result.error
    assert (await _panels(tx_sessionmaker, account.space_id))[0].spec["icon"] == "idCard"
    assert "2 поля" in asker.questions[0].question
    assert "ПМ-31" not in asker.questions[0].question  # counts, not the card itself


# --- refusals: nothing is stored -----------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"icon": "rocket"},
        {"title": "<b>Рейтинг</b>"},
        {"title": "Рейтинг https://evil.example"},
        {"type": "kpi"},  # a kpi panel shows grades, the dataset says ranking
        {"type": "gauge"},
        {"dataset": {"kind": "ranking"}},  # a ranking dataset needs its board
        {"dataset": {"kind": "ranking", "board": "Рейтинг группы", "courseId": "x"}},
        {"view": {"top": 500}},
        {"view": {"top": "10"}},
        {"view": {"script": "alert(1)"}},
        {"key": "Bad Key!"},
    ],
)
async def test_an_invalid_spec_is_a_structured_error_and_nothing_is_stored(
    tx_sessionmaker: Sm, account: Account, over: dict[str, Any]
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account, **over), _context(account, asker)
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code in {"validation", "invalid_spec"}
    assert result.error.detail
    assert asker.questions == []
    assert await _count(tx_sessionmaker) == 0


async def test_a_foreign_space_or_source_is_not_found(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    foreign_source = await _seed_rankings(tx_sessionmaker, second_account)
    tool = _tools(tx_sessionmaker)["ui_propose_panel"]

    stranger_space = await tool.run(_ranking_args(second_account), _context(account, Asker(0)))
    stranger_source = await tool.run(
        _ranking_args(account, source_id=foreign_source), _context(account, Asker(0))
    )

    for result in (stranger_space, stranger_source):
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "not_found"
    assert await _count(tx_sessionmaker) == 0


@pytest.mark.parametrize(
    "args",
    [
        {"type": "kpi", "dataset": {"kind": "grades"}, "title": "Оценки"},
        {"type": "profile", "dataset": {"kind": "profile"}, "title": "Карточка"},
        {
            "type": "ranking",
            "dataset": {"kind": "ranking", "board": "Рейтинг группы"},
            "title": "Рейтинг",
        },
    ],
)
async def test_a_panel_over_empty_data_is_refused_with_the_reason(
    tx_sessionmaker: Sm, account: Account, args: dict[str, Any]
) -> None:
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        {"space_id": account.space_id, "key": "empty-one", "rationale": "Нужна.", **args},
        _context(account, asker),
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "empty_dataset"
    assert "source_sync" in result.error.detail  # tells the model what to do first
    assert asker.questions == []
    assert await _count(tx_sessionmaker) == 0


async def test_a_ranking_board_the_source_does_not_have_is_empty(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account, dataset={"kind": "ranking", "board": "Рейтинг курса"}),
        _context(account, Asker(0)),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "empty_dataset"


async def test_a_chart_needs_numeric_grades_and_the_error_suggests_a_table(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_grades(tx_sessionmaker, account, numeric=False)
    tool = _tools(tx_sessionmaker)["ui_propose_panel"]
    chart = await tool.run(
        {
            "space_id": account.space_id,
            "key": "chart-all",
            "type": "chart",
            "title": "Оценки по датам",
            "dataset": {"kind": "grades"},
            "rationale": "Динамика.",
        },
        _context(account, Asker(0)),
    )
    assert not chart.ok
    assert chart.error is not None
    assert chart.error.code == "empty_dataset"
    assert "таблиц" in chart.error.detail
    assert await _count(tx_sessionmaker) == 0

    table = await tool.run(
        {
            "space_id": account.space_id,
            "key": "table-all",
            "type": "table",
            "title": "Оценки таблицей",
            "dataset": {"kind": "grades"},
            "rationale": "Все оценки.",
        },
        _context(account, Asker(0)),
    )
    assert table.ok, table.error


# --- secrets and personal data -------------------------------------------------------------


async def test_a_secret_in_the_text_is_refused_not_stored_not_echoed(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)

    def mask(text: str) -> str:
        return text.replace(SECRET, "[секрет скрыт]")

    tools = _tools(tx_sessionmaker, mask=mask)
    asker = Asker(0)
    for over in (
        {"title": f"Рейтинг {SECRET}"},
        {"rationale": f"Пароль {SECRET} подошёл"},
        {"dataset": {"kind": "ranking", "board": f"Список {SECRET}"}},
    ):
        result = await tools["ui_propose_panel"].run(
            _ranking_args(account, **over), _context(account, asker)
        )
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "secret_in_text"
        assert SECRET not in json.dumps(result.to_wire(), ensure_ascii=False)
    assert asker.questions == []
    assert await _count(tx_sessionmaker) == 0


async def test_the_question_and_the_result_hold_counts_not_other_peoples_rows(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account), _context(account, asker)
    )
    assert result.ok
    seen = asker.questions[0].question + json.dumps(result.to_wire(), ensure_ascii=False)
    for peer in PEERS:
        assert peer not in seen
    assert "96,4" not in seen


async def test_the_question_fits_the_choice_block_even_with_a_long_rationale(
    tx_sessionmaker: Sm, account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    asker = Asker(0)
    result = await _tools(tx_sessionmaker)["ui_propose_panel"].run(
        _ranking_args(account, rationale="Очень полезно. " * 130), _context(account, asker)
    )
    assert result.ok
    question = asker.questions[0].question
    assert len(question) <= 500
    assert question.startswith("Добавить в «Учёбу» панель «Рейтинг группы»?")
    assert "вы — 5 из 28" in question


# --- panel_list and panel_update -----------------------------------------------------------


async def test_panel_list_returns_only_the_students_own_panels(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    await _seed_rankings(tx_sessionmaker, second_account)
    tools = _tools(tx_sessionmaker)
    await tools["ui_propose_panel"].run(_ranking_args(account), _context(account, Asker(0)))
    await tools["ui_propose_panel"].run(
        _ranking_args(second_account, key="theirs", title="Чужой рейтинг"),
        _context(second_account, Asker(0)),
    )

    mine = await tools["panel_list"].run({}, _context(account))
    theirs = await tools["panel_list"].run({}, _context(second_account))

    assert mine.ok
    assert theirs.ok
    assert [item["key"] for item in _data(mine)["panels"]] == ["ranking-group"]
    assert [item["key"] for item in _data(theirs)["panels"]] == ["theirs"]
    only = _data(mine)["panels"][0]
    assert only["status"] == "active"
    assert only["origin"] == "agent"
    assert only["dataset"] == {"kind": "ranking", "board": "Рейтинг группы"}

    # A space of somebody else is not found, however it is asked.
    stranger = await tools["panel_list"].run(
        {"space_id": second_account.space_id}, _context(account)
    )
    assert not stranger.ok
    assert stranger.error is not None
    assert stranger.error.code == "not_found"


async def test_panel_list_filters_by_status(tx_sessionmaker: Sm, account: Account) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    tools = _tools(tx_sessionmaker)
    await tools["ui_propose_panel"].run(_ranking_args(account), _context(account, Asker(1)))

    dismissed = await tools["panel_list"].run({"status": "dismissed"}, _context(account))
    active = await tools["panel_list"].run({"status": "active"}, _context(account))

    assert [item["key"] for item in _data(dismissed)["panels"]] == ["ranking-group"]
    assert _data(active)["panels"] == []


async def test_panel_update_hides_and_foreign_panels_are_not_found(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    await _seed_rankings(tx_sessionmaker, account)
    tools = _tools(tx_sessionmaker)
    await tools["ui_propose_panel"].run(_ranking_args(account), _context(account, Asker(0)))
    (panel,) = await _panels(tx_sessionmaker, account.space_id)

    hidden = await tools["panel_update"].run(
        {"panel_id": panel.id, "visible": False, "position": 3}, _context(account)
    )
    assert hidden.ok
    assert _data(hidden)["visible"] is False
    assert _data(hidden)["position"] == 3

    nothing = await tools["panel_update"].run({"panel_id": panel.id}, _context(account))
    assert not nothing.ok
    assert nothing.error is not None
    assert nothing.error.code == "invalid_arguments"

    foreign = await tools["panel_update"].run(
        {"panel_id": panel.id, "visible": True}, _context(second_account)
    )
    assert not foreign.ok
    assert foreign.error is not None
    assert foreign.error.code == "not_found"
    assert (await _panels(tx_sessionmaker, account.space_id))[0].visible is False


# --- the specs the model sees --------------------------------------------------------------


def test_the_schema_requires_the_fields_and_rejects_unknown_ones() -> None:
    spec = PanelToolkit.propose_spec
    good = {
        "space_id": "s",
        "key": "k",
        "type": "ranking",
        "title": "Т",
        "dataset": {"kind": "ranking", "board": "Б"},
        "rationale": "Р",
    }
    assert validate_arguments(spec, good) == []
    assert validate_arguments(spec, {**good, "html": "<b>"})
    assert validate_arguments(spec, {k: v for k, v in good.items() if k != "rationale"})
    assert validate_arguments(spec, {**good, "type": "gauge"})
    assert PanelToolkit.propose_spec.policy == "write_soft"
    assert PanelToolkit.list_spec.parallel_safe()
    names = {tool.spec.name for tool in build_panel_tools(PanelToolkit(sm=None))}  # type: ignore[arg-type]
    assert names == {"ui_propose_panel", "panel_list", "panel_update"}
