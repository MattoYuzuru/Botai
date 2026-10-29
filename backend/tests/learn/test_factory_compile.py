"""Factory candidates reference observations; executable descriptors remain server-owned."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from botai.contracts.schemas import errors
from botai.core.browser import ObservedRequest
from botai.core.llm import LlmResponse, ScriptedLlm
from botai.learn.author import author_kind, build_spec
from botai.learn.compiler import CompiledCandidate, FactoryCompileError, compile_candidate
from botai.learn.lint import LintContext
from botai.learn.prompting import author_request, author_schema
from tests.sources import recipes
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"


class TrustedPlan(BaseModel):
    version: Literal[1] = 1
    steps: list[Any] = Field(default_factory=list)
    frame_path: list[Any] = Field(default_factory=list, alias="framePath")


def observed(path: str, **changes: Any) -> ObservedRequest:
    return ObservedRequest.model_validate(
        {
            "url": ORIGIN + path,
            "method": "GET",
            "status": 200,
            "resource_type": "fetch",
            "auth_proven": True,
            "has_cookie": True,
            "has_authorization": True,
            "authorization_scheme": "bearer",
            **changes,
        }
    )


def http_endpoint(ref: str = "rDeadlines") -> dict[str, Any]:
    return {**recipes.deadlines_endpoint(), "requestEvidenceId": ref}


def compile_source(
    endpoint: dict[str, Any], bootstrap: list[dict[str, Any]] | None = None, **maps: Any
) -> CompiledCandidate:
    return compile_candidate(
        endpoint,
        bootstrap or [],
        origin=ORIGIN,
        request_facts=maps.get(
            "request_facts", {"rDeadlines": observed("/api/v2/tasks/deadlines")}
        ),
        browser_plans=maps.get("browser_plans", {}),
        browser_plan_urls=maps.get("browser_plan_urls", {}),
        now=maps.get("now"),
        time_zone=maps.get("time_zone"),
    )


def as_spec(compiled: CompiledCandidate) -> dict[str, Any]:
    return build_spec(
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="a" * 64,
        endpoints=[compiled.endpoint],
        bootstrap=compiled.bootstrap,
        auth=compiled.auth,
        require_kinds=[compiled.endpoint["kind"]],
    )


def test_compiler_binds_endpoint_and_bootstrap_independently_without_saving_references() -> None:
    endpoint = http_endpoint()
    original = copy.deepcopy(endpoint)
    compiled = compile_source(
        endpoint,
        [
            {
                "id": "profile",
                "method": "GET",
                "path": "/api/v2/me",
                "extract": {"studentId": "/id"},
                "requestEvidenceId": "rProfile",
            }
        ],
        request_facts={
            "rDeadlines": observed("/api/v2/tasks/deadlines"),
            "rProfile": observed(
                "/api/v2/me", has_authorization=False, authorization_scheme="none"
            ),
        },
    )
    assert compiled.endpoint["request"]["authBinding"] == "bCookiesBearer"
    assert compiled.bootstrap[0]["authBinding"] == "bCookies"
    assert compiled.auth == {
        "type": "session",
        "version": 1,
        "bindings": {
            "bCookiesBearer": {"cookies": True, "bearer": "observedAuthorization"},
            "bCookies": {"cookies": True, "bearer": "none"},
        },
    }
    assert as_spec(compiled)["recipe"]["version"] == 2
    assert "requestEvidenceId" not in json.dumps(as_spec(compiled))
    assert endpoint == original


@pytest.mark.parametrize("field", ["authBinding", "browserPlan"])
def test_model_cannot_supply_executor_owned_request_fields(field: str) -> None:
    endpoint = http_endpoint()
    endpoint["request"][field] = "invented-private-fixture"
    with pytest.raises(FactoryCompileError) as error:
        compile_source(endpoint)
    assert error.value.code == "factory_execution_fields_forbidden"
    assert "invented-private-fixture" not in str(error.value)


def test_every_bootstrap_requires_its_own_observed_read() -> None:
    with pytest.raises(FactoryCompileError, match="factory_evidence_required"):
        compile_source(
            http_endpoint(),
            [
                {
                    "id": "profile",
                    "method": "GET",
                    "path": "/api/v2/me",
                    "extract": {"studentId": "/id"},
                }
            ],
        )


def test_rendered_plan_restores_only_trusted_entry_and_descriptor() -> None:
    endpoint = recipes.news_endpoint()
    endpoint["browserPlanRef"] = "pNews"
    endpoint["request"].update(path="/invented", query={"invented": "private-fixture"})
    plan = TrustedPlan()
    compiled = compile_source(
        endpoint,
        browser_plans={"pNews": plan},
        browser_plan_urls={"pNews": ORIGIN + "/news?week=2026-10-26"},
    )
    assert compiled.endpoint["request"] == {
        "method": "GET",
        "path": "/news",
        "query": {"week": "2026-10-26"},
        "render": True,
        "browserPlan": plan.model_dump(by_alias=True, exclude_none=True),
    }
    assert "browserPlanRef" not in json.dumps(compiled.endpoint)
    assert "invented" not in json.dumps(compiled.endpoint)
    assert as_spec(compiled)["recipe"]["version"] == 2


def test_browser_entry_allows_only_builtins_that_render_to_the_certified_url() -> None:
    endpoint = recipes.news_endpoint()
    endpoint["browserPlanRef"] = "pNews"
    endpoint["request"].update(path="/news/{weekStart}", query={"week": "{weekStart}"})
    compiled = compile_source(
        endpoint,
        browser_plans={"pNews": TrustedPlan()},
        browser_plan_urls={"pNews": ORIGIN + "/news/2026-10-26?week=2026-10-26"},
        now=NOW,
        time_zone=ZoneInfo("Europe/Moscow"),
    )
    assert compiled.endpoint["request"]["path"] == "/news/{weekStart}"
    assert compiled.endpoint["request"]["query"] == {"week": "{weekStart}"}


@pytest.mark.parametrize("template", ["{today}", "{studentId}", "{weekStart}{madeUp}"])
def test_browser_entry_never_executes_mismatching_or_unbound_templates(template: str) -> None:
    endpoint = recipes.news_endpoint()
    endpoint["browserPlanRef"] = "pNews"
    endpoint["request"]["query"] = {"week": template}
    with pytest.raises(FactoryCompileError, match="browser_entry_template_"):
        compile_source(
            endpoint,
            browser_plans={"pNews": TrustedPlan()},
            browser_plan_urls={"pNews": ORIGIN + "/news?week=2026-10-26"},
            now=NOW,
            time_zone=ZoneInfo("Europe/Moscow"),
        )


def test_browser_entry_keeps_certified_literal_if_no_clock_was_supplied() -> None:
    endpoint = recipes.news_endpoint()
    endpoint["browserPlanRef"] = "pNews"
    endpoint["request"]["query"] = {"week": "{studentId}"}
    compiled = compile_source(
        endpoint,
        browser_plans={"pNews": TrustedPlan()},
        browser_plan_urls={"pNews": ORIGIN + "/news?week=2026-10-26"},
    )
    assert compiled.endpoint["request"]["query"] == {"week": "2026-10-26"}


@pytest.mark.parametrize("problem", ["unknown", "cross_origin", "post", "json", "body"])
def test_unproven_or_mutating_browser_replay_is_rejected(problem: str) -> None:
    endpoint = recipes.news_endpoint()
    endpoint["browserPlanRef"] = "pNews" if problem != "unknown" else "invented"
    if problem == "post":
        endpoint["request"]["method"] = "POST"
    elif problem == "json":
        endpoint["response"]["format"] = "json"
    elif problem == "body":
        endpoint["request"]["body"] = {}
    entry = ("https://id.fakeuni.test" if problem == "cross_origin" else ORIGIN) + "/news"
    with pytest.raises(FactoryCompileError):
        compile_source(
            endpoint, browser_plans={"pNews": TrustedPlan()}, browser_plan_urls={"pNews": entry}
        )


def test_plain_render_requires_a_real_document_and_does_not_upgrade_a_legacy_recipe_by_flag() -> (
    None
):
    endpoint = recipes.news_endpoint()
    endpoint["request"]["render"] = True
    endpoint["requestEvidenceId"] = "rNews"
    with pytest.raises(FactoryCompileError, match="browser_page_unobserved"):
        compile_source(endpoint, request_facts={"rNews": observed("/news")})
    compiled = compile_source(
        endpoint, request_facts={"rNews": observed("/news", resource_type="document")}
    )
    assert "authBinding" not in compiled.endpoint["request"]
    assert as_spec(compiled)["recipe"]["version"] == 1


@pytest.mark.parametrize("feature", ["collections", "expand", "compose", "from"])
def test_data_expressions_select_recipe_v2_without_affecting_plain_legacy(feature: str) -> None:
    endpoint = recipes.deadlines_endpoint()
    kwargs: dict[str, Any] = dict(
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="a" * 64,
        endpoints=[endpoint],
        bootstrap=[],
        auth={"type": "cookie"},
        require_kinds=["deadlines"],
    )
    assert build_spec(**kwargs)["recipe"]["version"] == 1
    response = endpoint["response"]
    if feature == "collections":
        response.pop("itemsPath")
        response["collections"] = [{"as": "task", "path": "/tasks"}]
    elif feature == "expand":
        endpoint["kind"] = "scheduleEvents"
        kwargs["require_kinds"] = ["scheduleEvents"]
        response["fields"] = {"externalId": {"path": "/id"}, "title": {"path": "/title"}}
        response["expand"] = {
            "type": "weekly",
            "timeZone": "Europe/Moscow",
            "startDate": {"path": "/from"},
            "untilDate": {"path": "/until"},
            "weekdays": {"path": "/days"},
            "startTime": {"path": "/start"},
            "endTime": {"path": "/end"},
            "horizon": {"from": "{weekStart}", "days": 21},
        }
    elif feature == "compose":
        response["fields"]["dueAt"] = {
            "compose": {
                "type": "dateTime",
                "date": {"path": "/date"},
                "time": {"path": "/time"},
                "timeZone": "Europe/Moscow",
            }
        }
    else:
        response["fields"]["dueAt"]["from"] = "root"
    spec = build_spec(**kwargs)
    assert spec["recipe"]["version"] == 2
    assert errors("adapter-spec.schema.json", spec) == []


def test_author_schemas_offer_only_available_refs_and_exclude_unreachable_descriptors() -> None:
    legacy = author_schema("deadlines")
    Draft202012Validator.check_schema(legacy)
    assert not any(name.startswith("v2") or name.endswith("V2") for name in legacy["$defs"])
    schema = author_schema(
        "deadlines",
        factory_v2=True,
        request_facts={"rDeadlines": observed("/api/v2/tasks/deadlines")},
        browser_plans={"pNews": TrustedPlan()},
        browser_plan_urls={"pNews": ORIGIN + "/news"},
    )
    Draft202012Validator.check_schema(schema)
    endpoint = schema["$defs"]["endpointV2"]
    assert endpoint["properties"]["requestEvidenceId"]["enum"] == ["rDeadlines"]
    assert endpoint["properties"]["browserPlanRef"]["enum"] == ["pNews"]
    assert "authBinding" not in endpoint["properties"]["request"]["properties"]
    assert "browserPlan" not in endpoint["properties"]["request"]["properties"]
    assert "v2BrowserPlan" not in schema["$defs"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"found": True, "endpoints": [http_endpoint()]})
    assert not validator.is_valid({"found": True, "endpoints": [http_endpoint("invented")]})


async def test_author_compiles_before_lint_and_retries_static_failures_without_raw_auth() -> None:
    marker = "invented-private-fixture"
    responses = [
        {"found": True, "endpoints": [http_endpoint(marker)]},
        {
            "found": True,
            "endpoints": [http_endpoint()],
            "auth": {"type": "bearer", "headerName": marker},
        },
    ]
    llm = ScriptedLlm(queue=[LlmResponse(content=json.dumps(value)) for value in responses])
    result = await author_kind(
        llm,
        "deadlines",
        "<observation>rDeadlines: observed synthetic GET</observation>",
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="a" * 64,
        lint_ctx=LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN),
        factory_v2=True,
        request_facts={"rDeadlines": observed("/api/v2/tasks/deadlines")},
    )
    assert len(llm.requests) == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].auth["type"] == "session"
    assert marker not in json.dumps(result.candidates[0].auth)
    assert "evidence_unknown" in llm.requests[1].messages[-1].content
    assert marker not in llm.requests[1].messages[-1].content


def test_legacy_author_request_remains_available_without_factory_references() -> None:
    request = author_request("deadlines", "fixture")
    assert request.json_schema is not None
    assert "requestEvidenceId" not in json.dumps(request.json_schema)
    assert "Проверенные ссылки фабрики" not in request.messages[-1].content
