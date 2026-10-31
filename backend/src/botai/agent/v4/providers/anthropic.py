"""Anthropic (Claude) provider for v4: the Messages API with tool use (D-012, §10).

Mapping between the shared `core.llm` types and the Anthropic wire format:

* system messages become the top-level ``system`` string;
* assistant `tool_calls` become ``tool_use`` blocks; `tool` messages become ``tool_result``
  blocks grouped into the next user turn (the API requires it);
* `LlmRequest.tools` become ``tools`` with ``input_schema`` (Anthropic has no ``function``
  wrapper); tool results are plain strings.

Extended thinking is not wired yet: a request's `thinking`/`effort` is ignored for Claude (the
provider stays correct for all models rather than sending an unsupported parameter). Streaming is
not wired either; the runtime consumes whole responses.

The HTTP client comes from `botai.agent.v4.http` (single factory); `transport` is for tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from botai.agent.v4.http import build_client
from botai.agent.v4.providers._retry import Sleep, llm_error, post_json
from botai.agent.v4.providers.base import require_secret
from botai.core.llm import (
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmTool,
    LlmToolCall,
    LlmUsage,
)
from botai.settings import Settings

MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 64_000
# A non-streaming Claude answer can take a while; the provider waits up to this per attempt.
CALL_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=20.0, pool=10.0)


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Ответь одним JSON-объектом, строго по этой JSON Schema, без пояснений и без Markdown:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + MESSAGES_PATH


def _tool_to_wire(tool: LlmTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def _text_blocks(message: LlmMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return blocks


def _tool_result_block(message: LlmMessage) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id or "",
        "content": message.content,
    }


def to_anthropic_messages(
    messages: Sequence[LlmMessage],
) -> tuple[list[dict[str, Any]], str]:
    """Convert the shared conversation into `(messages, system)`.

    Consecutive same-role turns merge; consecutive tool results become one user turn with several
    `tool_result` blocks, which is what the Messages API expects after parallel tool calls.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            existing = converted[-1]["content"]
            if isinstance(existing, list):
                existing.extend(blocks)
            else:  # pragma: no cover - the converter below always emits lists
                converted[-1]["content"] = [{"type": "text", "text": str(existing)}, *blocks]
            return
        converted.append({"role": role, "content": list(blocks)})

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "tool":
            push("user", [_tool_result_block(message)])
            continue
        if message.role == "assistant":
            push("assistant", _text_blocks(message))
            continue
        push("user", _text_blocks(message))
    return converted, "\n\n".join(system_parts)


def build_body(request: LlmRequest, model: str) -> dict[str, Any]:
    """The JSON body for `POST /v1/messages`. Pure, so the mapping is testable."""
    messages, system = to_anthropic_messages(request.messages)
    if request.json_schema is not None:
        # Anthropic has no response_format: the schema travels as a system instruction.
        instruction = _schema_instruction(request.json_schema)
        system = f"{system}\n\n{instruction}" if system else instruction
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": min(request.max_tokens, MAX_OUTPUT_TOKENS),
        "messages": messages,
        "temperature": request.temperature,
    }
    if system:
        body["system"] = system
    if request.tools:
        body["tools"] = [_tool_to_wire(tool) for tool in request.tools]
    return body


def _tool_calls_from_wire(content: Any) -> list[LlmToolCall]:
    calls: list[LlmToolCall] = []
    if not isinstance(content, list):
        return calls
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        arguments = block.get("input")
        calls.append(
            LlmToolCall(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def content_from_wire(content: Any) -> str:
    """Concatenated text blocks; thinking blocks are not shown to the model or the user."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def usage_from_wire(raw: Mapping[str, Any] | None, *, model: str, latency_ms: int) -> LlmUsage:
    usage = raw or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    uncached = int(usage.get("input_tokens") or 0)
    return LlmUsage(
        input_cache_hit=cache_read,
        input_cache_miss=uncached + cache_write,
        output=int(usage.get("output_tokens") or 0),
        model=model,
        latency_ms=latency_ms,
    )


def parse_response(data: Mapping[str, Any], *, fallback_model: str, latency_ms: int) -> LlmResponse:
    if not isinstance(data.get("content"), list):
        raise ValueError("no content blocks in the response")
    model = str(data.get("model") or fallback_model)
    raw_usage = data.get("usage")
    return LlmResponse(
        content=content_from_wire(data.get("content")),
        tool_calls=_tool_calls_from_wire(data.get("content")),
        usage=usage_from_wire(
            raw_usage if isinstance(raw_usage, Mapping) else None,
            model=model,
            latency_ms=latency_ms,
        ),
    )


class AnthropicProvider:
    """`Provider` over `POST /v1/messages`; the key comes from settings or an explicit override."""

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._url = _messages_url(base_url or settings.anthropic_base_url)
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "anthropic"

    def _resolve_model(self, request: LlmRequest) -> str:
        if request.model:
            return request.model
        if self._model:
            return self._model
        if request.tier == "pro":
            return self._settings.anthropic_model_pro
        return self._settings.anthropic_model_fast

    def _headers(self) -> dict[str, str]:
        key = self._api_key
        if key is None:
            key = require_secret(self._settings.anthropic_api_key, provider="anthropic")
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client(
                timeout=CALL_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    async def complete(self, request: LlmRequest) -> LlmResponse:
        model = self._resolve_model(request)
        body = build_body(request, model)
        data, latency_ms = await post_json(
            self._http(),
            self._url,
            headers=self._headers(),
            body=body,
            sleep=self._sleep,
        )
        try:
            return parse_response(data, fallback_model=model, latency_ms=latency_ms)
        except (TypeError, ValueError) as exc:
            raise llm_error("Модель вернула ответ в неожиданном формате.") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "ANTHROPIC_VERSION",
    "AnthropicProvider",
    "build_body",
    "content_from_wire",
    "parse_response",
    "to_anthropic_messages",
    "usage_from_wire",
]
