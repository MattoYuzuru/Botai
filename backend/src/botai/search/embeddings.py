"""Embedding providers for RAG (plan 2.5): one interface, batching, cache, retries, cost.

The provider is a detail: the indexer and hybrid search take `EmbeddingProvider` and never import a
concrete transport. Three implementations ship:

* `HashingEmbeddings` — offline, deterministic feature hashing (words + character trigrams, so
  Russian inflections still collide). No network, no keys, zero cost; the safe default when no
  embedding key is configured.
* `ApiEmbeddings` — OpenAI-compatible `/embeddings` endpoint with batching and bounded retries. The
  key is read from an environment variable **by name** and never appears in a result, log, error or
  the object representation.
* `ScriptedEmbeddings` — a deterministic mock for tests: exact texts (and configured synonyms) map
  to fixed vectors, everything else falls back to hashing. This is how the tests prove that the
  vector path really prefers a semantically close chunk.

`CachedEmbeddings` wraps any provider with an LRU by content hash: repeated texts never cost a
second call, and `usage()` reports calls / texts / cache hits / estimated tokens / cost.

Secrets: the API key is never logged, never in `repr`, never in an error message. Errors carry only
a status code and a short reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from botai.core.http import build_client

DEFAULT_DIMENSION = 384
DEFAULT_BATCH_SIZE = 64
DEFAULT_ATTEMPTS = 3
DEFAULT_USD_PER_MILLION_TOKENS = 0.02
_TOKEN = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")
_TRIGRAM_WEIGHT = 0.35


class EmbeddingError(RuntimeError):
    """An embedding call failed. The message is secret-free and safe to return to the model."""


@dataclass(frozen=True, slots=True)
class EmbeddingUsage:
    """What one provider or one ingest/search call spent."""

    calls: int = 0
    texts: int = 0
    cache_hits: int = 0
    estimated_tokens: int = 0
    cost_usd: float = 0.0

    def plus(self, other: EmbeddingUsage) -> EmbeddingUsage:
        return EmbeddingUsage(
            calls=self.calls + other.calls,
            texts=self.texts + other.texts,
            cache_hits=self.cache_hits + other.cache_hits,
            estimated_tokens=self.estimated_tokens + other.estimated_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
        )

    def minus(self, other: EmbeddingUsage) -> EmbeddingUsage:
        return EmbeddingUsage(
            calls=max(self.calls - other.calls, 0),
            texts=max(self.texts - other.texts, 0),
            cache_hits=max(self.cache_hits - other.cache_hits, 0),
            estimated_tokens=max(self.estimated_tokens - other.estimated_tokens, 0),
            cost_usd=round(max(self.cost_usd - other.cost_usd, 0.0), 8),
        )


class EmbeddingProvider(Protocol):
    """The only embedding API the indexer and the search know."""

    name: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def estimate_cost_usd(self, texts: Sequence[str]) -> float: ...

    def usage(self) -> EmbeddingUsage: ...


# --- math ----------------------------------------------------------------------------------------


def estimate_tokens(texts: Sequence[str]) -> int:
    """Cheap conservative token estimate (no tokenizer dependency)."""
    return sum(max(1, len(text) // 4) for text in texts)


def normalize(vector: Sequence[float]) -> list[float]:
    """L2-normalize; an all-zero vector stays zero (cosine treats it as no similarity)."""
    length = math.sqrt(sum(value * value for value in vector))
    if length == 0.0:
        return [0.0 for _ in vector]
    return [value / length for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; zero vectors give 0.0, not a division error."""
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def content_hash(text: str) -> str:
    """Content address of a text: the cache/dedup key, never a secret."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- offline hashing provider --------------------------------------------------------------------


def _grams(token: str, size: int = 3) -> list[str]:
    padded = f"^{token}$" if len(token) >= size else token
    return [padded[index : index + size] for index in range(len(padded) - size + 1)]


def hashing_vector(text: str, dimension: int) -> list[float]:
    """Deterministic feature hashing: whole tokens plus character trigrams.

    Trigrams are what makes «дедлайн» and «дедлайны» close without a model; they are not real
    semantics, which is why the live provider is configurable and the docs say so.
    """
    vector = [0.0] * dimension
    for token in _TOKEN.findall(text.casefold()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        vector[index] += -1.0 if digest[4] & 1 else 1.0
        if len(token) > 3:
            for gram in _grams(token):
                gram_digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                gram_index = int.from_bytes(gram_digest[:4], "big") % dimension
                sign = -1.0 if gram_digest[4] & 1 else 1.0
                vector[gram_index] += sign * _TRIGRAM_WEIGHT
    return normalize(vector)


class HashingEmbeddings:
    """Offline deterministic embeddings: no key, no network, zero cost."""

    name = "local-hash-v1"

    def __init__(self, dimension: int = DEFAULT_DIMENSION) -> None:
        if dimension < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dimension = dimension
        self._calls = 0
        self._texts = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._calls += len(texts)
        self._texts += len(texts)
        return [hashing_vector(text, self.dimension) for text in texts]

    def estimate_cost_usd(self, texts: Sequence[str]) -> float:
        del texts
        return 0.0

    def usage(self) -> EmbeddingUsage:
        return EmbeddingUsage(calls=self._calls, texts=self._texts)


# --- scripted mock -------------------------------------------------------------------------------


class ScriptedEmbeddings:
    """A deterministic mock for tests: configured texts map to configured vectors.

    Values are normalized and padded/trimmed to `dimension`; an unknown text falls back to the same
    hashing vector as `HashingEmbeddings`, so a test can mix known «concepts» with ordinary text.
    Not for production: its purpose is to make semantic similarity controllable in a test.
    """

    name = "mock-scripted"

    def __init__(
        self,
        vectors: Mapping[str, Sequence[float]],
        *,
        dimension: int = DEFAULT_DIMENSION,
        synonyms: Mapping[str, str] | None = None,
    ) -> None:
        self.dimension = dimension
        self._vectors = {
            self._key(text): normalize(self._fit(vector, dimension))
            for text, vector in vectors.items()
        }
        self._synonyms = {
            key.casefold(): value.casefold() for key, value in (synonyms or {}).items()
        }
        self._calls = 0
        self._texts = 0
        self.embedded_texts: list[str] = []

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.strip().casefold().split())

    @staticmethod
    def _fit(vector: Sequence[float], dimension: int) -> list[float]:
        values = list(vector)[:dimension]
        return values + [0.0] * (dimension - len(values))

    def _vector(self, text: str) -> list[float]:
        normalized = self._key(text)
        for candidate in (normalized, self._synonyms.get(normalized, "")):
            if candidate and candidate in self._vectors:
                return self._vectors[candidate]
        return hashing_vector(text, self.dimension)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._calls += len(texts)
        self._texts += len(texts)
        self.embedded_texts.extend(texts)
        return [self._vector(text) for text in texts]

    def estimate_cost_usd(self, texts: Sequence[str]) -> float:
        del texts
        return 0.0

    def usage(self) -> EmbeddingUsage:
        return EmbeddingUsage(calls=self._calls, texts=self._texts)


# --- API provider --------------------------------------------------------------------------------


class ApiEmbeddings:
    """OpenAI-compatible embeddings API: batching, bounded retries, cost estimate, no key leaks.

    The key is resolved from `api_key_env` on every call and only placed into the `Authorization`
    header. It is not stored on the object, not included in `repr`, and never put into an error.
    `client` is injectable for tests (`httpx.MockTransport`), so no live network is needed.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key_env: str = "BOTAI_EMBEDDING_API_KEY",
        dimension: int = DEFAULT_DIMENSION,
        batch_size: int = DEFAULT_BATCH_SIZE,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout_s: float = 30.0,
        usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = f"api:{model}"
        self.dimension = dimension
        self._endpoint = endpoint
        self._model = model
        self._api_key_env = api_key_env
        self._batch_size = max(1, batch_size)
        self._attempts = max(1, attempts)
        self._timeout_s = timeout_s
        self._usd_per_million_tokens = usd_per_million_tokens
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._sleep = sleep
        self._calls = 0
        self._texts = 0
        self._tokens = 0

    async def __aenter__(self) -> ApiEmbeddings:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _api_key(self) -> str:
        value = os.environ.get(self._api_key_env, "").strip()
        if not value:
            raise EmbeddingError(
                f"embedding API key is not configured ({self._api_key_env}); "
                "fall back to the offline provider"
            )
        return value

    def _get_client(self) -> httpx.AsyncClient:
        # Outbound HTTP goes through the vetted factory (`core.http`), never a raw client: the
        # guard test in tests/core/test_architecture.py enforces one place for timeouts/retries.
        if self._client is None:
            host = urlsplit(self._endpoint).hostname
            if not host:
                raise EmbeddingError("embedding API endpoint is invalid")
            self._client = build_client(
                allow_hosts={host},
                proxy_url=None,
                transport=self._transport,
            )
        return self._client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(await self._embed_batch(batch))
            self._calls += 1
            self._texts += len(batch)
            self._tokens += estimate_tokens(batch)
        return vectors

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        last_error = "unknown error"
        for attempt in range(self._attempts):
            try:
                response = await self._get_client().post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key()}"},
                    json={"model": self._model, "input": list(batch)},
                )
            except httpx.HTTPError as exc:
                last_error = f"transport error: {type(exc).__name__}"
                if attempt + 1 < self._attempts:
                    await self._sleep(0.5 * (2**attempt))
                    continue
                raise EmbeddingError(last_error) from None
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"embedding API status {response.status_code}"
                if attempt + 1 < self._attempts:
                    await self._sleep(_retry_delay(response, attempt))
                    continue
                raise EmbeddingError(last_error)
            if response.status_code >= 400:
                # Never include the response body: it may echo the input text.
                raise EmbeddingError(f"embedding API rejected the request ({response.status_code})")
            return _parse_embeddings(response, len(batch))
        raise EmbeddingError(last_error)

    def estimate_cost_usd(self, texts: Sequence[str]) -> float:
        return round(estimate_tokens(texts) * self._usd_per_million_tokens / 1_000_000, 8)

    def usage(self) -> EmbeddingUsage:
        return EmbeddingUsage(
            calls=self._calls,
            texts=self._texts,
            estimated_tokens=self._tokens,
            cost_usd=round(self._tokens * self._usd_per_million_tokens / 1_000_000, 8),
        )

    def __repr__(self) -> str:
        return f"ApiEmbeddings(model={self._model!r}, dimension={self.dimension})"


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after", "")
    try:
        value = float(retry_after)
    except ValueError:
        value = 0.0
    if value > 0:
        return min(value, 30.0)
    return 0.5 * (2**attempt)


def _parse_embeddings(response: httpx.Response, expected: int) -> list[list[float]]:
    try:
        payload = response.json()
        rows = payload["data"]
        ordered = sorted(rows, key=lambda row: int(row["index"]))
        vectors = [[float(value) for value in row["embedding"]] for row in ordered]
    except (KeyError, TypeError, ValueError):
        raise EmbeddingError("embedding API returned an unexpected body") from None
    if len(vectors) != expected:
        raise EmbeddingError(f"embedding API returned {len(vectors)} vectors for {expected} texts")
    return vectors


# --- cache ---------------------------------------------------------------------------------------


class CachedEmbeddings:
    """LRU by content hash around any provider; the same text is never embedded twice in a process.

    The database cache is separate: `chunk_embeddings` stores `content_hash`, so the indexer skips
    texts that are already indexed for the same model even across restarts.
    """

    def __init__(self, inner: EmbeddingProvider, *, max_items: int = 4096) -> None:
        self.name = inner.name
        self.dimension = inner.dimension
        self._inner = inner
        self._max_items = max(1, max_items)
        self._items: OrderedDict[str, list[float]] = OrderedDict()
        self._hits = 0
        self._delta = EmbeddingUsage()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        keys = [content_hash(text) for text in texts]
        missing: dict[str, str] = {}
        results: list[list[float] | None] = [None] * len(texts)
        for position, key in enumerate(keys):
            cached = self._items.get(key)
            if cached is None:
                missing.setdefault(key, texts[position])
            else:
                self._items.move_to_end(key)
                self._hits += 1
                results[position] = cached
        if missing:
            before = self._inner.usage()
            fresh = await self._inner.embed(list(missing.values()))
            after = self._inner.usage()
            self._delta = self._delta.plus(after.minus(before))
            for key, vector in zip(missing.keys(), fresh, strict=True):
                self._items[key] = vector
                while len(self._items) > self._max_items:
                    self._items.popitem(last=False)
            for position, key in enumerate(keys):
                if results[position] is None:
                    results[position] = self._items.get(key, [0.0] * self.dimension)
        return [vector if vector is not None else [0.0] * self.dimension for vector in results]

    def estimate_cost_usd(self, texts: Sequence[str]) -> float:
        return self._inner.estimate_cost_usd(texts)

    def usage(self) -> EmbeddingUsage:
        return EmbeddingUsage(
            calls=self._delta.calls,
            texts=self._delta.texts,
            cache_hits=self._hits,
            estimated_tokens=self._delta.estimated_tokens,
            cost_usd=self._delta.cost_usd,
        )
