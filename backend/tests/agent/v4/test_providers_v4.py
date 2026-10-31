"""Providers (L0): mock queue, DeepSeek adapter, Anthropic/OpenAI mapping on MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from botai.agent.v4.providers import MockProvider
from botai.agent.v4.providers.anthropic import (
    AnthropicProvider,
    to_anthropic_messages,
)
from botai.agent.v4.providers.anthropic import (
    build_body as anthropic_body,
)
from botai.agent.v4.providers.base import require_secret
from botai.agent.v4.providers.deepseek import DeepSeekProviderAdapter
from botai.agent.v4.providers.factory import build_provider
from botai.agent.v4.providers.openai_compat import OpenAICompatProvider
from botai.agent.v4.providers.openai_compat import build_body as openai_body
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import (
    LlmFixtureMissing,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmTool,
    LlmToolCall,
    LlmUsage,
)
from botai.settings import Settings
from tests.agent.v4.runtime_support import NOW, Wire, answer_chunks, make_settings, sse

ANTHROPIC_KEY = "sk-ant-FIXTURE-DO-NOT-LEAK"
OPENAI_KEY = "sk-openai-FIXTURE-DO-NOT-LEAK"


def _request(**kw: Any) -> LlmRequest:
    values: dict[str, Any] = {"messages": [LlmMessage(role="user", content="привет")]}
    values.update(kw)
    return LlmRequest(**values)


# --- mock provider ------------------------------------------------------------------------------


async def test_mock_provider_replays_the_queue_and_records_requests() -> None:
    provider = MockProvider.from_responses(LlmResponse(content="раз"), LlmResponse(content="два"))
    first = await provider.complete(_request())
    second = await provider.complete(_request())
    assert (first.content, second.content) == ("раз", "два")
    assert len(provider.requests) == 2
    assert provider.name == "mock"


async def test_mock_provider_fails_loudly_when_the_script_is_empty() -> None:
    provider = MockProvider()
    with pytest.raises(LlmFixtureMissing):
        await provider.complete(_request())


# --- DeepSeek adapter ---------------------------------------------------------------------------


async def test_deepseek_adapter_delegates_to_the_v3_provider() -> None:
    wire = Wire(lambda _request: sse(*answer_chunks("эй")))
    settings = make_settings(llm_provider="deepseek", deepseek_api_key="sk-deepseek-FIXTURE")
    provider = DeepSeekProviderAdapter(
        settings,
        transport=httpx.MockTransport(wire),
        now=lambda: NOW,
    )
    response = await provider.complete(_request(purpose="agent"))
    assert provider.name == "deepseek"
    assert response.content == "эй"
    assert str(wire.requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert wire.body()["model"] == "deepseek-flash"
    await provider.aclose()


# --- Anthropic ----------------------------------------------------------------------------------


def anthropic(handler: Any, **settings: Any) -> AnthropicProvider:
    return AnthropicProvider(
        make_settings(anthropic_api_key=ANTHROPIC_KEY, **settings),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )


async def _no_sleep(seconds: float) -> None:
    return None


def anthropic_ok(
    *,
    content: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_fixture",
            "model": "claude-haiku-4-5",
            "content": content or [{"type": "text", "text": "готово"}],
            "usage": usage
            or {
                "input_tokens": 10,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 3,
                "output_tokens": 7,
            },
            "stop_reason": "end_turn",
        },
    )


def test_anthropic_body_maps_system_tools_and_tool_results() -> None:
    request = LlmRequest(
        messages=[
            LlmMessage(role="system", content="система"),
            LlmMessage(role="user", content="вопрос"),
            LlmMessage(
                role="assistant",
                content="",
                tool_calls=[LlmToolCall(id="tu1", name="http_request", arguments={"url": "x"})],
            ),
            LlmMessage(role="tool", tool_call_id="tu1", content='{"ok": true}'),
            LlmMessage(role="user", content="продолжай"),
        ],
        tools=[
            LlmTool(
                name="http_request",
                description="запрос",
                parameters={"type": "object", "additionalProperties": False, "properties": {}},
            )
        ],
    )
    body = anthropic_body(request, "claude-haiku-4-5")
    assert body["system"] == "система"
    assert body["tools"] == [
        {
            "name": "http_request",
            "description": "запрос",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        }
    ]
    messages = body["messages"]
    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "вопрос"}]}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0] == {
        "type": "tool_use",
        "id": "tu1",
        "name": "http_request",
        "input": {"url": "x"},
    }
    # The tool result and the following user text merge into one user turn.
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu1",
        "content": '{"ok": true}',
    }
    assert messages[2]["content"][1] == {"type": "text", "text": "продолжай"}


def test_anthropic_body_injects_a_json_schema_instruction() -> None:
    request = LlmRequest(
        messages=[
            LlmMessage(role="system", content="система"),
            LlmMessage(role="user", content="верни JSON"),
        ],
        json_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    body = anthropic_body(request, "claude-haiku-4-5")
    assert "система" in body["system"]
    assert "JSON Schema" in body["system"]


def test_anthropic_messages_group_parallel_tool_results() -> None:
    messages, system = to_anthropic_messages(
        [
            LlmMessage(role="user", content="q"),
            LlmMessage(
                role="assistant",
                content="",
                tool_calls=[
                    LlmToolCall(id="a", name="first", arguments={}),
                    LlmToolCall(id="b", name="second", arguments={}),
                ],
            ),
            LlmMessage(role="tool", tool_call_id="a", content="1"),
            LlmMessage(role="tool", tool_call_id="b", content="2"),
        ]
    )
    assert system == ""
    assert messages[-1]["role"] == "user"
    assert [block["tool_use_id"] for block in messages[-1]["content"]] == ["a", "b"]


async def test_anthropic_complete_sends_headers_and_parses_usage() -> None:
    wire = Wire(
        lambda _request: anthropic_ok(
            content=[
                {"type": "text", "text": "готово"},
                {"type": "tool_use", "id": "tu2", "name": "http_request", "input": {"url": "y"}},
            ]
        )
    )
    provider = anthropic(wire)
    response = await provider.complete(_request(max_tokens=1500))
    request = wire.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == ANTHROPIC_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = wire.body()
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 1500
    assert response.content == "готово"
    assert response.tool_calls[0].id == "tu2"
    assert response.tool_calls[0].arguments == {"url": "y"}
    assert response.usage.input_cache_hit == 5
    assert response.usage.input_cache_miss == 13
    assert response.usage.output == 7
    await provider.aclose()


async def test_anthropic_model_is_chosen_by_tier_and_request_override() -> None:
    wire = Wire(lambda _request: anthropic_ok())
    provider = anthropic(wire, anthropic_model_pro="claude-pro-FIXTURE")
    await provider.complete(_request(tier="pro"))
    assert wire.body()["model"] == "claude-pro-FIXTURE"
    await provider.complete(_request(model="explicit-model"))
    assert wire.body()["model"] == "explicit-model"


async def test_anthropic_missing_key_fails_before_any_request() -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return anthropic_ok()

    provider = AnthropicProvider(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
    assert called == []


async def test_anthropic_malformed_body_is_an_error() -> None:
    wire = Wire(lambda _request: httpx.Response(200, json={"id": "msg", "type": "message"}))
    provider = anthropic(wire)
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
    assert "неожиданном формате" in caught.value.detail


async def test_anthropic_auth_error_is_not_retried() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    wire = Wire(lambda _request: httpx.Response(401, json={"error": {"message": "nope"}}))
    provider = AnthropicProvider(
        make_settings(anthropic_api_key=ANTHROPIC_KEY),
        transport=httpx.MockTransport(wire),
        sleep=sleep,
    )
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
    assert "Ключ модели отклонён" in caught.value.detail
    assert sleeps == []
    assert len(wire.requests) == 1


async def test_anthropic_retries_a_500_then_succeeds() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    wire = Wire(
        lambda _request: httpx.Response(500),
        lambda _request: anthropic_ok(),
    )
    provider = AnthropicProvider(
        make_settings(anthropic_api_key=ANTHROPIC_KEY),
        transport=httpx.MockTransport(wire),
        sleep=sleep,
    )
    response = await provider.complete(_request())
    assert response.content == "готово"
    assert len(wire.requests) == 2
    assert sleeps == [1.0]


async def test_anthropic_honours_retry_after() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    wire = Wire(
        lambda _request: httpx.Response(429, headers={"retry-after": "5"}),
        lambda _request: anthropic_ok(),
    )
    provider = AnthropicProvider(
        make_settings(anthropic_api_key=ANTHROPIC_KEY),
        transport=httpx.MockTransport(wire),
        sleep=sleep,
    )
    await provider.complete(_request())
    assert sleeps == [5.0]


async def test_anthropic_retries_twice_then_fails() -> None:
    wire = Wire(lambda _request: httpx.Response(503))
    provider = anthropic(wire)
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
    assert len(wire.requests) == 3


async def test_anthropic_base_url_with_v1_is_not_doubled() -> None:
    wire = Wire(lambda _request: anthropic_ok())
    provider = AnthropicProvider(
        make_settings(anthropic_api_key=ANTHROPIC_KEY, anthropic_base_url="https://proxy.test/v1"),
        transport=httpx.MockTransport(wire),
        sleep=_no_sleep,
    )
    await provider.complete(_request())
    assert str(wire.requests[0].url) == "https://proxy.test/v1/messages"


# --- OpenAI-compatible --------------------------------------------------------------------------


def openai(handler: Any, **settings: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        make_settings(llm_openai_api_key=OPENAI_KEY, **settings),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )


def openai_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "готово",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "http_request",
                                    "arguments": json.dumps({"url": "z"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 6,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        },
    )


def test_openai_body_uses_function_tools_and_schema_instruction() -> None:
    request = LlmRequest(
        messages=[
            LlmMessage(role="system", content="система"),
            LlmMessage(role="user", content="q"),
        ],
        tools=[LlmTool(name="f", description="d", parameters={"type": "object"})],
        json_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    body = openai_body(request, "gpt-fixture")
    assert body["model"] == "gpt-fixture"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "f"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "JSON Schema" in body["messages"][1]["content"]
    assert body["messages"][2] == {"role": "user", "content": "q"}


async def test_openai_complete_sends_headers_and_parses_tool_calls() -> None:
    wire = Wire(lambda _request: openai_ok())
    provider = openai(wire)
    response = await provider.complete(_request())
    assert provider.name == "openai_compat"
    request = wire.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert wire.body()["model"] == "gpt-4.1-mini"
    assert response.content == "готово"
    assert response.tool_calls[0].name == "http_request"
    assert response.tool_calls[0].arguments == {"url": "z"}
    assert response.usage.input_cache_hit == 30
    assert response.usage.input_cache_miss == 10
    assert response.usage.output == 6
    await provider.aclose()


async def test_openai_unparsable_tool_arguments_are_marked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "f", "arguments": "not-json"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = openai(handler)
    response = await provider.complete(_request())
    assert "__invalid_arguments__" in response.tool_calls[0].arguments


async def test_openai_malformed_body_is_an_error() -> None:
    wire = Wire(lambda _request: httpx.Response(200, json={"id": "chatcmpl", "choices": []}))
    provider = openai(wire)
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE


async def test_openai_missing_key_fails_before_any_request() -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return openai_ok()

    provider = OpenAICompatProvider(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(BotaiError) as caught:
        await provider.complete(_request())
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE
    assert called == []


async def test_openai_retries_on_429() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    wire = Wire(
        lambda _request: httpx.Response(429, headers={"retry-after": "0.01"}),
        lambda _request: openai_ok(),
    )
    provider = OpenAICompatProvider(
        make_settings(llm_openai_api_key=OPENAI_KEY),
        transport=httpx.MockTransport(wire),
        sleep=sleep,
    )
    response = await provider.complete(_request())
    assert response.content == "готово"
    assert sleeps
    assert sleeps[0] >= 0.01


async def test_openai_base_url_and_model_overrides() -> None:
    wire = Wire(lambda _request: openai_ok())
    provider = OpenAICompatProvider(
        make_settings(llm_openai_api_key=OPENAI_KEY),
        base_url="http://127.0.0.1:11434/v1",
        model="llama-fixture",
        transport=httpx.MockTransport(wire),
        sleep=_no_sleep,
    )
    await provider.complete(_request())
    assert str(wire.requests[0].url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert wire.body()["model"] == "llama-fixture"


# --- factory ------------------------------------------------------------------------------------


def test_factory_builds_every_supported_provider() -> None:
    assert isinstance(build_provider(make_settings(llm_provider="mock")), MockProvider)
    deepseek = build_provider(
        make_settings(llm_provider="deepseek", deepseek_api_key="sk-deepseek-FIXTURE")
    )
    assert isinstance(deepseek, DeepSeekProviderAdapter)
    anthropic_provider = build_provider(
        make_settings(llm_provider="anthropic", anthropic_api_key=ANTHROPIC_KEY)
    )
    assert isinstance(anthropic_provider, AnthropicProvider)
    openai_provider = build_provider(
        make_settings(llm_provider="openai_compat", llm_openai_api_key=OPENAI_KEY)
    )
    assert isinstance(openai_provider, OpenAICompatProvider)


def test_factory_refuses_an_unknown_provider_name() -> None:
    bogus = Settings.model_construct(llm_provider="bogus")
    with pytest.raises(ValueError, match="unknown llm_provider"):
        build_provider(bogus)


def test_require_secret_never_echoes_the_value() -> None:
    from pydantic import SecretStr

    with pytest.raises(BotaiError) as caught:
        require_secret(SecretStr(""), provider="anthropic")
    assert "Ключ модели" in caught.value.detail


def test_usage_defaults_are_used_when_a_provider_reports_nothing() -> None:
    usage = LlmUsage()
    assert usage.input_cache_miss == 0
    assert usage.output == 0
