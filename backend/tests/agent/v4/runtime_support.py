"""Helpers for the v4 runtime tests (unique name: `agent/v4/executor` tests own their own)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.agent.v4.runtime import RunLimits, RunTask
from botai.core.llm import LlmMessage, LlmResponse, LlmToolCall, LlmUsage
from botai.settings import Settings

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # Saturday: DeepSeek off-peak


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"env": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def usage(
    *,
    miss: int = 10,
    hit: int = 0,
    output: int = 2,
    model: str = "deepseek-flash",
    latency_ms: int = 5,
) -> LlmUsage:
    return LlmUsage(
        input_cache_miss=miss,
        input_cache_hit=hit,
        output=output,
        model=model,
        latency_ms=latency_ms,
    )


def answer(text: str = "готово", **kw: Any) -> LlmResponse:
    return LlmResponse(content=text, usage=kw.pop("usage", usage()), **kw)


def call(
    name: str = "echo",
    arguments: dict[str, Any] | None = None,
    call_id: str = "c1",
) -> LlmToolCall:
    return LlmToolCall(id=call_id, name=name, arguments=arguments or {})


def tool_response(*calls: LlmToolCall, content: str = "", **kw: Any) -> LlmResponse:
    return LlmResponse(
        content=content,
        tool_calls=list(calls),
        usage=kw.pop("usage", usage()),
        **kw,
    )


def echo_spec(name: str = "echo", *, policy: str = "read") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"tool {name}",
        parameters=object_schema({"text": {"type": "string"}}, required=["text"]),
        policy=policy,  # type: ignore[arg-type]
    )


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


def handler_tool(
    name: str = "echo",
    handler: Handler | None = None,
    *,
    spec: ToolSpec | None = None,
    policy: str = "read",
) -> FunctionTool:
    async def default(args: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success(summary="ok", data={"echo": args.get("text")})

    return FunctionTool(spec or echo_spec(name, policy=policy), handler or default)


def task(**kw: Any) -> RunTask:
    values: dict[str, Any] = {
        "task": "проверка цикла",
        "system": "система",
        "messages": [LlmMessage(role="user", content="привет")],
    }
    values.update(kw)
    return RunTask(**values)


def limits(**kw: Any) -> RunLimits:
    return RunLimits(**kw)


# --- provider wire helpers ----------------------------------------------------------------------


def chat_completion_body(
    *, content: str = "готово", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-fixture",
        "model": "fixture-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
    }


def sse(*chunks: dict[str, Any]) -> httpx.Response:
    text = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        content=(text + "data: [DONE]\n\n").encode(),
        headers={"content-type": "text/event-stream"},
    )


def answer_chunks(text: str = "ок") -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 3,
                "prompt_cache_hit_tokens": 5,
                "prompt_cache_miss_tokens": 15,
            },
        },
    ]


class Wire:
    """Records requests, answers with a script of handlers (the last one repeats)."""

    def __init__(self, *handlers: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handlers = list(handlers) or [lambda _request: httpx.Response(200, json={})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.handlers) - 1)
        return self.handlers[index](request)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)
