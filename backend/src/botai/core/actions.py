"""Whether an address names an action rather than a page.

A GET to an address that leaves, joins, confirms, cancels, deletes, votes or marks can change
something on a careless site, so a read-only reader never makes it: the agent's `browse_source`
refuses such an address and every hop of its redirects, and the read-only browser of an agent's
reading refuses it as a navigation or a request of the page.
"""

from __future__ import annotations

import itertools
import re
from urllib.parse import unquote, urlsplit

# What the address itself says it does: leaving, joining, confirming, cancelling, deleting,
# voting, marking. A GET to such an address can change something on a careless site, so it is
# never made. The address (decoded, path and query) is cut into whole words at case changes and at
# `_ - . / & = ?`, so `/api/deleteItem`, `/doLogout`, `/log_out` and `/cancelBooking/5` are caught
# and `/address` or `/exam` are not.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_ACTION_WORDS = frozenset(
    {
        "logout", "signout", "signoff", "logoff", "exit", "quit", "delete", "remove", "destroy",
        "unsubscribe", "subscribe", "enrol", "enroll", "enrolment", "enrollment", "unenrol",
        "unenroll", "join", "leave", "confirm", "accept", "approve", "reject", "decline",
        "submit", "cancel", "revoke", "deactivate", "activate", "disable", "toggle", "mark",
        "vote", "register", "signup", "reset", "terminate", "drop", "withdraw", "send", "pay",
        "purchase", "buy", "publish", "ban", "kick", "purge", "wipe", "sesskey", "act",
        "action", "do",
    }
)  # fmt: skip
_ACTION_PAIRS = frozenset(
    {
        ("log", "out"), ("sign", "out"), ("log", "off"), ("sign", "off"), ("sign", "up"),
        ("opt", "out"), ("check", "out"), ("un", "subscribe"), ("un", "enrol"), ("un", "enroll"),
    }
)  # fmt: skip
# A verb glued to the rest in lower case (`logoutall`, `unsubscribeme`) is still that verb.
_ACTION_PREFIXES = ("logout", "signout", "logoff", "signoff", "unsubscribe", "unenrol")


def _decoded(url: str) -> str:
    parts = urlsplit(url)
    text = f"{parts.path}?{parts.query}"
    if parts.fragment.startswith("/"):  # the route of a hash-routed application is a path too
        text += f"#{parts.fragment}"
    for _ in range(3):  # a double-encoded verb is still a verb
        unquoted = unquote(text)
        if unquoted == text:
            break
        text = unquoted
    return text


def looks_like_action(url: str) -> bool:
    """Whether the address names an action rather than a page (see `_ACTION_WORDS`)."""
    words = [w.lower() for w in _WORD.findall(_decoded(url))]
    return any(w in _ACTION_WORDS or w.startswith(_ACTION_PREFIXES) for w in words) or any(
        pair in _ACTION_PAIRS for pair in itertools.pairwise(words)
    )
