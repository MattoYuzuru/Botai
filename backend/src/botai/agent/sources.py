"""Source book of one chat turn: tools register what they read, the answer cites it as `[S1]`.

The model never writes URLs. It only repeats keys it saw in tool results; the TextBlock `sources`
are built here from what the tools actually returned, so a citation cannot be invented
(AGENTS.md: do not present an answer as confirmed when there is no source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import AnyUrl, ValidationError

from botai.contracts.agent_ui import Source

MAX_SOURCES = 10  # TextBlock.sources limit
_CITE = re.compile(r"\[(S\d+(?:\s*[,;]\s*S\d+)*)\]")
_KEY = re.compile(r"S(\d+)")

DATA_KINDS = frozenset({"data", "connection"})
DOCUMENT_KINDS = frozenset({"document", "page"})


@dataclass(frozen=True, slots=True)
class SourceRef:
    key: str
    title: str
    url: str
    kind: str  # data | document | page | connection
    fetched_at: datetime | None
    stale: bool = False


@dataclass(slots=True)
class SourceBook:
    refs: dict[str, SourceRef] = field(default_factory=dict)
    _index: dict[tuple[str, str, str, bool], str] = field(default_factory=dict)

    def add(
        self,
        *,
        title: str,
        url: str,
        kind: str,
        fetched_at: datetime | None,
        stale: bool = False,
    ) -> str:
        """Register a source and return its key (`S1`...). The same source gets the same key."""
        try:
            AnyUrl(url)
        except ValidationError:
            url = f"botai://sources/{len(self.refs) + 1}"  # keeps the numbering honest
        ident = (kind, url, title, stale)
        if ident in self._index:
            return self._index[ident]
        key = f"S{len(self.refs) + 1}"
        self.refs[key] = SourceRef(key, title[:200], url, kind, fetched_at, stale)
        self._index[ident] = key
        return key

    def __len__(self) -> int:
        return len(self.refs)


@dataclass(frozen=True, slots=True)
class Finalized:
    markdown: str
    sources: list[Source]
    cited: list[SourceRef]  # in order of first appearance, matching the `[n]` numbers


def finalize(markdown: str, book: SourceBook, *, now: datetime) -> Finalized:
    """Replace `[S3]` with `[1]` in order of appearance and build the matching `sources`."""
    order: dict[str, int] = {}
    dropped = False

    def number(match: re.Match[str]) -> str:
        numbers: list[str] = []
        for m in _KEY.finditer(match.group(1)):
            key = f"S{m.group(1)}"
            if key not in book.refs:
                continue  # the model made it up: drop it
            n = order.setdefault(key, len(order) + 1)
            numbers.append(str(n))
        nonlocal dropped
        if not numbers:
            dropped = True
        return f"[{', '.join(dict.fromkeys(numbers))}]" if numbers else ""

    text = _CITE.sub(number, markdown)
    if dropped:  # a removed marker must not leave a stray space before punctuation
        text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    cited = [book.refs[k] for k, _ in sorted(order.items(), key=lambda kv: kv[1])]
    sources = [
        Source(
            title=ref.title,
            url=ref.url,  # type: ignore[arg-type]
            fetched_at=ref.fetched_at or now,
            stale=ref.stale,
        )
        for ref in cited[:MAX_SOURCES]
    ]
    return Finalized(text, sources, cited[:MAX_SOURCES])
