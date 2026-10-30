"""The read-time merge of the schedule events of several sources of one university (FR-054, L0).

Pure functions over `ScheduleEventOut`: no database, no model. The data is the synthetic `fakeuni`:
a cabinet that knows rooms and campus (the primary) and a calendar that knows the teacher and an
online link (the secondary). Nothing here is about a real university.
"""

from __future__ import annotations

import itertools
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.domain import merge
from botai.domain.merge import Coverage, SpacePlan
from botai.domain.schemas import (
    EventAttribute,
    EventLink,
    EventSource,
    Provenance,
    ScheduleEventOut,
)

MSK = ZoneInfo("Europe/Moscow")
CABINET = "conn-cabinet"
CALENDAR = "conn-calendar"
NOTES = "conn-notes"
T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0, *, day: int = 27, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def ev(
    id: str,
    title: str,
    start: datetime,
    end: datetime | None = None,
    *,
    conn: str = CABINET,
    space: str = "space-a",
    **kw: Any,
) -> ScheduleEventOut:
    fields: dict[str, Any] = {
        "kind": "other",
        "format": "unknown",
        "location": None,
        "teacher": None,
        "course_title": None,
        "url": None,
        "changed": False,
        "conflicts_with": [],
    }
    fields.update(kw)
    return ScheduleEventOut(
        id=id,
        title=title,
        starts_at=start,
        ends_at=end or start + timedelta(minutes=80),
        provenance=Provenance(
            connection_id=conn,
            space_id=space,
            source_url="https://fakeuni.test/x",
            fetched_at=T0,
            stale=False,
        ),
        sources=[EventSource(connection_id=conn, title=TITLES[conn])],
        **fields,
    )


TITLES = {
    CABINET: "Кабинет",
    CALENDAR: "Календарь",
    NOTES: "Заметки",
    "conn-a": "Альфа",
    "conn-b": "Бета",
}


def plan(*order: str, coverage: Coverage | None = None) -> SpacePlan:
    return SpacePlan(
        order=tuple(order),
        titles={c: TITLES[c] for c in order},
        zone=MSK,
        coverage=coverage,
    )


def run(events: list[ScheduleEventOut], *order: str, **kw: Any) -> list[ScheduleEventOut]:
    return merge.merge_events(events, {"space-a": plan(*(order or (CABINET, CALENDAR)), **kw)})


def by_id(events: list[ScheduleEventOut]) -> dict[str, ScheduleEventOut]:
    return {e.id: e for e in events}


# --- time ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shift_start", "shift_end", "joined"),
    [
        (0, 0, True),
        (10, 10, True),  # exactly the tolerance
        (-10, 10, True),
        (5, -10, True),
        (11, 0, True),  # beyond the tolerance, but 69 of 80 minutes still overlap: 0.86
        (0, 15, True),  # a quarter of an hour for the walk: overlap / union = 0.84
        (20, 20, False),  # 60 of 100 minutes: not the same class
        (45, 45, False),
    ],
)
def test_match_tolerance_and_strong_overlap(shift_start: int, shift_end: int, joined: bool) -> None:
    primary = ev("p", "Математический анализ", at(9), at(10, 20), conn=CABINET)
    other = ev(
        "s",
        "Математический анализ",
        at(9) + timedelta(minutes=shift_start),
        at(10, 20) + timedelta(minutes=shift_end),
        conn=CALENDAR,
    )
    merged = run([primary, other])
    assert (len(merged) == 1) is joined


def test_neighbouring_pairs_with_the_same_title_never_cross() -> None:
    """Two identical classes in a row: the calendar's second one joins the cabinet's second one."""
    first = ev("p1", "Английский язык", at(9), at(10, 30), conn=CABINET)
    second = ev("p2", "Английский язык", at(10, 40), at(12, 10), conn=CABINET)
    only_second = ev("s2", "Английский язык", at(10, 40), at(12, 10), conn=CALENDAR, location="305")
    merged = by_id(run([first, second, only_second]))
    assert sorted(merged) == ["p1", "p2"]
    assert merged["p2"].location == "305"
    assert merged["p1"].location is None


def test_a_long_event_is_not_a_class() -> None:
    day = ev("p", "Каникулы", at(0), at(23), conn=CABINET)
    note = ev("s", "Каникулы", at(0), at(23), conn=CALENDAR)
    assert len(run([day, note])) == 2  # shown, never merged


# --- titles -------------------------------------------------------------------------------------


def tokens(title: str) -> frozenset[str]:
    return merge.title_tokens(title)


def similar(a: str, b: str) -> float:
    return merge.title_similarity(tokens(a), tokens(b))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Математический анализ", "МАТЕМАТИЧЕСКИЙ АНАЛИЗ."),  # case and punctuation
        ("Математический анализ", "математический  анализ (лекция)"),  # the kind is not the name
        ("Лекция: Матанализ", "Матанализ"),
        ("Алгоритмы и структуры данных", "Алгоритмы, структуры данных"),  # a function word
        ("Теория вероятностей", "Теория вероятностей и математическая статистика"),  # contained
        ("Основы права", "Основы права РФ"),
        ("Linear Algebra (Lecture)", "linear algebra"),
        ("Ёлочные игрушки", "Елочные игрушки"),  # ё
        ("Анализ данных", "Анализа данных"),  # an ending
        ("Физика", "Физика 2"),  # a single contained word, only in the same slot
        ("Лекция", "Лекция"),  # nothing but the kind: the words stay
    ],
)
def test_similar_titles_match(a: str, b: str) -> None:
    assert similar(a, b) >= merge.TITLE_MATCH


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Математический анализ", "Физическая культура"),
        ("Физика 1", "Физика 2"),  # a digit tells classes apart
        ("Теория игр", "Теория вероятностей"),  # one shared word of three
        ("", "Математический анализ"),
        ("Лекция", "Семинар по праву"),
    ],
)
def test_different_titles_do_not_match(a: str, b: str) -> None:
    assert similar(a, b) < merge.TITLE_MATCH


def test_a_single_shared_word_is_not_enough_to_call_a_class_elsewhere() -> None:
    assert similar("Физика", "Физика 2") < merge.SIMILAR_ELSEWHERE
    assert similar("Теория вероятностей", "Теория вероятностей и статистика") >= (
        merge.SIMILAR_ELSEWHERE
    )


# --- filling and the primary's word -------------------------------------------------------------


def test_empty_fields_are_filled_from_the_secondary_and_the_primary_keeps_the_rest() -> None:
    primary = ev("p", "Алгоритмы", at(9), kind="lecture", location="305", address="ул. Учебная, 1")
    other = ev(
        "s",
        "алгоритмы (лекция)",
        at(9, 5),
        at(10, 25),
        conn=CALENDAR,
        kind="other",
        location=None,
        teacher="Иванов И. И.",
        group="Б-21",
        url="https://meet.fakeuni.test/algo",
        format="online",
        lesson_number=2,
        course_title="Алгоритмы и структуры данных",
        kind_text="Лекция",
        links=[EventLink(url="https://meet.fakeuni.test/room", label="Комната")],
        attributes=[EventAttribute(label="Корпус", value="Б")],
    )
    (one,) = run([primary, other])
    assert one.id == "p"  # the primary's identity
    assert (one.title, one.kind, one.starts_at, one.ends_at) == (
        "Алгоритмы",
        "lecture",
        at(9),
        at(10, 20),
    )
    assert one.location == "305"
    assert one.address == "ул. Учебная, 1"
    assert one.teacher == "Иванов И. И."
    assert one.group == "Б-21"
    assert one.url == "https://meet.fakeuni.test/algo"
    assert one.format == "online"
    assert one.lesson_number == 2
    assert one.kind_text == "Лекция"
    assert one.course_title == "Алгоритмы и структуры данных"
    assert [link.url for link in one.links] == ["https://meet.fakeuni.test/room"]
    assert [(a.label, a.value) for a in one.attributes] == [("Корпус", "Б")]
    assert [s.connection_id for s in one.sources] == [CABINET, CALENDAR]
    assert [s.title for s in one.sources] == ["Кабинет", "Календарь"]
    assert one.provenance.connection_id == CABINET  # the primary's provenance
    # a start five minutes later is a difference, however small: the primary still decides
    assert one.disagreement is not None
    assert one.disagreement.kind == "time"


def test_an_unknown_kind_is_filled_but_a_known_one_is_not_replaced() -> None:
    cabinet = ev("p", "Право", at(9), kind="other")
    calendar = ev("s", "Право", at(9), conn=CALENDAR, kind="seminar")
    assert run([cabinet, calendar])[0].kind == "seminar"
    lecture = ev("p", "Право", at(9), kind="lecture")
    assert run([lecture, calendar])[0].kind == "lecture"


def test_a_different_address_of_the_same_class_becomes_a_link_not_a_loss() -> None:
    cabinet = ev("p", "Право", at(9), url="https://cabinet.fakeuni.test/p")
    calendar = ev("s", "Право", at(9), conn=CALENDAR, url="https://meet.fakeuni.test/p")
    (one,) = run([cabinet, calendar])
    assert one.url == "https://cabinet.fakeuni.test/p"
    assert [(link.url, link.label) for link in one.links] == [
        ("https://meet.fakeuni.test/p", "Календарь")
    ]


def test_links_and_attributes_are_not_repeated_and_stay_within_the_contract() -> None:
    cabinet = ev(
        "p",
        "Право",
        at(9),
        links=[EventLink(url=f"https://a.fakeuni.test/{i}", label="a") for i in range(3)],
        attributes=[EventAttribute(label="Корпус", value="Б")],
    )
    calendar = ev(
        "s",
        "Право",
        at(9),
        conn=CALENDAR,
        links=[EventLink(url="https://a.fakeuni.test/0", label="again")]
        + [EventLink(url=f"https://b.fakeuni.test/{i}", label="b") for i in range(3)],
        attributes=[
            EventAttribute(label="корпус", value="А"),  # the label is taken
            EventAttribute(label="Этаж", value="б"),  # the value is taken
            EventAttribute(label="Этаж", value="2"),
        ],
    )
    (one,) = run([cabinet, calendar])
    assert len(one.links) == merge.MAX_LINKS
    assert len({link.url for link in one.links}) == len(one.links)
    assert [(a.label, a.value) for a in one.attributes] == [("Корпус", "Б"), ("Этаж", "2")]


def test_changed_on_either_side_marks_the_class() -> None:
    cabinet = ev("p", "Право", at(9))
    calendar = ev("s", "Право", at(9), conn=CALENDAR, changed=True)
    assert run([cabinet, calendar])[0].changed is True


# --- disagreements ------------------------------------------------------------------------------


def test_the_primary_wins_and_the_note_names_the_other_value() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), location="305")
    calendar = ev("s", "Алгоритмы", at(9), conn=CALENDAR, location="405")
    (one,) = run([cabinet, calendar])
    assert one.location == "305"
    assert one.disagreement is not None
    assert one.disagreement.kind == "location"
    assert one.disagreement.detail == "«Календарь» — ауд. 405"


def test_a_time_within_the_tolerance_that_differs_is_noted_in_the_zone_of_the_university() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), at(10, 20))
    calendar = ev("s", "Алгоритмы", at(9, 5), at(10, 30), conn=CALENDAR)
    (one,) = run([cabinet, calendar])
    assert (one.starts_at, one.ends_at) == (at(9), at(10, 20))
    assert one.disagreement is not None
    assert one.disagreement.kind == "time"
    assert one.disagreement.detail == "«Календарь» — время 12:05–13:30"  # 09:05 UTC is 12:05 MSK


def test_several_differences_are_one_note() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), location="305", teacher="Петров П. П.", group="Б-21")
    calendar = ev(
        "s",
        "Алгоритмы",
        at(9),
        conn=CALENDAR,
        location="Ауд. 405",
        teacher="Сидоров",
        group="Б-22",
    )
    (one,) = run([cabinet, calendar])
    assert one.disagreement is not None
    assert one.disagreement.kind == "several"
    assert one.disagreement.detail == ("«Календарь» — Ауд. 405, преподаватель Сидоров, группа Б-22")
    assert (one.location, one.teacher, one.group) == ("305", "Петров П. П.", "Б-21")


def test_a_long_note_is_cut_to_what_the_contract_allows() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), teacher="Петров", group="Б-21", address="ул. Учебная, 1")
    calendar = ev(
        "s",
        "Алгоритмы",
        at(9),
        conn=CALENDAR,
        teacher="Сидоров " * 40,
        group="В-" + "9" * 190,
        address="проспект Дальний, " + "д" * 280,
    )
    (one,) = run([cabinet, calendar])
    assert one.disagreement is not None
    assert len(one.disagreement.detail) <= merge.MAX_DETAIL
    assert one.disagreement.detail.endswith("…")


@pytest.mark.parametrize(
    ("kind", "mine", "theirs"),
    [
        ("location", "305", "ауд. 305"),
        ("location", "305", "Корпус Б, ауд. 305"),
        ("location", "Аудитория 305", "305"),
        ("teacher", "Иванов И. И.", "Иванов Иван Иванович"),
        ("teacher", "Иванов И. И.", "И. И. Иванов"),
        ("teacher", "Проф. Иванов", "Иванова"),
        ("teacher", "Иванов, Петров", "Петров"),
        ("address", "Покровский б-р, 11", "Покровский бульвар 11"),
        ("group", "ПМИ-231", "231 ПМИ"),
    ],
)
def test_the_same_value_written_differently_is_not_a_disagreement(
    kind: str, mine: str, theirs: str
) -> None:
    cabinet = ev("p", "Алгоритмы", at(9), **{kind: mine})
    calendar = ev("s", "Алгоритмы", at(9), conn=CALENDAR, **{kind: theirs})
    (one,) = run([cabinet, calendar])
    assert one.disagreement is None


def test_a_degree_alone_is_not_a_common_name() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), teacher="Проф. Петров")
    calendar = ev("s", "Алгоритмы", at(9), conn=CALENDAR, teacher="Проф. Иванов")
    (one,) = run([cabinet, calendar])
    assert one.disagreement is not None
    assert one.disagreement.kind == "teacher"


def test_an_empty_value_is_filled_never_a_disagreement() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), location="  ")
    calendar = ev("s", "Алгоритмы", at(9), conn=CALENDAR, location="405")
    (one,) = run([cabinet, calendar])
    assert one.location == "405"
    assert one.disagreement is None


# --- what is not matched ------------------------------------------------------------------------


def test_a_secondary_event_with_no_match_is_shown_as_its_own() -> None:
    cabinet = ev("p", "Алгоритмы", at(9))
    extra = ev("s", "Йога", at(18), conn=CALENDAR)
    merged = by_id(run([cabinet, extra]))
    assert set(merged) == {"p", "s"}
    assert [s.connection_id for s in merged["s"].sources] == [CALENDAR]
    assert merged["s"].disagreement is None  # the cabinet has no yoga to compare with


COVERED = Coverage.span(at(0, day=19), at(23, day=30))


def test_a_class_the_primary_has_elsewhere_but_not_here_is_marked_not_hidden() -> None:
    last_week = ev("p", "Алгоритмы", at(9, day=20), conn=CABINET)
    this_week = ev("s", "Алгоритмы", at(9, day=27), conn=CALENDAR)
    merged = by_id(run([last_week, this_week], coverage=COVERED))
    assert set(merged) == {"p", "s"}  # nothing is hidden
    note = merged["s"].disagreement
    assert note is not None
    assert note.kind == "not_in_primary"
    assert note.detail.startswith("Нет в основном источнике «Кабинет»")
    assert merged["p"].disagreement is None


def test_the_marker_needs_a_similar_class_within_seven_days() -> None:
    far = ev("p", "Алгоритмы", at(9, day=19), conn=CABINET)
    this_week = ev("s", "Алгоритмы", at(9, day=27), conn=CALENDAR)
    assert by_id(run([far, this_week], coverage=COVERED))["s"].disagreement is None
    near = ev("p", "Алгоритмы", at(9, day=20), conn=CABINET)
    assert by_id(run([near, this_week], coverage=COVERED))["s"].disagreement is not None
    other = ev("p", "Физическая культура", at(9, day=24), conn=CABINET)
    assert by_id(run([other, this_week], coverage=COVERED))["s"].disagreement is None


def test_no_marker_where_the_primary_has_no_data_at_all() -> None:
    """The cabinet reaches four weeks ahead: a class after that is not "missing" in it."""
    before = ev("p", "Алгоритмы", at(9, day=25, month=11), conn=CABINET)
    after = ev("s", "Алгоритмы", at(9, day=30, month=11), conn=CALENDAR)
    edge = Coverage.span(at(0, day=19), at(11, day=26, month=11))
    assert by_id(run([before, after], coverage=edge))["s"].disagreement is None
    assert by_id(run([before, after], coverage=None))["s"].disagreement is None
    wider = Coverage.span(at(0, day=19), at(23, day=15, month=12))
    assert by_id(run([before, after], coverage=wider))["s"].disagreement is not None


def test_a_duplicate_in_the_secondary_is_not_missing_from_the_primary() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), conn=CABINET)
    calendar = [
        ev("s1", "Алгоритмы", at(9), conn=CALENDAR),
        ev("s2", "Алгоритмы", at(9), conn=CALENDAR),
    ]
    merged = by_id(run([cabinet, *calendar], coverage=COVERED))
    assert set(merged) == {"p", "s2"}  # the first of the two takes the slot (ids decide)
    assert merged["s2"].disagreement is None  # the cabinet does have it at this slot


# --- more than two sources, and what never merges -----------------------------------------------


def test_a_third_source_joins_what_the_second_left_alone() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), conn=CABINET, location="305")
    calendar = ev("c", "Йога", at(18), conn=CALENDAR)
    notes = ev("n", "Йога", at(18), conn=NOTES, teacher="Сидорова")
    merged = by_id(run([cabinet, calendar, notes], CABINET, CALENDAR, NOTES))
    assert set(merged) == {"p", "c"}
    assert [s.connection_id for s in merged["c"].sources] == [CALENDAR, NOTES]
    assert merged["c"].teacher == "Сидорова"


def test_the_order_decides_who_is_the_base() -> None:
    cabinet = ev("p", "Алгоритмы", at(9), conn=CABINET, location="305")
    calendar = ev("c", "Алгоритмы", at(9), conn=CALENDAR, location="405")
    (one,) = merge.merge_events([cabinet, calendar], {"space-a": plan(CALENDAR, CABINET)})
    assert one.id == "c"
    assert one.location == "405"
    assert one.disagreement is not None
    assert "305" in one.disagreement.detail
    assert [s.title for s in one.sources] == ["Календарь", "Кабинет"]


def test_different_spaces_never_merge() -> None:
    a = ev("a", "Алгоритмы", at(9), conn=CABINET, space="space-a")
    b = ev("b", "Алгоритмы", at(9), conn=CALENDAR, space="space-b")
    # even with a plan for space-a that lists the other connection: the space decides
    merged = merge.merge_events([a, b], {"space-a": plan(CABINET, CALENDAR)})
    assert {e.id for e in merged} == {"a", "b"}
    other = ev("c", "Алгоритмы", at(9), conn=CALENDAR, space="space-b")
    both = merge.merge_events(
        [a, other],
        {"space-a": plan(CABINET, CALENDAR), "space-b": plan(CABINET, CALENDAR)},
    )
    assert {e.id for e in both} == {"a", "c"}


def test_a_space_without_a_plan_passes_through_untouched() -> None:
    a = ev("a", "Алгоритмы", at(9), conn=CABINET)
    b = ev("b", "Алгоритмы", at(9), conn=CALENDAR)
    assert merge.merge_events([a, b], {}) == [a, b]


def test_the_result_is_the_same_whatever_the_order_of_the_rows() -> None:
    rows = [
        ev("p1", "Алгоритмы", at(9), conn=CABINET),
        ev("p2", "Алгоритмы", at(10, 40), at(12, 10), conn=CABINET),
        ev("s1", "Алгоритмы", at(9), conn=CALENDAR, teacher="Иванов"),
        ev("s2", "Алгоритмы", at(10, 40), at(12, 10), conn=CALENDAR, teacher="Петров"),
    ]
    forward = run(rows)
    backward = run(list(reversed(rows)))
    assert [(e.id, e.teacher) for e in forward] == [(e.id, e.teacher) for e in backward]


# --- cost ---------------------------------------------------------------------------------------


def test_three_months_of_two_sources_merge_fast() -> None:
    """90 days, six classes a day in each source, partly different: well under a second."""
    rows: list[ScheduleEventOut] = []
    day0 = datetime(2026, 9, 1, tzinfo=UTC)
    titles = ["Алгоритмы", "Матанализ", "Физика", "Английский язык", "Право", "История"]
    for d in range(90):
        for n, title in enumerate(titles):
            start = day0 + timedelta(days=d, hours=6 + n * 2)
            rows.append(ev(f"p{d}-{n}", title, start, conn=CABINET, location=str(300 + n)))
            shifted = start + timedelta(minutes=5 if n % 2 else 0)
            rows.append(
                ev(f"s{d}-{n}", title, shifted, conn=CALENDAR, teacher=f"Преподаватель {n}")
            )
    coverage = Coverage.span(day0, day0 + timedelta(days=91))
    started = time.perf_counter()
    merged = run(rows, coverage=coverage)
    elapsed = time.perf_counter() - started
    assert len(merged) == 90 * len(titles)
    assert all(len(e.sources) == 2 for e in merged)
    assert elapsed < 2.0, f"merge took {elapsed:.2f} s"


# --- comparing two sources ----------------------------------------------------------------------


def test_compare_counts_pairs_and_what_only_one_side_has() -> None:
    left = [
        ev("a1", "Алгоритмы", at(9), location="305"),
        ev("a2", "Физика", at(11), location="101"),
        ev("a3", "Право", at(13)),
    ]
    right = [
        ev("b1", "Алгоритмы", at(9), conn=CALENDAR, location="405", teacher="Иванов"),
        ev("b2", "Физика", at(11), conn=CALENDAR, location="101"),
        ev("b3", "Йога", at(18), conn=CALENDAR),
    ]
    result = merge.compare(left, right, MSK)
    assert [(p.a.id, p.b.id) for p in result.pairs] == [("a1", "b1"), ("a2", "b2")]
    assert [e.id for e in result.only_a] == ["a3"]
    assert [e.id for e in result.only_b] == ["b3"]
    first, second = result.pairs
    assert [k for k, _ in first.differences] == ["location"]
    assert second.differences == ()


# --- classes that are not the same class (the review of 5821546) -------------------------------


def test_a_lecture_and_a_seminar_of_one_course_at_one_hour_are_two_classes() -> None:
    lecture = ev("p", "Матанализ", at(9), kind="lecture", location="101")
    seminar = ev(
        "s",
        "Матанализ",
        at(9),
        conn=CALENDAR,
        kind="seminar",
        location="205",
        url="https://meet.fakeuni.test/seminar",
        format="online",
    )
    merged = by_id(run([lecture, seminar]))
    assert set(merged) == {"p", "s"}
    assert merged["p"].url is None  # the seminar's online room is not the lecture's
    assert merged["p"].format == "unknown"
    assert merged["p"].links == []
    assert [s.connection_id for s in merged["p"].sources] == [CABINET]
    assert merged["p"].disagreement is None


def test_a_class_and_its_exam_are_two_classes() -> None:
    seminar = ev("p", "Матанализ", at(9), kind="seminar", location="101")
    exam = ev(
        "s", "Матанализ, контрольная работа", at(9), conn=CALENDAR, kind="exam", location="101"
    )
    merged = by_id(run([seminar, exam]))
    assert set(merged) == {"p", "s"}
    assert merged["p"].kind == "seminar"
    assert merged["p"].disagreement is None


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ("Матанализ", "Экзамен: Матанализ"),
        ("Матанализ", "Матанализ (контрольная работа)"),
        ("Матанализ", "Зачёт по матанализу"),
        ("Матанализ", "Консультация: Матанализ"),
        ("Матанализ", "Коллоквиум Матанализ"),
        ("Linear algebra", "Linear algebra exam"),
        ("Linear algebra", "Linear algebra quiz"),
        ("Экзамен: Матанализ", "Зачёт: Матанализ"),
    ],
)
def test_an_assessment_word_in_only_one_title_keeps_the_classes_apart(
    mine: str, theirs: str
) -> None:
    merged = run([ev("p", mine, at(9)), ev("s", theirs, at(9), conn=CALENDAR)])
    assert {e.id for e in merged} == {"p", "s"}


def test_the_same_assessment_word_in_both_titles_is_the_same_exam() -> None:
    merged = run(
        [
            ev("p", "Экзамен: Матанализ", at(9), kind="exam", location="101"),
            ev("s", "Матанализ — экзамен", at(9), conn=CALENDAR, teacher="Иванов"),
        ]
    )
    (one,) = merged
    assert one.id == "p"
    assert one.teacher == "Иванов"


def test_the_kind_a_title_names_keeps_a_lecture_and_a_seminar_apart_even_without_a_kind() -> None:
    merged = run(
        [ev("p", "Лекция: Матанализ", at(9)), ev("s", "Семинар: Матанализ", at(9), conn=CALENDAR)]
    )
    assert {e.id for e in merged} == {"p", "s"}
    (one,) = run([ev("p", "Лекция: Матанализ", at(9)), ev("s", "Матанализ", at(9), conn=CALENDAR)])
    assert one.id == "p"


def test_an_unknown_kind_does_not_keep_two_classes_apart() -> None:
    (one,) = run(
        [
            ev("p", "Матанализ", at(9), kind="seminar"),
            ev("s", "Матанализ", at(9), conn=CALENDAR, kind="other", teacher="Иванов"),
        ]
    )
    assert (one.kind, one.teacher) == ("seminar", "Иванов")


def test_an_extra_word_in_the_title_of_the_other_source_is_noted() -> None:
    cabinet = ev("p", "Матанализ", at(9))
    calendar = ev("s", "Матанализ (подгруппа Б)", at(9), conn=CALENDAR)
    (one,) = run([cabinet, calendar])
    assert one.title == "Матанализ"
    assert one.disagreement is not None
    assert one.disagreement.kind == "title"
    assert one.disagreement.detail == "«Календарь» — название «Матанализ (подгруппа Б)»"


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ("Матанализ", "Матанализ (лекция)"),  # only the kind of the class
        ("Матанализ", "МАТАНАЛИЗ."),
        ("Теория вероятностей и статистика", "Теория вероятностей"),  # the other has fewer words
    ],
)
def test_no_title_note_without_words_of_their_own(mine: str, theirs: str) -> None:
    (one,) = run([ev("p", mine, at(9)), ev("s", theirs, at(9), conn=CALENDAR)])
    assert one.disagreement is None


def test_links_of_the_other_source_carry_the_name_of_that_source() -> None:
    cabinet = ev(
        "p", "Право", at(9), links=[EventLink(url="https://a.fakeuni.test/1", label="Материалы")]
    )
    calendar = ev(
        "s",
        "Право",
        at(9),
        conn=CALENDAR,
        links=[EventLink(url="https://meet.fakeuni.test/room", label="Комната")],
    )
    (one,) = run([cabinet, calendar])
    assert [(link.url, link.label) for link in one.links] == [
        ("https://a.fakeuni.test/1", "Материалы"),
        ("https://meet.fakeuni.test/room", "Календарь: Комната"),
    ]


def test_the_url_of_the_primary_is_never_replaced() -> None:
    cabinet = ev("p", "Право", at(9), url="https://cabinet.fakeuni.test/p")
    calendar = ev("s", "Право", at(9), conn=CALENDAR, url="https://calendar.fakeuni.test/event/42")
    (one,) = run([cabinet, calendar])
    assert one.url == "https://cabinet.fakeuni.test/p"
    assert "https://calendar.fakeuni.test/event/42" in [link.url for link in one.links]


# --- time and length ----------------------------------------------------------------------------


def test_events_with_no_length_still_meet_themselves() -> None:
    (one,) = run(
        [ev("p", "Матанализ", at(9), at(9)), ev("s", "Матанализ", at(9), at(9), conn=CALENDAR)]
    )
    assert one.id == "p"
    assert [s.connection_id for s in one.sources] == [CABINET, CALENDAR]
    assert one.disagreement is None


def test_a_reminder_does_not_swallow_the_class_at_the_same_minute() -> None:
    merged = run(
        [
            ev("p", "Алгоритмы", at(9), at(10, 20)),
            ev("s", "Напомнить про домашку", at(9), at(9), conn=CALENDAR),
        ]
    )
    assert {e.id for e in merged} == {"p", "s"}


@pytest.mark.parametrize(
    ("start_shift", "end_shift", "noted"),
    [
        (0, 0, False),
        (0, 10, False),  # a calendar that rounds the end of a class up is noise
        (0, 15, False),
        (0, -15, False),
        (0, 16, True),
        (0, 20, True),
        (1, 0, True),  # the start moved, however little
        (-5, 0, True),
    ],
)
def test_a_time_is_noted_when_the_start_differs_or_the_end_by_more_than_a_quarter_of_an_hour(
    start_shift: int, end_shift: int, noted: bool
) -> None:
    cabinet = ev("p", "Алгоритмы", at(9), at(10, 20))
    calendar = ev(
        "s",
        "Алгоритмы",
        at(9) + timedelta(minutes=start_shift),
        at(10, 20) + timedelta(minutes=end_shift),
        conn=CALENDAR,
    )
    (one,) = run([cabinet, calendar])
    assert (one.disagreement is not None and one.disagreement.kind == "time") is noted


# --- the order of the sources -------------------------------------------------------------------


def test_sources_the_plan_does_not_list_come_after_it_in_the_order_of_their_ids() -> None:
    rows = [
        ev("p", "Право", at(9), conn=CABINET, location="305"),
        ev("b", "Право", at(9), conn="conn-b", location="405", teacher="Бета"),
        ev("a", "Право", at(9), conn="conn-a", location="505", teacher="Альфа"),
    ]
    plan_ = {"space-a": plan(CABINET)}
    (one,) = merge.merge_events(rows, plan_)
    assert [s.connection_id for s in one.sources] == [CABINET, "conn-a", "conn-b"]
    assert one.teacher == "Альфа"  # the earlier id fills first
    assert merge.merge_events(list(reversed(rows)), plan_) == [one]


# --- how far the primary's regular classes reach ------------------------------------------------


def test_one_far_exam_does_not_stretch_the_reach_of_the_primary() -> None:
    last = ev("p1", "Матанализ", at(9, day=27))
    exam = ev("ex", "Экзамен", at(9, day=20, month=12), kind="exam")
    later = ev("s", "Матанализ", at(9, day=3, month=11), conn=CALENDAR)
    classes = [
        (e.starts_at, e.ends_at)
        for e in (last, exam)
        if merge.is_regular(e.kind, e.title, e.starts_at, e.ends_at)
    ]
    coverage = Coverage.of_classes(classes)
    assert coverage is not None
    assert not coverage.reaches(later.starts_at, later.ends_at)
    out = by_id(run([last, exam, later], coverage=coverage))
    assert out["s"].disagreement is None  # the primary's classes end on 27.10: nothing is "missing"


def test_a_long_gap_splits_the_reach_and_a_short_one_does_not() -> None:
    day = timedelta(days=1)
    first = at(9, day=1)
    classes = [(first + day * n, first + day * n + timedelta(minutes=80)) for n in (0, 3, 10)]
    classes.append((first + day * 40, first + day * 40 + timedelta(minutes=80)))  # after a break
    coverage = Coverage.of_classes(classes)
    assert coverage is not None
    assert len(coverage.spans) == 2
    assert coverage.reaches(first + day * 5, first + day * 5 + timedelta(minutes=80))
    assert not coverage.reaches(first + day * 25, first + day * 25 + timedelta(minutes=80))
    assert Coverage.of_classes([]) is None


@pytest.mark.parametrize(
    ("kind", "title", "regular"),
    [
        ("lecture", "Алгоритмы", True),
        ("other", "Алгоритмы", True),
        ("exam", "Алгоритмы", False),
        ("consultation", "Алгоритмы", False),
        ("other", "Экзамен по алгоритмам", False),
        ("other", "Зачёт", False),
    ],
)
def test_what_counts_as_a_regular_class(kind: str, title: str, regular: bool) -> None:
    assert merge.is_regular(kind, title, at(9), at(10, 20)) is regular
    assert merge.is_regular("lecture", "Алгоритмы", at(0), at(23)) is False  # not a class's length


def test_a_consultation_of_a_course_is_not_a_missing_lecture() -> None:
    lectures = [ev("p1", "Матанализ", at(9, day=26)), ev("p2", "Матанализ", at(9, day=28))]
    consultation = ev("s", "Консультация: Матанализ", at(15, day=30), conn=CALENDAR)
    out = by_id(run([*lectures, consultation], coverage=COVERED))
    assert out["s"].disagreement is None


# --- the probes of the review: invariants over many random days --------------------------------


def test_permuting_the_rows_never_changes_who_absorbs_whom() -> None:
    p1 = ev("p1", "Матанализ", at(9), location="101")
    p2 = ev("p2", "Матанализ", at(9), location="102")
    s = ev("s", "Матанализ", at(9), conn=CALENDAR, teacher="Иванов")
    seen = {
        tuple((e.id, e.teacher) for e in run(list(perm)))
        for perm in itertools.permutations([p1, p2, s])
    }
    assert len(seen) == 1


def test_the_same_instant_in_another_zone_is_the_same_class() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 25, 9, 0, tzinfo=berlin)  # the day the clocks go back
    cabinet = ev("p", "Матанализ", start, start + timedelta(minutes=90))
    calendar = ev(
        "s",
        "Матанализ",
        start.astimezone(UTC),
        start.astimezone(UTC) + timedelta(minutes=90),
        conn=CALENDAR,
    )
    assert len(run([cabinet, calendar])) == 1


def test_random_days_lose_nothing_repeat_no_id_and_ignore_the_order_of_rows() -> None:
    rnd = random.Random(1)
    titles = ["Матанализ", "Физика", "История России", "Иностранный язык", "Программирование"]
    for _ in range(300):
        rows = []
        for k in range(rnd.randint(1, 12)):
            start = at(8) + timedelta(minutes=10 * rnd.randint(0, 60))
            length = timedelta(minutes=rnd.choice([80, 90, 60, 0]))
            rows.append(
                ev(
                    f"e{k}",
                    rnd.choice(titles),
                    start,
                    start + length,
                    conn=rnd.choice([CABINET, CALENDAR, NOTES]),
                )
            )
        out = run(rows, CABINET, CALENDAR, NOTES)
        ids = [e.id for e in out]
        assert len(ids) == len(set(ids))
        assert sum(len(e.sources) for e in out) == len(rows), (
            "a class vanished or was counted twice"
        )
        again = run(list(reversed(rows)), CABINET, CALENDAR, NOTES)
        assert [e.model_dump() for e in out] == [e.model_dump() for e in again]
