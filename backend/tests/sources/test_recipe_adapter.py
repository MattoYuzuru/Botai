"""Recipe adapter against the synthetic fakeuni (JSON and HTML) and against crafted transports."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from botai.contracts.schemas import errors
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapter import AdapterResult, ExpiredRules, SessionExpired
from botai.sources.factory import AdapterFactory
from tests.fakeuni import api as fakeuni_api
from tests.sources import recipes
from tests.sources.helpers import NOW, make_ctx, session_state


async def run(spec: dict[str, Any], ctx: Any) -> AdapterResult:
    return await AdapterFactory().create(spec).fetch(ctx)


def by_kind(result: AdapterResult, kind: str) -> list[Any]:
    return sorted((r for r in result.records if r.kind == kind), key=lambda r: r.external_id)


def test_the_fixture_recipes_satisfy_the_contract_schema() -> None:
    assert errors("adapter-spec.schema.json", recipes.full_recipe()) == []
    assert (
        errors("adapter-spec.schema.json", recipes.recipe_spec([recipes.deadlines_html_endpoint()]))
        == []
    )


async def test_json_recipe_yields_schedule_and_deadlines(fakeuni_app: object) -> None:
    result = await run(recipes.recipe_spec(), make_ctx(fakeuni_app))
    events = by_kind(result, "scheduleEvents")
    assert [e.external_id for e in events] == ["ev-1", "ev-2", "ev-3", "ev-4"]
    first = events[0]
    assert first.fields == {
        "title": "Математический анализ",
        "kind": "lecture",
        "startsAt": "2026-10-26T06:30:00+00:00",
        "endsAt": "2026-10-26T07:50:00+00:00",
        "location": "А-101",
    }
    assert first.source_url.startswith("https://fakeuni.test/api/v2/timetable?from=2026-10-27")
    assert "to=2026-11-03" in first.source_url
    deadlines = by_kind(result, "deadlines")
    assert [d.external_id for d in deadlines] == ["t-1", "t-2"]
    assert deadlines[0].fields["dueAt"] == "2026-10-28T20:00:00+00:00"
    assert result.spec_revision == 1
    assert all(o.ok for o in result.outcomes)
    assert result.complete_kinds() == {"scheduleEvents", "deadlines"}


async def test_html_recipe_reads_news_and_ids_are_stable(fakeuni_app: object) -> None:
    spec = recipes.recipe_spec([recipes.news_endpoint()], require=("news",))
    first = await run(spec, make_ctx(fakeuni_app, logged_in=False))
    second = await run(spec, make_ctx(fakeuni_app, logged_in=False))
    news = by_kind(first, "news")
    assert sorted(n.fields["title"] for n in news) == [
        "Запись на пересдачу открыта",
        "Перенос занятий 4 ноября",
    ]
    assert {n.fields["url"] for n in news} == {"/news/1", "/news/2"}
    assert {n.fields["publishedAt"] for n in news} == {
        "2026-10-26T21:00:00+00:00",
        "2026-10-24T21:00:00+00:00",
    }
    # no id in the markup: the id is a hash of the `identity` fields, identical on every run
    assert [n.external_id for n in news] == [n.external_id for n in by_kind(second, "news")]
    assert all(len(n.external_id) == 64 for n in news)


async def test_html_table_behind_login(fakeuni_app: object) -> None:
    spec = recipes.recipe_spec([recipes.deadlines_html_endpoint()], require=("deadlines",))
    result = await run(spec, make_ctx(fakeuni_app))
    assert [d.external_id for d in by_kind(result, "deadlines")] == ["t-1", "t-2"]
    assert by_kind(result, "deadlines")[1].fields["title"] == "Лабораторная 2: индексы"


async def test_dead_session_on_json_endpoint_is_session_expired(fakeuni_app: object) -> None:
    ctx = make_ctx(fakeuni_app)
    fakeuni_api.expire_sessions()
    with pytest.raises(SessionExpired):
        await run(recipes.recipe_spec(), ctx)


async def test_redirect_to_login_page_is_session_expired_by_preset(fakeuni_app: object) -> None:
    spec = recipes.recipe_spec([recipes.deadlines_html_endpoint()], require=("deadlines",))
    ctx = make_ctx(fakeuni_app, expired=ExpiredRules(redirect_path_prefixes=("/auth/",)))
    fakeuni_api.expire_sessions()
    with pytest.raises(SessionExpired) as exc:
        await run(spec, ctx)
    assert "login" in exc.value.reason


async def test_login_form_where_data_was_expected_is_session_expired_without_preset(
    fakeuni_app: object,
) -> None:
    spec = recipes.recipe_spec([recipes.deadlines_html_endpoint()], require=("deadlines",))
    ctx = make_ctx(fakeuni_app)
    fakeuni_api.expire_sessions()
    with pytest.raises(SessionExpired):
        await run(spec, ctx)


async def test_session_past_its_expiry_hint_sends_no_request(fakeuni_app: object) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    ctx = make_ctx(
        session=session_state(expires_hint=NOW - timedelta(minutes=1)),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SessionExpired):
        await run(recipes.recipe_spec(), ctx)
    assert calls == []  # T13: a known-dead session costs zero requests


async def test_moved_endpoint_is_a_structural_problem_for_that_kind_only(
    fakeuni_app: object,
) -> None:
    fakeuni_api.STATE.timetable_version = "v3"
    result = await run(recipes.recipe_spec(), make_ctx(fakeuni_app))
    problems = {o.endpoint_id: o for o in result.outcomes}
    assert problems["timetable"].problem == "not_found"
    assert problems["timetable"].structural
    assert problems["deadlines"].ok
    assert result.complete_kinds() == {"deadlines"}
    assert result.count("deadlines") == 2
    assert result.count("scheduleEvents") == 0


async def test_changed_markup_yields_empty_not_an_exception(fakeuni_app: object) -> None:
    fakeuni_api.STATE.news_markup = "v2"
    spec = recipes.recipe_spec([recipes.news_endpoint()], require=("news",))
    result = await run(spec, make_ctx(fakeuni_app, logged_in=False))
    assert result.records == []
    assert result.outcomes[0].problem == "empty"  # the Runner decides if that is suspicious


async def test_html_answer_where_json_is_expected_is_format_problem(fakeuni_app: object) -> None:
    endpoint = recipes.deadlines_endpoint()
    endpoint["request"]["path"] = "/news"
    result = await run(
        recipes.recipe_spec([endpoint], require=("deadlines",)), make_ctx(fakeuni_app)
    )
    assert result.outcomes[0].problem == "format"


async def test_items_without_required_fields_are_a_fields_problem(fakeuni_app: object) -> None:
    endpoint = recipes.deadlines_endpoint()
    endpoint["response"]["fields"]["dueAt"]["path"] = "/doesNotExist"
    result = await run(
        recipes.recipe_spec([endpoint], require=("deadlines",)), make_ctx(fakeuni_app)
    )
    assert result.outcomes[0].problem == "fields"
    assert result.outcomes[0].items_seen == 2
    assert result.records == []


# --- pagination -----------------------------------------------------------------------------


def courses_endpoint(pagination: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": "/api/v2/courses"},
        "pagination": pagination,
        "response": {
            "format": "json",
            "itemsPath": "/results",
            "fields": {"externalId": {"path": "/id"}, "title": {"path": "/name", "required": True}},
        },
    }


async def test_page_pagination_walks_until_total(fakeuni_app: object) -> None:
    pagination = {
        "type": "page",
        "param": "page",
        "limitParam": "size",
        "limit": 2,
        "maxPages": 10,
        "totalPath": "/total",
    }
    spec = recipes.recipe_spec([courses_endpoint(pagination)], require=("courses",))
    result = await run(spec, make_ctx(fakeuni_app))
    assert [c.external_id for c in by_kind(result, "courses")] == [f"c-{i}" for i in range(1, 6)]


async def test_max_pages_bounds_the_walk(fakeuni_app: object) -> None:
    pagination = {"type": "page", "param": "page", "limitParam": "size", "limit": 2, "maxPages": 2}
    spec = recipes.recipe_spec([courses_endpoint(pagination)], require=("courses",))
    result = await run(spec, make_ctx(fakeuni_app))
    assert len(by_kind(result, "courses")) == 4


async def test_cursor_pagination_follows_the_cursor_path(fakeuni_app: object) -> None:
    endpoint = {
        "id": "feed",
        "kind": "news",
        "request": {"method": "GET", "path": "/api/v2/feed"},
        "pagination": {"type": "cursor", "param": "cursor", "cursorPath": "/next", "maxPages": 5},
        "response": {
            "format": "json",
            "itemsPath": "/items",
            "fields": {
                "externalId": {"path": "/id"},
                "title": {"path": "/title"},
                "publishedAt": {"path": "/id", "transform": "iso"},
            },
        },
    }
    # publishedAt is not parseable here; the point is that all three pages are read
    spec = recipes.recipe_spec([endpoint], require=("news",))
    result = await run(spec, make_ctx(fakeuni_app))
    assert result.outcomes[0].items_seen == 3


async def test_offset_pagination_and_the_loop_guard() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        offset = int(request.url.params.get("offset", "0"))
        rows = [{"id": i, "t": f"T{i}"} for i in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json=rows)

    endpoint = {
        "id": "rows",
        "kind": "courses",
        "request": {"method": "GET", "path": "/rows"},
        "pagination": {"type": "offset", "param": "offset", "limitParam": "n", "limit": 2},
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {"externalId": {"path": "/id"}, "title": {"path": "/t"}},
        },
    }
    spec = recipes.recipe_spec([endpoint], require=("courses",))
    result = await run(spec, make_ctx(transport=httpx.MockTransport(handler)))
    assert len(result.records) == 5
    assert [p["offset"] for p in seen] == ["0", "2", "4"]
    assert all(p["n"] == "2" for p in seen)

    # a server that ignores the paging parameter must not trap us in a loop
    pages = {"n": 0}

    def stubborn(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        return httpx.Response(200, json=[{"id": 1, "t": "A"}, {"id": 2, "t": "B"}])

    result = await run(spec, make_ctx(transport=httpx.MockTransport(stubborn)))
    assert pages["n"] == 2
    assert len(result.records) == 2


# --- bootstrap, safety ----------------------------------------------------------------------


def bootstrap_recipe() -> dict[str, Any]:
    spec = recipes.recipe_spec(
        [
            {
                "id": "mine",
                "kind": "courses",
                "request": {"method": "GET", "path": "/api/users/{accountId}/courses"},
                "response": {
                    "format": "json",
                    "itemsPath": "/items",
                    "fields": {"externalId": {"path": "/id"}, "title": {"path": "/t"}},
                },
            }
        ],
        require=("courses",),
    )
    spec["recipe"]["bootstrap"] = [
        {"id": "account", "method": "GET", "path": "/api/me", "extract": {"accountId": "/sub"}}
    ]
    return spec


async def test_bootstrap_value_is_encoded_into_one_path_segment() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"sub": "../../admin?x=1"})
        return httpx.Response(200, json={"items": [{"id": "1", "t": "Курс"}]})

    result = await run(bootstrap_recipe(), make_ctx(transport=httpx.MockTransport(handler)))
    assert paths[1] == "/api/users/..%2F..%2Fadmin%3Fx%3D1/courses"
    assert len(result.records) == 1


async def test_failed_bootstrap_marks_dependent_endpoints() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(500) if request.url.path == "/api/me" else httpx.Response(200, json={})
        )

    ctx = make_ctx(transport=httpx.MockTransport(handler))
    with pytest.raises(BotaiError) as exc:
        await run(bootstrap_recipe(), ctx)
    assert exc.value.code == ErrorCode.SOURCE_UNAVAILABLE

    def handler2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nosub": 1})

    result = await run(bootstrap_recipe(), make_ctx(transport=httpx.MockTransport(handler2)))
    assert result.outcomes[0].problem == "bootstrap"


async def test_post_is_never_sent_unless_read_only_by_construction() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"items": []})

    endpoint = recipes.deadlines_endpoint()
    endpoint["request"] = {"method": "POST", "readOnlyPost": True, "path": "/api/delete-all"}
    result = await run(
        recipes.recipe_spec([endpoint], require=("deadlines",)),
        make_ctx(transport=httpx.MockTransport(handler)),
    )
    assert calls == []
    assert result.outcomes[0].problem == "status"


async def test_recipe_for_a_host_outside_the_connection_is_blocked(fakeuni_app: object) -> None:
    ctx = make_ctx(fakeuni_app, allow_hosts=("other.test",))
    with pytest.raises(BotaiError) as exc:
        await run(recipes.recipe_spec(), ctx)
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED


async def test_redirect_to_a_foreign_host_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/collect"})

    with pytest.raises(BotaiError) as exc:
        await run(recipes.recipe_spec(), make_ctx(transport=httpx.MockTransport(handler)))
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED


async def test_source_down_raises_unavailable_and_secrets_stay_out_of_results(
    fakeuni_app: object,
) -> None:
    ctx = make_ctx(fakeuni_app)
    fakeuni_api.STATE.fail_status = 503
    with pytest.raises(BotaiError) as exc:
        await run(recipes.recipe_spec(), ctx)
    assert exc.value.code == ErrorCode.SOURCE_UNAVAILABLE
    fakeuni_api.STATE.fail_status = None
    result = await run(recipes.recipe_spec(), ctx)
    dump = repr(result)
    assert "tok-fixture-session" not in dump


async def test_one_sync_cannot_become_a_flood() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[{"id": f"{request.url.path}-{page}", "t": "x"}])

    def endpoint(i: int) -> dict[str, Any]:
        return {
            "id": f"rows{i}",
            "kind": "courses",
            "request": {"method": "GET", "path": f"/rows{i}"},
            "pagination": {"type": "page", "param": "page", "maxPages": 20},
            "response": {
                "format": "json",
                "itemsPath": "",
                "fields": {"externalId": {"path": "/id"}, "title": {"path": "/t"}},
            },
        }

    spec = recipes.recipe_spec([endpoint(i) for i in range(10)], require=("courses",))
    with pytest.raises(BotaiError) as exc:
        await run(spec, make_ctx(transport=httpx.MockTransport(handler)))
    assert exc.value.code == ErrorCode.SOURCE_RATE_LIMITED
    assert calls["n"] == 120  # the budget, not 200


async def test_absurdly_nested_json_is_a_format_problem_not_a_crash() -> None:
    body = "[" * 200_000 + "]" * 200_000
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    result = await run(
        recipes.recipe_spec([recipes.deadlines_endpoint()], require=("deadlines",)),
        make_ctx(transport=transport),
    )
    assert result.outcomes[0].problem == "format"


async def test_one_endpoint_cannot_flood_the_database_with_items() -> None:
    rows = [{"id": f"r{i}", "t": "x"} for i in range(6000)]
    endpoint = {
        "id": "rows",
        "kind": "courses",
        "request": {"method": "GET", "path": "/rows"},
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {"externalId": {"path": "/id"}, "title": {"path": "/t"}},
        },
    }
    result = await run(
        recipes.recipe_spec([endpoint], require=("courses",)),
        make_ctx(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows))),
    )
    assert len(result.records) == 5000
    assert result.outcomes[0].items_seen == 6000
    assert result.outcomes[0].ok is False
    assert result.outcomes[0].problem == "limit"
    assert result.complete_kinds() == set()
