"""Search: public web search (v3) and RAG over the student's own material (phase 2.5).

RAG lives next to the public-search module because both answer "where does this text live", but the
RAG path only reads the user's own chunks, documents and source artifacts.
"""

from botai.search.hybrid import HybridFilters, HybridHit, HybridSearchResult, hybrid_search
from botai.search.rag import (
    IngestReport,
    RagAnswer,
    RagHit,
    RagIndexer,
    RagQueryService,
    RagSettings,
    rag_enabled,
    rag_settings,
    split_text,
)
from botai.search.safety import institutional_search_query
from botai.search.service import SearchBudget, SearchResponse, SearchResult, SearchService

__all__ = [
    "HybridFilters",
    "HybridHit",
    "HybridSearchResult",
    "IngestReport",
    "RagAnswer",
    "RagHit",
    "RagIndexer",
    "RagQueryService",
    "RagSettings",
    "SearchBudget",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "hybrid_search",
    "institutional_search_query",
    "rag_enabled",
    "rag_settings",
    "split_text",
]
