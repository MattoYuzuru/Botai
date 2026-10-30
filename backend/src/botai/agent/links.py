"""Links inside the app (`botai://…`) and the rule that bare identifiers never reach the student."""

from __future__ import annotations

import re

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
APP_LINK = re.compile(rf"botai://(?:threads|connections|documents|attachments)/{ULID}")
_BARE_ID = re.compile(rf"(?<![\w/]){ULID}(?!\w)")
_ID_LABEL = re.compile(r"\b[a-z]+_id\s*[=:]\s*(?=$|[\s,.;:)\]])", re.IGNORECASE | re.MULTILINE)
_EMPTY_BRACKETS = re.compile(r"\s*[(\[]\s*[)\]]")


def strip_bare_ids(text: str) -> str:
    """Remove ULIDs the model copied out of tool results into the answer. Links to threads,
    connections, documents and attachments keep their id: that is what the client resolves."""
    kept: list[str] = []

    def hold(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    protected = APP_LINK.sub(hold, text)
    if not _BARE_ID.search(protected):
        return text
    cleaned = _ID_LABEL.sub("", _BARE_ID.sub("", protected))
    cleaned = _EMPTY_BRACKETS.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], cleaned)
