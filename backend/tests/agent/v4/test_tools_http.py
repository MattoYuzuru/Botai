"""`http_request` on an httpx MockTransport (L0): methods, bodies, redirects, errors, saving."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.http import HttpRequestTool


def context(workspace: Path) -> ToolContext:
    return ToolContext(run_id="run-1", workspace=workspace)


async def call_tool(
    arguments: dict[str, Any],
    *,
    workspace: Path,
    handlers: list[Any] | None = None,
) -> tuple[Any, list[httpx.Request]]:
    wire_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire_requests.append(request)
        if handlers:
            index = min(len(wire_requests) - 1, len(handlers) - 1)
            return handlers[index](request)
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    tool = HttpRequestTool(transport=httpx.MockTransport(handler))
    result = await tool.run(arguments, context(workspace))
    return result, wire_requests


async def test_get_returns_structured_response(tmp_path: Path) -> None:
    result, requests = await call_tool(
        {"method": "GET", "url": "https://example.test/api?x=1"},
        workspace=tmp_path,
    )
    assert result.ok
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://example.test/api?x=1"
    assert request.headers["user-agent"].startswith("BotAI/")
    assert result.data["status"] == 200
    assert result.data["url"] == "https://example.test/api?x=1"
    assert result.data["json"] == {"ok": True}
    assert result.data["redirects"] == []
    assert result.data["content_type"] == "application/json"
    assert result.data["bytes"] > 0
    assert result.data["body_truncated"] is False
    assert "200" in result.summary


async def test_post_json_body_and_custom_headers(tmp_path: Path) -> None:
    received: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = json.loads(request.content)
        received["content_type"] = request.headers.get("content-type")
        received["x_token"] = request.headers.get("x-token")
        return httpx.Response(201, json={"created": True})

    result, _ = await call_tool(
        {
            "method": "POST",
            "url": "https://example.test/items",
            "headers": {"x-token": "fixture"},
            "body": {"name": "ВШЭ"},
            "body_kind": "json",
        },
        workspace=tmp_path,
        handlers=[handler],
    )
    assert result.data["status"] == 201
    assert received["body"] == {"name": "ВШЭ"}
    assert received["content_type"] == "application/json"
    assert received["x_token"] == "fixture"


async def test_form_body_is_urlencoded(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content.decode()
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, text="ok")

    result, _ = await call_tool(
        {
            "method": "POST",
            "url": "https://example.test/form",
            "body": {"login": "student", "remember": "1"},
            "body_kind": "form",
        },
        workspace=tmp_path,
        handlers=[handler],
    )
    assert result.ok
    assert "login=student" in captured["content"]
    assert "application/x-www-form-urlencoded" in captured["content_type"]


async def test_raw_body_is_sent_as_is(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content.decode()
        return httpx.Response(200, text="ok")

    await call_tool(
        {
            "method": "PUT",
            "url": "https://example.test/raw",
            "body": "a=1&b=2",
            "body_kind": "raw",
        },
        workspace=tmp_path,
        handlers=[handler],
    )
    assert captured["content"] == "a=1&b=2"


async def test_cookies_are_sent(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, text="ok")

    await call_tool(
        {"method": "GET", "url": "https://example.test/me", "cookies": {"session": "abc123"}},
        workspace=tmp_path,
        handlers=[handler],
    )
    assert "session=abc123" in captured["cookie"]


async def test_redirect_chain_is_reported(tmp_path: Path) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.test/final"})

    def final(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="done", headers={"content-type": "text/plain"})

    result, requests = await call_tool(
        {"method": "GET", "url": "https://example.test/start"},
        workspace=tmp_path,
        handlers=[redirect, final],
    )
    assert len(requests) == 2
    assert result.data["status"] == 200
    assert result.data["url"] == "https://example.test/final"
    assert result.data["redirects"] == ["https://example.test/start"]
    assert result.data["text"] == "done"


async def test_redirects_can_be_disabled(tmp_path: Path) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.test/final"})

    result, requests = await call_tool(
        {"method": "GET", "url": "https://example.test/start", "follow_redirects": False},
        workspace=tmp_path,
        handlers=[redirect],
    )
    assert len(requests) == 1
    assert result.data["status"] == 302
    assert result.data["redirects"] == []


async def test_http_error_status_is_data_not_a_tool_error(tmp_path: Path) -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="нет такой страницы")

    result, _ = await call_tool(
        {"method": "GET", "url": "https://example.test/missing"},
        workspace=tmp_path,
        handlers=[not_found],
    )
    assert result.ok
    assert result.data["status"] == 404
    assert result.data["text"] == "нет такой страницы"


async def test_transport_timeout_is_a_retryable_error(tmp_path: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result, _ = await call_tool(
        {"method": "GET", "url": "https://example.test/slow", "timeout_s": 0.5},
        workspace=tmp_path,
        handlers=[timeout],
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True


async def test_network_error_is_a_retryable_error(tmp_path: Path) -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    result, _ = await call_tool(
        {"method": "GET", "url": "https://unreachable.test/"},
        workspace=tmp_path,
        handlers=[disconnected],
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "network_error"
    assert result.error.retryable is True


async def test_big_body_is_capped_and_marked_truncated(tmp_path: Path) -> None:
    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5_000, headers={"content-type": "text/plain"})

    result, _ = await call_tool(
        {"method": "GET", "url": "https://example.test/big", "max_bytes": 1024},
        workspace=tmp_path,
        handlers=[big],
    )
    assert result.truncated
    assert result.data["bytes"] == 1024
    assert result.data["body_truncated"] is True
    assert result.data["text"] == "x" * 1024


async def test_binary_body_is_not_forced_into_text(tmp_path: Path) -> None:
    def binary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00\x01\x02", headers={"content-type": "image/png"})

    result, _ = await call_tool(
        {"method": "GET", "url": "https://example.test/avatar.png"},
        workspace=tmp_path,
        handlers=[binary],
    )
    assert result.ok
    assert result.data["text"] is None
    assert result.data["sha256"] == hashlib.sha256(b"\x00\x01\x02").hexdigest()


async def test_body_can_be_saved_into_the_workspace(tmp_path: Path) -> None:
    payload = b'{"items": [1, 2, 3]}'

    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    result, _ = await call_tool(
        {
            "method": "GET",
            "url": "https://example.test/export.json",
            "save_to": "downloads/export.json",
        },
        workspace=tmp_path,
        handlers=[download],
    )
    saved = tmp_path / "downloads" / "export.json"
    assert saved.read_bytes() == payload
    assert result.data["saved_to"] == "downloads/export.json"
    assert result.data["sha256"] == hashlib.sha256(payload).hexdigest()


async def test_save_to_cannot_escape_the_workspace(tmp_path: Path) -> None:
    result, _ = await call_tool(
        {
            "method": "GET",
            "url": "https://example.test/data",
            "save_to": "../../escape.txt",
        },
        workspace=tmp_path,
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "workspace_escape"
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"method": "GET", "url": "ftp://example.test/file"}, "invalid_url"),
        ({"method": "BREW", "url": "https://example.test/"}, "invalid_arguments"),
        ({"method": "GET", "url": "https://user:pass@example.test/"}, "credentials_in_url"),
        ({"method": "GET"}, "invalid_arguments"),
        (
            {"method": "POST", "url": "https://example.test/", "body": [1, 2], "body_kind": "form"},
            "invalid_arguments",
        ),
        (
            {
                "method": "POST",
                "url": "https://example.test/",
                "body": {"a": 1},
                "body_kind": "raw",
            },
            "invalid_arguments",
        ),
    ],
)
async def test_bad_arguments_are_structured_errors(
    arguments: dict[str, Any], code: str, tmp_path: Path
) -> None:
    result, requests = await call_tool(arguments, workspace=tmp_path)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == code
    assert requests == []  # no request left the process
