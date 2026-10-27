from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.sources import normalize, schedule
from botai.sources.adapter import AdapterResult, EndpointOutcome, ExtractedDocument, Record
from botai.sources.runner import assess
from tests.sources import recipes
from tests.sources.helpers import NOW
from tests.sources.support import seed_connection

MSK = ZoneInfo("Europe/Moscow")


def rec(
    kind: Any, fields: dict[str, Any], ext: str = "x1", url: str = "https://fakeuni.test/p"
) -> Record:
    return Record(kind, ext, fields, url)


# --- coercion -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Лекция", "lecture"),
        ("lecture (online)", "lecture"),
        ("Практическое занятие", "seminar"),
        ("Лабораторная работа", "lab"),
        ("Экзамен", "exam"),
        ("Консультация перед экзаменом", "exam"),
        ("Консультация", "consultation"),
        ("что-то иное", "other"),
        (None, "other"),
    ],
)
def test_class_kind_synonyms(raw: str | None, expected: str) -> None:
    fields = {
        "title": "T",
        "startsAt": "2026-10-26T06:30:00+00:00",
        "endsAt": "2026-10-26T07:50:00+00:00",
    }
    row = normalize.coerce("scheduleEvents", rec("scheduleEvents", {**fields, "kind": raw}), MSK)
    assert row is not None
    assert row["kind"] == expected


def test_event_duration_replaces_a_missing_end_and_bad_ranges_are_rejected() -> None:
    base = {"title": "T", "startsAt": "2026-10-26T06:30:00+00:00"}
    row = normalize.coerce(
        "scheduleEvents", rec("scheduleEvents", {**base, "durationMinutes": 80}), MSK
    )
    assert row is not None
    assert row["ends_at"] == datetime(2026, 10, 26, 7, 50, tzinfo=UTC)
    assert normalize.coerce("scheduleEvents", rec("scheduleEvents", base), MSK) is None
    backwards = {**base, "endsAt": "2026-10-26T05:00:00+00:00"}
    assert normalize.coerce("scheduleEvents", rec("scheduleEvents", backwards), MSK) is None


def test_links_must_be_http_without_credentials_and_are_resolved() -> None:
    def url_of(value: str) -> str | None:
        row = normalize.coerce(
            "news", rec("news", {"title": "T", "publishedAt": "2026-10-01", "url": value}), MSK
        )
        assert row is not None
        return row["url"]

    assert url_of("/news/1") == "https://fakeuni.test/news/1"
    assert url_of("javascript:alert(1)") is None
    assert url_of("https://user:pw@evil.test/x") is None
    assert url_of("data:text/html,x") is None


def test_grade_values_are_text_and_states_have_synonyms() -> None:
    row = normalize.coerce("grades", rec("grades", {"value": 8.0, "weight": 0.2}), MSK)
    assert row is not None
    assert row["value"] == "8"
    row = normalize.coerce("grades", rec("grades", {"value": 8.5}), MSK)
    assert row is not None
    assert row["value"] == "8.5"
    assert normalize.coerce("grades", rec("grades", {"value": True}), MSK) is None
    due = {"title": "T", "dueAt": "2026-10-28T20:00:00+00:00"}
    states = {"Сдано": "submitted", "Не сдано": "open", "overdue": "missed", "??": "unknown"}
    for raw, expected in states.items():
        row = normalize.coerce("deadlines", rec("deadlines", {**due, "state": raw}), MSK)
        assert row is not None
        assert row["state"] == expected


def test_required_columns_and_length_limits() -> None:
    assert normalize.coerce("deadlines", rec("deadlines", {"title": "T"}), MSK) is None
    assert normalize.coerce("people", rec("people", {"name": "  "}), MSK) is None
    row = normalize.coerce("courses", rec("courses", {"title": "я" * 1000, "code": "c" * 200}), MSK)
    assert row is not None
    assert len(row["title"]) == 300
    assert len(row["code"]) == 60


# --- persistence ----------------------------------------------------------------------------


async def test_invalid_records_are_counted_and_skipped(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    good = rec("courses", {"title": "Алгебра"}, "c1")
    bad = rec("courses", {"code": "no title"}, "c2")
    result = AdapterResult([good, bad], NOW, [EndpointOutcome("c", "courses", ok=True)])
    stats = await normalize.apply_result(
        db,
        user_id=seeded.user_id,
        connection_id=seeded.connection_id,
        result=result,
        now=NOW,
        tz=MSK,
    )
    assert stats["courses"].inserted == 1
    assert stats["courses"].invalid == 1
    count = (
        await db.execute(select(func.count()).select_from(normalize.MODELS["courses"]))
    ).scalar_one()
    assert count == 1


async def test_a_vanished_course_is_archived_and_returns_unarchived(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    kwargs: dict[str, Any] = {
        "user_id": seeded.user_id,
        "connection_id": seeded.connection_id,
        "tz": MSK,
    }
    both = AdapterResult(
        [rec("courses", {"title": "A"}, "a"), rec("courses", {"title": "B"}, "b")],
        NOW,
        [EndpointOutcome("c", "courses", ok=True)],
    )
    await normalize.apply_result(db, result=both, now=NOW, **kwargs)
    later = NOW + timedelta(days=1)
    only_a = AdapterResult(
        [rec("courses", {"title": "A"}, "a")], later, [EndpointOutcome("c", "courses", ok=True)]
    )
    await normalize.apply_result(db, result=only_a, now=later, **kwargs)

    async def archived() -> dict[str, bool]:
        db.expire_all()
        rows = await db.execute(select(normalize.m.Course.external_id, normalize.m.Course.archived))
        return {r[0]: r[1] for r in rows.all()}

    assert await archived() == {"a": False, "b": True}
    await normalize.apply_result(db, result=both, now=later + timedelta(days=1), **kwargs)
    assert await archived() == {"a": False, "b": False}


async def test_a_partial_kind_never_marks_rows_missing(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    kwargs: dict[str, Any] = {
        "user_id": seeded.user_id,
        "connection_id": seeded.connection_id,
        "tz": MSK,
    }
    first = AdapterResult(
        [rec("courses", {"title": "A"}, "a")], NOW, [EndpointOutcome("c", "courses", ok=True)]
    )
    await normalize.apply_result(db, result=first, now=NOW, **kwargs)
    failed = AdapterResult(
        [],
        NOW + timedelta(days=1),
        [EndpointOutcome("c", "courses", ok=False, problem="not_found")],
    )
    stats = await normalize.apply_result(db, result=failed, now=NOW + timedelta(days=1), **kwargs)
    assert "courses" not in stats or stats["courses"].missing == 0
    db.expire_all()
    archived = (await db.execute(select(normalize.m.Course.archived))).scalar_one()
    assert archived is False


async def test_documents_are_replaced_when_the_hash_changes(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    kwargs: dict[str, Any] = {
        "user_id": seeded.user_id,
        "space_id": seeded.space_id,
        "connection_id": seeded.connection_id,
    }

    def doc(digest: str, chunks: list[str]) -> ExtractedDocument:
        return ExtractedDocument("Правила", "file", digest, chunks)

    first = await normalize.store_document(
        db, doc=doc("a" * 64, ["один", "два"]), now=NOW, **kwargs
    )
    same = await normalize.store_document(
        db, doc=doc("a" * 64, ["один", "два"]), now=NOW + timedelta(hours=1), **kwargs
    )
    assert same == first
    changed = await normalize.store_document(
        db, doc=doc("b" * 64, ["три"]), now=NOW + timedelta(hours=2), **kwargs
    )
    assert changed != first
    docs = (await db.execute(select(func.count()).select_from(normalize.m.Document))).scalar_one()
    chunks = (await db.execute(select(normalize.m.Chunk.text))).scalars().all()
    assert docs == 1
    assert list(chunks) == ["три"]


async def test_chunks_are_searchable_in_russian(db: AsyncSession) -> None:
    seeded = await seed_connection(db)
    await normalize.store_document(
        db,
        user_id=seeded.user_id,
        space_id=seeded.space_id,
        connection_id=seeded.connection_id,
        doc=ExtractedDocument(
            "Правила", "file", "c" * 64, ["Заявление подаётся за три дня до экзамена"]
        ),
        now=NOW,
    )
    hit = await db.execute(
        select(normalize.m.Chunk.text).where(
            normalize.m.Chunk.tsv.op("@@")(func.plainto_tsquery("russian", "заявление экзамена"))
        )
    )
    assert hit.scalar_one().startswith("Заявление")


# --- verdicts and freshness -----------------------------------------------------------------


def outcome(kind: Any, problem: Any = None, ok: bool = True) -> EndpointOutcome:
    return EndpointOutcome("e", kind, ok=ok, problem=problem)


def test_assess_separates_drift_from_an_empty_student() -> None:
    spec = recipes.recipe_spec()
    empty = AdapterResult(
        [], NOW, [outcome("deadlines", "empty"), outcome("scheduleEvents", "empty")]
    )
    no_history = assess(spec, empty, {})
    assert no_history.drift == []
    assert no_history.suspicious == set()
    with_history = assess(spec, empty, {"deadlines": 2})
    assert [d.problem for d in with_history.drift] == ["empty"]
    assert with_history.suspicious == {"deadlines"}
    structural = AdapterResult([], NOW, [outcome("scheduleEvents", "not_found", ok=False)])
    verdict = assess(spec, structural, {})
    assert [(d.endpoint_id, d.problem) for d in verdict.drift] == [("e", "not_found")]


def test_freshness_defaults_by_adapter() -> None:
    assert schedule.freshness_of(recipes.recipe_spec()) == schedule.Freshness(180, 720)
    assert schedule.freshness_of({"adapter": "moodle_ws"}).every_minutes == 120
    assert schedule.freshness_of({"adapter": "ical"}).every_minutes == 180
    doc = {"adapter": "document", "source": {"kind": "url", "refreshEveryMinutes": 60}}
    assert schedule.freshness_of(doc).every_minutes == 60
    assert (
        schedule.freshness_of({"adapter": "document", "source": {"kind": "upload"}}).every_minutes
        == 0
    )
    assert (
        schedule.next_sync_at(
            NOW, schedule.freshness_of({"adapter": "document", "source": {}}), "c"
        )
        is None
    )


def test_next_sync_is_spread_and_bounded() -> None:
    fresh = schedule.Freshness(180, 720)
    times = {schedule.next_sync_at(NOW, fresh, f"conn-{i}") for i in range(20)}
    assert len(times) > 10  # jitter separates connections of one origin
    assert all(
        NOW + timedelta(minutes=180) <= t <= NOW + timedelta(minutes=198) for t in times if t
    )
    assert schedule.is_stale(NOW, NOW - timedelta(minutes=721), fresh)
    assert not schedule.is_stale(NOW, NOW - timedelta(minutes=700), fresh)
    assert not schedule.is_stale(NOW, None, fresh)
