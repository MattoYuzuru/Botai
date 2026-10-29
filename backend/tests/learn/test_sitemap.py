"""The map of a source is shared, so it keeps structure and nothing that belongs to a student."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.browser import ObservedRequest, PageSnapshot
from botai.learn.horizon import plural, thin_question
from botai.learn.redact import Redactor
from botai.learn.sitemap import build_sitemap, get_site_map, merge_sitemaps, save_site_map
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"


def call(path: str, status: int = 200) -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=ORIGIN + path,
        status=status,
        resource_type="fetch",
        response_content_type="application/json",
    )


def test_navigation_routes_and_question_endpoints_without_identifiers_or_values() -> None:
    redactor = Redactor()
    redactor.know("Иванова Анна")
    built: dict[str, Any] = build_sitemap(
        ORIGIN,
        redactor,
        [PageSnapshot(url=ORIGIN + "/students/74219/schedule?begin_date=2026-10-26")],
        [
            ("Расписание", ORIGIN + "/schedule"),
            ("Иванова Анна", ORIGIN + "/profile"),
            ("Частые вопросы", ORIGIN + "/faq"),
            ("Чужой", "https://other.test/x"),
            ("Вход", ORIGIN + "/cb?access_token=abc.def.ghi"),
        ],
        [
            call("/api/get-faq?q=how"),
            call("/api/search?query=exam&page=1"),
            call("/api/get-myschedule?begin_date=2026-10-26"),  # data, not a question endpoint
            call("/api/search?query=failed", 500),
        ],
    )
    assert {"label": "Расписание", "path": "/schedule"} in built["navigation"]
    assert all("Иванова" not in entry["label"] for entry in built["navigation"])
    assert not any("other.test" in route for route in built["routes"])
    assert not any("access_token" in route for route in built["routes"])
    assert "/students/{id}/schedule" in built["routes"]  # the identifier is generalised
    assert built["endpoints"] == [
        {"kind": "faq", "path": "/api/get-faq", "queryKeys": ["q"]},
        {"kind": "search", "path": "/api/search", "queryKeys": ["page", "query"]},
    ]
    assert "begin_date" not in str(built)  # query values and data endpoints are not kept


def test_a_later_observation_adds_to_the_map_within_its_caps() -> None:
    first = {
        "navigation": [{"label": "Расписание", "path": "/schedule"}],
        "routes": ["/schedule"],
        "endpoints": [],
    }
    second = {
        "navigation": [
            {"label": "Задания", "path": "/tasks"},
            {"label": "Расписание на неделю", "path": "/schedule"},
        ],
        "routes": ["/tasks"],
        "endpoints": [{"kind": "faq", "path": "/api/get-faq", "queryKeys": []}],
    }
    merged = merge_sitemaps(first, second)
    assert [e["path"] for e in merged["navigation"]] == ["/tasks", "/schedule"]
    assert merged["navigation"][1]["label"] == "Расписание на неделю"  # the newest wording
    assert merged["routes"] == ["/tasks", "/schedule"]
    big = {"navigation": [{"label": str(i), "path": f"/p{i}"} for i in range(80)]}
    assert len(merge_sitemaps(None, big)["navigation"]) == 40


async def test_the_map_is_saved_once_per_origin_and_read_back(db: AsyncSession) -> None:
    assert await get_site_map(db, ORIGIN) is None
    await save_site_map(db, ORIGIN, {"navigation": [{"label": "A", "path": "/a"}]}, NOW)
    await save_site_map(db, ORIGIN, {"routes": ["/b"], "endpoints": []}, NOW + timedelta(hours=1))
    stored = await get_site_map(db, ORIGIN)
    assert stored == {
        "navigation": [{"label": "A", "path": "/a"}],
        "routes": ["/b"],
        "endpoints": [],
    }
    await save_site_map(db, "https://empty.test", {}, NOW)  # nothing observed: no row
    assert await get_site_map(db, "https://empty.test") is None


def test_russian_agreement_and_the_question_carry_counts_and_dates_only() -> None:
    assert [plural(n, ("занятие", "занятия", "занятий")) for n in (0, 1, 2, 5, 11, 21, 24)] == [
        "занятий",
        "занятие",
        "занятия",
        "занятий",
        "занятий",
        "занятие",
        "занятия",
    ]
    from datetime import date

    text = thin_question(
        2,
        kind_forms=("занятие", "занятия", "занятий"),
        span=(date(2026, 10, 19), date(2026, 11, 29)),
        per_window=[0, 2, 0, 0, 0, 0],
        unit="неделям",
    )
    assert text.startswith("Вижу 2 занятия за 19.10–29.11. По неделям: 0, 2, 0, 0, 0, 0.")
    assert thin_question(
        0, kind_forms=("запись", "записи", "записей"), span=None, per_window=[], unit="периодам"
    ).startswith("Не вижу записей.")
