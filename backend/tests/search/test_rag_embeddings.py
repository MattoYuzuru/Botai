"""Embedding providers: determinism, semantics on mock vectors, cache, retries, no key leaks."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from botai.search.embeddings import (
    ApiEmbeddings,
    CachedEmbeddings,
    EmbeddingError,
    EmbeddingUsage,
    HashingEmbeddings,
    ScriptedEmbeddings,
    content_hash,
    cosine_similarity,
    estimate_tokens,
    hashing_vector,
    normalize,
)


def test_hashing_vector_is_deterministic_and_normalized() -> None:
    first = hashing_vector("дедлайн по матанализу", 64)
    again = hashing_vector("дедлайн по матанализу", 64)
    other = hashing_vector("расписание пар", 64)
    assert first == again
    assert first != other
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_hashing_keeps_inflections_closer_than_unrelated_text() -> None:
    base = hashing_vector("дедлайн по матанализу", 128)
    inflection = hashing_vector("дедлайны по матанализу", 128)
    unrelated = hashing_vector("расписание пар на неделю", 128)
    assert cosine_similarity(base, inflection) > cosine_similarity(base, unrelated)


async def test_hashing_provider_reports_free_usage() -> None:
    provider = HashingEmbeddings(32)
    vectors = await provider.embed(["раз", "два"])
    assert len(vectors) == 2
    assert provider.dimension == 32
    assert provider.usage().texts == 2
    assert provider.estimate_cost_usd(["раз"]) == 0.0


async def test_scripted_embeddings_put_synonyms_on_one_concept() -> None:
    provider = ScriptedEmbeddings(
        {
            "дедлайн по машинному обучению": [1.0, 0.0, 0.0, 0.0],
            "срок сдачи работы по ML": [1.0, 0.0, 0.0, 0.0],
            "расписание пар": [0.0, 1.0, 0.0, 0.0],
        },
        dimension=4,
    )
    close, also_close, far = await provider.embed(
        ["срок сдачи работы по ML", "дедлайн по машинному обучению", "расписание пар"]
    )
    assert cosine_similarity(close, also_close) > 0.99
    assert cosine_similarity(close, also_close) > cosine_similarity(close, far)


async def test_cache_does_not_embed_the_same_text_twice() -> None:
    class Counting:
        name = "counting"
        dimension = 4

        def __init__(self) -> None:
            self.texts: list[str] = []

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def estimate_cost_usd(self, texts: Sequence[str]) -> float:
            return 0.001 * len(texts)

        def usage(self) -> EmbeddingUsage:
            return EmbeddingUsage(texts=len(self.texts), calls=len(self.texts))

    inner = Counting()
    cached = CachedEmbeddings(inner)
    first = await cached.embed(["одинаковый", "второй"])
    again = await cached.embed(["одинаковый"])
    assert inner.texts == ["одинаковый", "второй"]
    assert first[0] == again[0]
    assert cached.usage().cache_hits == 1
    assert content_hash("x") == content_hash("x")
    assert normalize([3.0, 4.0]) == [0.6, 0.8]
    assert estimate_tokens(["abcd"]) >= 1


# --- API provider --------------------------------------------------------------------------------


def _api(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> ApiEmbeddings:
    return ApiEmbeddings(
        endpoint="https://api.example.test/v1/embeddings",
        model="text-embedding-3-small",
        dimension=2,
        batch_size=2,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_api_embeddings_batch_retry_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTAI_EMBEDDING_API_KEY", "unit-test-key")
    calls: list[dict[str, object]] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(
            {
                "authorization": request.headers.get("authorization"),
                "input": payload["input"],
                "url": str(request.url),
            }
        )
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        batch = payload["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": position, "embedding": [float(position), 1.0]}
                    for position in range(len(batch))
                ]
            },
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider = _api(handler, sleep=fake_sleep)
    try:
        vectors = await provider.embed(["one", "two", "three"])
        assert len(vectors) == 3
        assert calls[0]["authorization"] == "Bearer unit-test-key"
        assert calls[0]["url"].startswith("https://api.example.test/v1/embeddings")
        # Retried once (same batch), then two successful batches.
        assert len(calls) == 3
        assert calls[-1]["input"] == ["three"]
        assert sleeps
        usage = provider.usage()
        assert usage.texts == 3
        assert usage.calls == 2
        assert usage.estimated_tokens >= 1
        assert "unit-test-key" not in repr(provider)
    finally:
        await provider.aclose()


async def test_api_embeddings_missing_key_is_reported_without_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOTAI_EMBEDDING_API_KEY", raising=False)
    provider = _api(lambda request: httpx.Response(200, json={"data": []}))
    try:
        with pytest.raises(EmbeddingError) as error:
            await provider.embed(["text"])
    finally:
        await provider.aclose()
    message = str(error.value)
    assert "BOTAI_EMBEDDING_API_KEY" in message
    assert "not configured" in message


async def test_api_embeddings_rejects_without_echoing_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTAI_EMBEDDING_API_KEY", "unit-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="echo: unit-test-key and private text")

    provider = _api(handler)
    try:
        with pytest.raises(EmbeddingError) as error:
            await provider.embed(["private text"])
    finally:
        await provider.aclose()
    assert "unit-test-key" not in str(error.value)
    assert "private text" not in str(error.value)
    assert "400" in str(error.value)


async def test_api_embeddings_retries_5xx_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTAI_EMBEDDING_API_KEY", "unit-test-key")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async def fake_sleep(delay: float) -> None:
        del delay

    provider = _api(handler, sleep=fake_sleep, attempts=2)
    try:
        with pytest.raises(EmbeddingError):
            await provider.embed(["text"])
        assert len(calls) == 2
    finally:
        await provider.aclose()
