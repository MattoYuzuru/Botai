"""Run warnings (D-043 §3.5): pure checks over normalized records, hints and never errors."""

from __future__ import annotations

from typing import Any

from botai.sources.v4.checks import (
    generic_links,
    grades_without_numbers,
    no_item_grades,
    run_warnings,
    unknown_states,
)
from botai.sources.v4.runner import NormalizedRecord

LIST = "https://uni.example/learn/tasks"


def rec(kind: str, ext: str, open_url: str | None = None, **fields: Any) -> NormalizedRecord:
    return NormalizedRecord(kind=kind, external_id=ext, fields=fields, open_url=open_url)


def test_generic_links_fire_when_every_entity_of_a_kind_leads_to_one_page() -> None:
    same = [rec("deadlines", str(i), url=LIST) for i in range(3)]
    warning = generic_links(same)
    assert warning is not None
    assert (warning.code, warning.kinds, warning.examples) == (
        "generic_links",
        ["deadlines"],
        [LIST],
    )
    # the open_url of a declarative read counts when a record has no url of its own
    assert generic_links([rec("grades", str(i), open_url=LIST) for i in range(3)]) is not None
    # two records are too few to tell; distinct links, no links or a kind without pages are fine
    assert generic_links(same[:2]) is None
    assert generic_links([rec("deadlines", str(i), url=f"{LIST}/{i}") for i in range(3)]) is None
    assert generic_links([rec("deadlines", str(i)) for i in range(3)]) is None
    assert generic_links([rec("news", str(i), url=LIST) for i in range(3)]) is None


def test_unknown_states_from_thirty_percent_with_the_words_met() -> None:
    rows = [
        rec("deadlines", "1", state="unknown", state_text="Ждёт"),
        rec("deadlines", "2", state="unknown", state_text="Ждёт"),
        rec("deadlines", "3", state="open"),
        rec("deadlines", "4", state="unknown"),
    ]
    warning = unknown_states(rows)
    assert warning is not None
    assert warning.examples == ["Ждёт", "—"]
    assert "3 из 4" in warning.message
    calm = [rec("deadlines", str(i), state="open") for i in range(3)]
    assert unknown_states([*calm, rec("deadlines", "x", state="unknown")]) is None  # 25 %
    assert unknown_states([]) is None


def test_numeric_grades_without_the_top_of_the_scale() -> None:
    warning = grades_without_numbers(
        [
            rec("grades", "1", title="Баллы за курс", value="11", scale_max=None),
            rec("grades", "2", title="ДЗ", value="8", value_num=8.0, scale_max=10.0),
            rec("grades", "3", title="Зачёт", value="зачтено"),
        ]
    )
    assert warning is not None
    assert warning.examples == ["Баллы за курс: 11"]
    assert grades_without_numbers([rec("grades", "1", value="отлично")]) is None


def test_no_item_grades_when_scored_tasks_are_graded_but_no_work_grade_is_written() -> None:
    scored = rec("assignments", "a1", title="ДЗ 1", max_score=10.0)
    graded = rec("deadlines", "d1", title="ДЗ 1", state="graded")
    total = rec("grades", "t", value="11", layer="current")
    warning = no_item_grades([scored, graded, total])
    assert warning is not None
    assert warning.examples == ["ДЗ 1"]
    assert no_item_grades([scored, graded, rec("grades", "g", value="8", layer="item")]) is None
    linked = rec("grades", "g", value="8", assignment_external_id="a1")
    assert no_item_grades([scored, graded, linked]) is None
    assert no_item_grades([graded, total]) is None  # no maximum score anywhere: nothing to say
    assert no_item_grades([scored, total]) is None  # nothing graded yet


def test_run_warnings_are_ordered_by_code_and_empty_for_clean_records() -> None:
    clean = [
        rec("deadlines", "1", state="open", url=f"{LIST}/1"),
        rec("grades", "g", value="8", value_num=8.0, scale_max=10.0, layer="item"),
    ]
    assert run_warnings(clean) == []
    messy = [rec("deadlines", str(i), state="unknown", url=LIST) for i in range(3)]
    assert [w.code for w in run_warnings(messy)] == ["generic_links", "unknown_states"]
