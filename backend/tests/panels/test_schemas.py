"""Panel spec v1: a closed vocabulary of plain strings, numbers and enums (D-039)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.panels import schemas
from botai.panels.schemas import PANEL_ICONS, dump_spec, parse_spec
from botai.panels.service import MAX_PANELS_PER_SPACE, ranking_key, valid_key

GRADES = {"kind": "grades"}
VALID: dict[str, dict[str, Any]] = {
    "kpi": {
        "v": 1,
        "type": "kpi",
        "title": "Успеваемость",
        "icon": "grade",
        "dataset": GRADES,
        "view": {"metrics": ["count", "latest", "best"]},
    },
    "chart": {
        "v": 1,
        "type": "chart",
        "title": "Оценки по датам",
        "icon": "chart",
        "dataset": {"kind": "grades", "courseId": "01JABCDEFGHJKMNPQRSTVWXYZ0"},
        "view": {"variant": "bar", "x": "course", "limit": 50},
    },
    "table": {
        "v": 1,
        "type": "table",
        "title": "Таблица оценок",
        "icon": "table",
        "dataset": GRADES,
        "view": {"columns": ["title", "value", "scale"], "limit": 100},
    },
    "ranking": {
        "v": 1,
        "type": "ranking",
        "title": "Рейтинг потока",
        "icon": "trophy",
        "dataset": {"kind": "ranking", "board": "Рейтинг потока"},
        "view": {"top": 50, "highlightMe": False},
    },
    "profile": {
        "v": 1,
        "type": "profile",
        "title": "Профиль",
        "icon": "idCard",
        "dataset": {"kind": "profile"},
        "view": {"fields": ["group", "year"]},
    },
    "list": {
        "v": 1,
        "type": "list",
        "title": "Последние оценки",
        "icon": "star",
        "dataset": GRADES,
        "view": {"limit": 5},
    },
}


@pytest.mark.parametrize("kind", sorted(VALID))
def test_every_panel_type_accepts_its_spec_and_round_trips(kind: str) -> None:
    spec = parse_spec(VALID[kind])
    assert spec.type == kind
    stored = dump_spec(spec)
    assert stored == VALID[kind]  # camelCase, nothing added or lost
    assert parse_spec(stored) == spec


def test_a_spec_without_a_view_gets_the_default_view() -> None:
    raw = {k: v for k, v in VALID["chart"].items() if k != "view"}
    raw["dataset"] = GRADES
    spec = parse_spec(raw)
    assert dump_spec(spec)["view"] == {"variant": "line", "x": "date", "limit": 24}


def _mutated(kind: str, **changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(VALID[kind])
    for path, value in changes.items():
        target = raw
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        if value is _DROP:
            del target[last]
        else:
            target[last] = value
    return raw


_DROP = object()

REJECTED: list[tuple[str, dict[str, Any]]] = [
    # text that is not plain: HTML, markdown, links, scripts
    ("html title", _mutated("kpi", title="<b>Оценки</b>")),
    ("script title", _mutated("kpi", title="Оценки <script>alert(1)</script>")),
    ("markdown bold", _mutated("kpi", title="**Оценки**")),
    ("markdown link", _mutated("kpi", title="[Оценки](https://evil.test)")),
    ("markdown heading", _mutated("kpi", title="# Оценки")),
    ("markdown list", _mutated("kpi", title="- Оценки")),
    ("code", _mutated("kpi", title="`Оценки`")),
    ("url", _mutated("kpi", title="Смотри https://evil.test")),
    ("www", _mutated("kpi", title="www.evil.test")),
    ("javascript", _mutated("kpi", title="javascript:alert(1)")),
    ("control", _mutated("kpi", title="Оценки\x00")),
    ("empty title", _mutated("kpi", title="")),
    ("blank title", _mutated("kpi", title="   ")),
    ("long title", _mutated("kpi", title="О" * 61)),
    ("missing title", _mutated("kpi", title=_DROP)),
    # icons are names of the design registry
    ("unknown icon", _mutated("kpi", icon="rocket")),
    ("icon with markup", _mutated("kpi", icon="<svg/>")),
    ("icon emoji", _mutated("kpi", icon="🏆")),
    ("missing icon", _mutated("kpi", icon=_DROP)),
    # the vocabulary is closed
    ("unknown type", _mutated("kpi", type="gauge")),
    ("no type", _mutated("kpi", type=_DROP)),
    ("future version", _mutated("kpi", v=2)),
    ("unknown field", _mutated("kpi", html="<b>x</b>")),
    ("unknown view field", _mutated("kpi", view__script="alert(1)")),
    ("unknown dataset field", _mutated("kpi", dataset__url="https://evil.test")),
    ("unknown dataset kind", _mutated("kpi", dataset__kind="messages")),
    # datasets that do not fit the type
    ("kpi on profile", _mutated("kpi", dataset__kind="profile")),
    ("chart on ranking", _mutated("chart", dataset={"kind": "ranking", "board": "b"})),
    ("ranking on grades", _mutated("ranking", dataset={"kind": "grades"})),
    ("ranking without board", _mutated("ranking", dataset={"kind": "ranking"})),
    ("board on grades", _mutated("kpi", dataset={"kind": "grades", "board": "b"})),
    ("course on profile", _mutated("profile", dataset={"kind": "profile", "courseId": "x" * 26})),
    ("bad course id", _mutated("chart", dataset__courseId="not-an-id")),
    ("board with a control char", _mutated("ranking", dataset__board="a\nb")),
    ("empty board", _mutated("ranking", dataset__board="")),
    ("long board", _mutated("ranking", dataset__board="б" * 121)),
    # limits
    ("chart labels over 50", _mutated("chart", view__limit=51)),
    ("chart labels zero", _mutated("chart", view__limit=0)),
    ("chart variant", _mutated("chart", view__variant="pie")),
    ("chart axis", _mutated("chart", view__x="score")),
    ("table rows over 100", _mutated("table", view__limit=101)),
    ("table without columns", _mutated("table", view__columns=[])),
    ("table repeated column", _mutated("table", view__columns=["title", "title"])),
    ("table unknown column", _mutated("table", view__columns=["password"])),
    ("table too many columns", _mutated("table", view__columns=["title"] * 6)),
    ("ranking top over 50", _mutated("ranking", view__top=51)),
    ("ranking top zero", _mutated("ranking", view__top=0)),
    ("ranking flag as text", _mutated("ranking", view__highlightMe="yes")),
    ("profile too many fields", _mutated("profile", view__fields=[f"f{i}" for i in range(31)])),
    ("profile field with a control char", _mutated("profile", view__fields=["a\tb"])),
    ("list over 50", _mutated("list", view__limit=51)),
    ("kpi no metric", _mutated("kpi", view__metrics=[])),
    ("kpi repeated metric", _mutated("kpi", view__metrics=["count", "count"])),
    ("kpi unknown metric", _mutated("kpi", view__metrics=["average"])),
    ("a number as a string limit", _mutated("list", view__limit="5; drop table")),
]


@pytest.mark.parametrize(("label", "raw"), REJECTED, ids=[label for label, _ in REJECTED])
def test_a_bad_spec_is_rejected_without_echoing_the_value(label: str, raw: dict[str, Any]) -> None:
    with pytest.raises(BotaiError) as caught:
        parse_spec(raw)
    assert caught.value.code == ErrorCode.VALIDATION
    assert "evil.test" not in str(caught.value)
    assert "alert(1)" not in str(caught.value)


@pytest.mark.parametrize("raw", [None, [], "kpi", 7, {}])
def test_a_spec_that_is_not_an_object_is_rejected(raw: Any) -> None:
    with pytest.raises(BotaiError):
        parse_spec(raw)


def test_titles_keep_ordinary_punctuation_and_collapse_spacing() -> None:
    spec = parse_spec(_mutated("kpi", title="Оценки: 1-й   курс (осень) — №1, 5% & ещё"))
    assert spec.title == "Оценки: 1-й курс (осень) — №1, 5% & ещё"


def test_the_icon_list_is_the_documented_one() -> None:
    assert set(PANEL_ICONS) == {
        # the registry names panels use
        "grade",
        "course",
        "person",
        "user",
        "university",
        "schedule",
        "deadline",
        "table",
        "number",
        "group",
        "teacher",
        "today",
        "news",
        "document",
        # new in the design registry for panels
        "chart",
        "trophy",
        "star",
        "idCard",
        "trend",
    }


NEW_ICONS = {"chart", "trophy", "star", "idCard", "trend"}  # added to the registry by the design PR
REGISTRY = Path(__file__).resolve().parents[3] / "packages/design/src/ui/icon/registry.ts"


def test_the_icons_that_already_exist_are_in_the_design_registry() -> None:
    """A panel may name only a registry icon: the names that predate panels must be there (the
    web package checks the whole list once the new ones are added)."""
    registry = set(re.findall(r'^\s+"?([A-Za-z-]+)"?: Icon', REGISTRY.read_text("utf-8"), re.M))
    assert {"grade", "course", "person"} <= registry  # the pattern finds the keys
    assert (set(PANEL_ICONS) - NEW_ICONS) <= registry


def test_limits_match_the_chart_limits_of_the_design_system() -> None:
    assert schemas.MAX_CHART_LABELS == 50
    assert schemas.MAX_CHART_SERIES == 6
    assert schemas.MAX_TITLE == 60


def test_panel_keys_are_short_slugs() -> None:
    for key in ("grades-overview", "ranking:flow-1a2b3c4d", "a", "a" * 60, "0day_x"):
        assert valid_key(key) == key
    for key in ("", "A", "Grades", "-x", ":x", "a b", "a/b", "a" * 61, "оценки", "a<b"):
        with pytest.raises(BotaiError):
            valid_key(key)


def test_a_ranking_key_is_stable_distinct_and_valid() -> None:
    first = ranking_key("Рейтинг потока")
    assert first == ranking_key("  рейтинг   ПОТОКА ")  # the same board, spelled differently
    assert first != ranking_key("Рейтинг группы")
    assert valid_key(first) == first
    assert valid_key(ranking_key("Overall Rating 2026")).startswith("ranking:overall-rating-2026-")
    assert valid_key(ranking_key("Ж" * 500))
    assert len(ranking_key("Ж" * 500)) <= 60
    assert valid_key(ranking_key("!!!"))


def test_the_per_space_cap_is_sensible() -> None:
    assert 10 <= MAX_PANELS_PER_SPACE <= 50
