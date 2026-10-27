from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """Embeddings are deferred (ADR-0006). None means: use full-text search only."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]] | None: ...


class NoopEmbedder:
    async def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        return None
