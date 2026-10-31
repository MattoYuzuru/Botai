"""OpenAI-compatible provider for v4: any endpoint that speaks `/chat/completions`.

Configured by `llm_openai_base_url` / `llm_openai_api_key` / models in settings (D-012); this
covers OpenAI itself, Ollama, vLLM, LiteLLM and other compatible gateways. Tool calls use the
standard `tools` / `tool_calls` shape; `LlmRequest`/`LlmResponse` are the shared `core.llm` types.

The HTTP client comes from `botai.agent.v4.http` (single factory); `transport` is for tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
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

CHAT_PATH = "/chat/completions"
MAX_OUTPUT_TOKENS = 64_000
CALL_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=20.0, pool=10.0)


def _tool_to_wire(tool: LlmTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _message_to_wire(message: LlmMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "tool":
        wire["tool_call_id"] = message.tool_call_id
    if message.role == "assistant" and message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return wire


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Ответь одним JSON-объектом, строго по этой JSON Schema, без пояснений и без Markdown:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def build_body(request: LlmRequest, model: str) -> dict[str, Any]:
    """The JSON body for `POST /chat/completions`. Pure, so the mapping is testable byte for byte."""
    messages = [_message_to_wire(message) for message in request.messages]
    if request.json_schema is not None:
        lead = 0
        while lead < len(messages) and messages[lead]["role"] == "system":
            lead += 1
        instruction = {"role": "system", "content": _schema_instruction(request.json_schema)}
        messages.insert(lead, instruction)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": min(request.max_tokens, MAX_OUTPUT_TOKENS),
    }
    if request.tools:
        body["tools"] = [_tool_to_wire(tool) for tool in request.tools]
    if request.json_schema is not None:
        body["response_format"] = {"type": "json_object"}
    return body


def _tool_call_from_wire(raw: Mapping[str, Any]) -> LlmToolCall:
    function = raw.get("function") or {}
    raw_arguments = function.get("arguments") or "{}"
    text = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        arguments: dict[str, Any] = {"__invalid_arguments__": text[:500]}
    else:
        arguments = parsed if isinstance(parsed, dict) else {"__invalid_arguments__": text[:500]}
    return LlmToolCall(
        id=str(raw.get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=arguments,
    )


def usage_from_wire(raw: Mapping[str, Any] | None, *, model: str, latency_ms: int) -> LlmUsage:
    usage = raw or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, Mapping) else 0
    return LlmUsage(
        input_cache_miss=max(prompt - cached, 0),
        input_cache_hit=min(cached, prompt),
        output=int(usage.get("completion_tokens") or 0),
        model=model,
        latency_ms=latency_ms,
    )


def parse_response(data: Mapping[str, Any], *, fallback_model: str, latency_ms: int) -> LlmResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("no choices in the response")
    choice: Any = choices[0]
    message: Any = choice.get("message") if isinstance(choice, Mapping) else None
    message = message if isinstance(message, Mapping) else {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    raw_calls = message.get("tool_calls")
    calls = [
        _tool_call_from_wire(raw)
        for raw in (raw_calls if isinstance(raw_calls, list) else [])
        if isinstance(raw, Mapping)
    ]
    model = str(data.get("model") or fallback_model)
    raw_usage = data.get("usage")
    usage = usage_from_wire(
        raw_usage if isinstance(raw_usage, Mapping) else None,
        model=model,
        latency_ms=latency_ms,
    )
    return LlmResponse(
        content=content if isinstance(content, str) else "",
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
        tool_calls=calls,
        usage=usage,
    )


class OpenAICompatProvider:
    """`Provider` over any OpenAI-compatible `/chat/completions` endpoint."""

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
        self._base_url = (base_url or settings.llm_openai_base_url).rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "openai_compat"

    def _resolve_model(self, request: LlmRequest) -> str:
        if request.model:
            return request.model
        if self._model:
            return self._model
        if request.tier == "pro":
            return self._settings.llm_openai_model_pro
        return self._settings.llm_openai_model_fast

    def _headers(self) -> dict[str, str]:
        key = self._api_key
        if key is None:
            key = require_secret(self._settings.llm_openai_api_key, provider="openai_compat")
        return {
            "authorization": f"Bearer {key}",
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
            self._base_url + CHAT_PATH,
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
    "CHAT_PATH",
    "OpenAICompatProvider",
    "build_body",
    "parse_response",
]
