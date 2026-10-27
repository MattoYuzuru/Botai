"""Recipes for the synthetic fakeuni, written the way the Explorer would write them."""

from __future__ import annotations

import copy
from typing import Any

from tests.fakeuni import data


def timetable_endpoint(path: str = "/api/v2/timetable") -> dict[str, Any]:
    return {
        "id": "timetable",
        "kind": "scheduleEvents",
        "request": {
            "method": "GET",
            "path": path,
            "query": {"from": "{today}", "to": "{plusDays:7}"},
        },
        "response": {
            "format": "json",
            "itemsPath": "/items",
            "fields": {
                "externalId": {"path": "/uid"},
                "title": {"path": "/subject/name", "required": True},
                "kind": {"path": "/type"},
                "startsAt": {"path": "/slot/begin", "transform": "iso", "required": True},
                "endsAt": {"path": "/slot/end", "transform": "iso"},
                "location": {"path": "/place/room"},
            },
        },
    }


def deadlines_endpoint() -> dict[str, Any]:
    return {
        "id": "deadlines",
        "kind": "deadlines",
        "request": {"method": "GET", "path": "/api/v2/tasks/deadlines"},
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {
                "externalId": {"path": "/taskId"},
                "title": {"path": "/name", "required": True},
                "dueAt": {"path": "/due", "transform": "unixSeconds", "required": True},
                "state": {"path": "/status"},
            },
        },
    }


def news_endpoint(selector: str = "li.news-item") -> dict[str, Any]:
    return {
        "id": "news",
        "kind": "news",
        "request": {"method": "GET", "path": "/news"},
        "response": {
            "format": "html",
            "itemsSelector": selector,
            "fields": {
                "title": {"selector": "a", "required": True},
                "url": {"selector": "a", "attr": "href"},
                "publishedAt": {
                    "selector": "time",
                    "attr": "datetime",
                    "transform": "iso",
                    "required": True,
                },
            },
            "identity": ["url"],
        },
    }


def deadlines_html_endpoint() -> dict[str, Any]:
    return {
        "id": "deadlinesPage",
        "kind": "deadlines",
        "request": {"method": "GET", "path": "/portal/deadlines"},
        "response": {
            "format": "html",
            "itemsSelector": "table#tasks tbody tr",
            "fields": {
                "externalId": {"selector": ".", "attr": "data-task"},
                "title": {"selector": "td.name", "required": True},
                "dueAt": {
                    "selector": "td.due",
                    "attr": "data-ts",
                    "transform": "unixSeconds",
                    "required": True,
                },
            },
        },
    }


def recipe_spec(
    endpoints: list[dict[str, Any]] | None = None,
    *,
    revision: int = 1,
    require: tuple[str, ...] = ("scheduleEvents", "deadlines"),
    min_items: int = 1,
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "adapter": "recipe",
        "recipe": {
            "version": 1,
            "origin": data.ORIGIN,
            "revision": revision,
            "learnedAt": "2026-10-27T10:00:00Z",
            "auth": auth or {"type": "cookie"},
            "endpoints": copy.deepcopy(endpoints or [timetable_endpoint(), deadlines_endpoint()]),
            "validation": {"requireKinds": list(require), "minItems": min_items},
            "freshness": {"syncEveryMinutes": 180, "staleAfterMinutes": 720},
        },
    }


def full_recipe(revision: int = 1) -> dict[str, Any]:
    return recipe_spec(
        [timetable_endpoint(), deadlines_endpoint(), news_endpoint()],
        revision=revision,
    )
