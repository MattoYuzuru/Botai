"""Portable browser descriptors obey the existing response-selector lint policy."""

from __future__ import annotations

from typing import Any

import pytest

from botai.learn.lint import LintContext, lint_spec
from tests.sources.recipes import deadlines_html_endpoint, recipe_spec


def browser_recipe() -> dict[str, Any]:
    endpoint = deadlines_html_endpoint()
    endpoint["request"].update(
        render=True,
        browserPlan={
            "version": 1,
            "steps": [
                {
                    "op": "wait_ready",
                    "target": {"selector": "main", "tag": "main"},
                    "region": {"selector": "section", "tag": "section"},
                    "framePath": [{"selector": "iframe"}],
                }
            ],
            "framePath": [{"selector": "iframe"}],
            "emptyTarget": {"selector": ".empty", "tag": "p", "role": "status"},
            "pagination": {
                "next": {
                    "op": "next_link",
                    "target": {"selector": "a", "tag": "a"},
                    "region": {"selector": "section", "tag": "section"},
                    "framePath": [{"selector": "iframe"}],
                },
                "maxPages": 3,
            },
        },
    )
    spec = recipe_spec([endpoint], require=("deadlines",))
    spec["recipe"]["version"] = 2
    return spec


def test_bounded_browser_descriptors_pass_existing_selector_policy() -> None:
    result = lint_spec(browser_recipe())
    assert result.ok, result.reasons()


@pytest.mark.parametrize("selector", ["article:has(input)", "p::before"])
@pytest.mark.parametrize(
    "path",
    [
        ("response", "itemsSelector"),
        ("request", "browserPlan", "steps", 0, "target", "selector"),
        ("request", "browserPlan", "steps", 0, "region", "selector"),
        ("request", "browserPlan", "steps", 0, "framePath", 0, "selector"),
        ("request", "browserPlan", "framePath", 0, "selector"),
        ("request", "browserPlan", "emptyTarget", "selector"),
        ("request", "browserPlan", "pagination", "next", "target", "selector"),
        ("request", "browserPlan", "pagination", "next", "region", "selector"),
        ("request", "browserPlan", "pagination", "next", "framePath", 0, "selector"),
    ],
)
def test_browser_plan_cannot_bypass_response_selector_policy(
    path: tuple[str | int, ...], selector: str
) -> None:
    spec = browser_recipe()
    node = spec["recipe"]["endpoints"][0]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = selector
    result = lint_spec(spec)
    where = "/recipe/endpoints/0/" + "/".join(map(str, path))
    assert any(issue.code == "html.selector" and issue.path == where for issue in result.issues)
    assert "schema" not in result.codes()


@pytest.mark.parametrize(
    "path",
    [
        ("steps", 0, "target", "selector"),
        ("steps", 0, "target", "label"),
        ("steps", 0, "target", "controls"),
        ("steps", 0, "region", "label"),
        ("steps", 0, "framePath", 0, "name"),
        ("framePath", 0, "title"),
        ("emptyTarget", "label"),
        ("pagination", "next", "target", "label"),
        ("steps", 0, "value"),
    ],
)
def test_browser_plan_does_not_share_observed_student_literals(path: tuple[str | int, ...]) -> None:
    spec = browser_recipe()
    plan = spec["recipe"]["endpoints"][0]["request"]["browserPlan"]
    if path[-1] == "value":
        plan["steps"][0].update(op="select_option")
        plan["steps"][0]["target"]["tag"] = "select"
    node = plan
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = "StudentA"
    result = lint_spec(spec, LintContext(observed=("StudentA",)))
    where = "/recipe/endpoints/0/request/browserPlan/" + "/".join(map(str, path))
    assert any(issue.code == "observed.value" and issue.path == where for issue in result.issues)
    assert "schema" not in result.codes()
