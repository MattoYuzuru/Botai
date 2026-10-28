"""Public search API. Results are untrusted; they grant no university network access."""

from botai.search.safety import institutional_search_query
from botai.search.service import SearchBudget, SearchResponse, SearchResult, SearchService

__all__ = [
    "SearchBudget",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "institutional_search_query",
]
