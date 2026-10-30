"""Text from universities, documents and pages is data, never instructions (AGENTS.md rule 4).

Three small, deterministic defences used by the harness around every tool result:

* `neutralize_tags` - a page cannot close our `<source_data>` wrapper and speak as the system;
* `mask_injections` - sentences that address the assistant (override the rules, exfiltrate a
  token) are cut out before the model sees them, and reported so that the caller can write the
  `injection_blocked` audit record and tell the student;
* `scrub_secrets` - password/cookie/token shaped strings never reach the model (NFR-004), by
  shape (Bearer, JWT, cookie lines) and by name: the value after a key that names a credential
  (`access_token: …`, `"sesskey":"…"`, `var sesskey = '…'`, `MoodleSession=…`) is hidden
  however short it is, and so is a whole object or list under such a key.

These are defence in depth. The structural guarantees are elsewhere: the chat agent has read-only
tools, no arbitrary outbound address, and a separate public-query gate before search (T5).
"""

from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import dataclass

from botai.core.logging import REDACTED, redact

SOURCE_TAG = "source_data"
MASK = "[…удалён фрагмент, похожий на инструкцию для ассистента…]"
HIDDEN = "[скрыто]"


def neutralize_tags(text: str) -> str:
    """Replace our wrapper tag inside data with an inert look-alike."""
    return re.sub(rf"<(/?)\s*{SOURCE_TAG}", r"‹\1" + SOURCE_TAG, text, flags=re.IGNORECASE)


def wrap_source_data(tool: str, text: str, *, max_chars: int) -> tuple[str, bool]:
    """`<source_data tool="…">…</source_data>`, truncated to `max_chars`. Returns (text, truncated)."""
    body = neutralize_tags(text)
    truncated = len(body) > max_chars
    if truncated:
        body = (
            body[:max_chars].rstrip()
            + f"\n[обрезано: показано {max_chars} из {len(body)} знаков; уточните запрос]"
        )
    safe_tool = re.sub(r"[^a-z0-9_]", "", tool.lower())[:40]
    return f'<{SOURCE_TAG} tool="{safe_tool}">\n{body}\n</{SOURCE_TAG}>', truncated


# --- secrets ------------------------------------------------------------------------------------

_Rules = tuple[tuple[re.Pattern[str], str], ...]

# Structural secrets: safe to mask anywhere, including documents and tool results.
_STRUCTURAL_RULES: _Rules = (
    # cookie / authorization header lines
    (re.compile(r"(?im)^\s*(?:set-)?cookie\s*:.*$"), HIDDEN),
    (re.compile(r"(?im)^\s*authorization\s*:.*$"), HIDDEN),
    # credential-looking key=value pairs
    (
        re.compile(
            r"(?i)\b(sessionid|session_id|sid|csrf\w*|access_token|refresh_token|id_token|token|"
            r"api_key|apikey|secret|sesskey|phpsessid|jsessionid)=[^\s&;\"']+"
        ),
        rf"\1={HIDDEN}",
    ),
)

# Natural-language secrets: only for text the student typed (a document may legitimately say
# "the password can be changed in the profile").
_PASSWORD_RULE = (
    re.compile(r"(?i)\b(парол\w*|password|passwd|pwd)\b\s*[:=—-]?\s*\S+"),
    rf"\1: {HIDDEN}",
)
_CODE_RULE = (
    re.compile(r"(?i)\b(код\w*|code|otp|смс|sms|одноразов\w+|пин|pin)\b[^\n\d]{0,16}\b\d{4,8}\b"),
    rf"\1 {HIDDEN}",
)
# The same without the bare words "код"/"code": "Код группы 2101" is a fact, "код из смс 4242" is not.
_ONE_TIME_RULE = (
    re.compile(r"(?i)\b(otp|смс|sms|одноразов\w+|пин|pin)\b[^\n\d]{0,16}\b\d{4,8}\b"),
    rf"\1 {HIDDEN}",
)

# --- names that mean a credential --------------------------------------------------------------

NOT_SECRET, WEAK, STRONG = 0, 1, 2

_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STRONG_WORDS = frozenset(
    {
        "token", "tokens", "secret", "secrets", "password", "passwords", "passwd", "passphrase",
        "passcode", "pwd", "credential", "credentials", "cookie", "cookies", "bearer", "jwt",
        "csrf", "xsrf", "sesskey", "sessid", "sessionid", "sessionkey", "phpsessid", "jsessionid",
        "sid", "otp", "pin", "authorization", "apikey", "signature",
    }
)  # fmt: skip
_STRONG_PAIRS = frozenset(
    {
        ("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"),
        ("session", "id"), ("session", "key"), ("session", "token"), ("auth", "key"),
    }
)  # fmt: skip
_WEAK_WORDS = frozenset({"session", "sessions", "sess", "key", "auth"})


def key_class(name: str) -> int:
    """Whether a field name means a credential: STRONG (its whole value is hidden, an object or a
    list too), WEAK (a plain value is hidden: `session`, `sortKey`), or NOT_SECRET. Whole words
    only: `Tokenization` and `authors` are not credentials, `access_token` and `userSid` are."""
    words = [w.lower() for w in _WORD.findall(name)]
    if not words:
        return NOT_SECRET
    if any(w in _STRONG_WORDS for w in words) or any(
        pair in _STRONG_PAIRS for pair in itertools.pairwise(words)
    ):
        return STRONG
    if words[-1] == "auth":
        return STRONG
    return WEAK if any(w in _WEAK_WORDS for w in words) else NOT_SECRET


_KEY_VALUE = re.compile(
    r"(?<![\w.\-])(?P<kq>[\"']?)(?P<key>[A-Za-z_][\w.\-]{0,63})(?P=kq)(?P<sep>[ \t]*[:=][ \t]*)"
)
_BARE_VALUE = re.compile(
    r"(?:(?:Bearer|Basic|Token)[ \t]+)?(?:\[скрыто\]|[^\s,;&<>\"'`)\]}]+)", re.IGNORECASE
)
# `{"name": "csrf_token", "value": "…"}` and `name="csrf" value="…"`: the name sits beside the value.
_NAMED_PAIR = re.compile(
    r"(?P<head>\b(?:name|key|field|header|param)[\"']?[ \t]*[:=][ \t]*[\"'](?P<name>[\w.\-]{1,64})[\"']"
    r"[ \t,;]*[\"']?(?:value|val|content)[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>\[скрыто\]|\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
    re.IGNORECASE,
)
_MAX_NESTED = 4_000


def _value_end(text: str, start: int) -> int:
    """Where the value that begins at `start` ends: a quoted string, a balanced object or list, or
    a bare word. `start` itself when there is no value."""
    if start >= len(text):
        return start
    if text.startswith(HIDDEN, start):
        return start + len(HIDDEN)
    ch = text[start]
    if ch in "\"'":
        i = start + 1
        while i < len(text) and text[i] != ch and text[i] != "\n":
            i += 2 if text[i] == "\\" else 1
        return min(i + 1, len(text)) if i < len(text) and text[i] == ch else i
    if ch in "{[":
        depth, quote, i = 0, "", start
        limit = min(len(text), start + _MAX_NESTED)
        while i < limit:
            c = text[i]
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        end = text.find("\n", start)
        return len(text) if end < 0 else end  # not closed: the rest of the line
    bare = _BARE_VALUE.match(text, start)
    return bare.end() if bare else start


def _mask_key_values(text: str) -> str:
    """`key: value`, `key = value`, `"key":"value"` for a key that names a credential."""
    if not any(sep in text for sep in ":="):
        return text
    out: list[str] = []
    last = 0
    for m in _KEY_VALUE.finditer(text):
        if m.start() < last:
            continue  # inside a value that is already hidden
        cls = key_class(m.group("key"))
        if cls == NOT_SECRET:
            continue
        sep = m.group("sep")
        # Prose says "Session: Lecture"; code and cookies say `session=…` or `"session":"…"`.
        if cls == WEAK and "=" not in sep and not m.group("kq"):
            continue
        end = _value_end(text, m.end())
        if end == m.end():
            continue
        out.append(text[last : m.end()])
        out.append(HIDDEN)
        last = end
    out.append(text[last:])
    return "".join(out)


def _mask_named_pairs(text: str) -> str:
    def hide(m: re.Match[str]) -> str:
        return m.group("head") + HIDDEN if key_class(m.group("name")) else m.group(0)

    return _NAMED_PAIR.sub(hide, text)


def scrub_secrets(text: str, *, typed_by_user: bool = False, codes: bool = True) -> str:
    """Mask password/cookie/token shaped strings (Bearer, JWT, cookie lines, `token=…`, the value of
    a credential-named field).

    With `typed_by_user` also "пароль: …" and one-time codes, which a student may paste by mistake;
    `codes=False` leaves the bare words "код"/"code" alone ("Код группы 2101").
    """
    out = redact(text)  # Bearer … and JWTs
    assert isinstance(out, str)
    rules = list(_STRUCTURAL_RULES)
    if typed_by_user:
        rules.append(_PASSWORD_RULE)
        rules.append(_CODE_RULE if codes else _ONE_TIME_RULE)
    for pattern, replacement in rules:
        out = pattern.sub(replacement, out)
    out = out.replace(REDACTED, HIDDEN)
    return _mask_named_pairs(_mask_key_values(out))


# --- injections ---------------------------------------------------------------------------------

_I = re.IGNORECASE | re.UNICODE
INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "override": re.compile(
        r"(игнорир\w+|забудь\w*|отмени\w*|не\s+учитывай\w*|ignore|disregard|forget|override)\s+"
        r"(?:(?:все|любые|свои|your|all|any)\s+)?"
        r"(?:(?:предыдущ\w+|прежн\w+|прошл\w+|выше\w*|системн\w+|эти|previous|prior|above|earlier|system)\s+)?"
        r"(инструкци\w+|указани\w+|правил\w+|команд\w+|промпт\w*|instructions?|rules|prompts?)",
        _I,
    ),
    "role_change": re.compile(
        r"(ты\s+теперь|с\s+этого\s+момента\s+ты|you\s+are\s+now|притворись|новые\s+инструкции|"
        r"new\s+instructions?|system\s+(?:prompt|message)|системн\w+\s+(?:промпт|сообщени\w+|инструкци\w+))",
        _I,
    ),
    "markers": re.compile(
        r"(<\|im_(?:start|end)\|>|\[/?INST\]|<<SYS>>|^\s*(?:system|assistant)\s*:|</?\s*source_data)",
        _I | re.MULTILINE,
    ),
    "exfiltration": re.compile(
        r"(отправ\w+|перешл\w+|передай\w*|пришл\w+|скин\w+|выгруз\w+|раскро\w+|send|forward|post|upload|leak|"
        r"reveal|exfiltrate)\b[^\n]{0,80}?"
        r"(токен\w*|token|cookie|куки|парол\w+|password|секрет\w*|secret|api[\s_-]?key|"
        r"ключ\w*\s+доступ\w*|сессионн\w+|credentials|учётн\w+\s+данн\w+)",
        _I,
    ),
    "concealment": re.compile(
        r"(не\s+(?:говори|сообщай|показывай)\w*\s+(?:это\s+)?(?:пользовател\w+|студент\w+)|"
        r"don'?t\s+(?:tell|show|mention)\s+(?:this\s+)?(?:to\s+)?the\s+user)",
        _I,
    ),
    "addresses_assistant": re.compile(
        r"(?:^|[\s(])(?:ассистент|нейросет\w+|языков\w+\s+модел\w+|llm|assistant)[,:]\s*"
        r"(?:ты|вы|you|please|пожалуйста|немедленно|обязательно)",
        _I | re.MULTILINE,
    ),
}


@dataclass(frozen=True, slots=True)
class InjectionFinding:
    pattern: str
    digest: str  # sha256 prefix of the removed sentence: enough to correlate, nothing to leak


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen [start, end) to the surrounding sentence (a dot inside `evil.test` is not an end)."""
    lo = 0
    for m in re.finditer(r"[!?\n]|\.(?=\s)", text[:start]):
        lo = m.end()
    m2 = re.search(r"[!?\n]|\.(?=\s|$)", text[end:])
    hi = end + m2.end() if m2 else len(text)
    return max(lo, start - 400), min(hi, end + 400)


def mask_injections(text: str) -> tuple[str, list[InjectionFinding]]:
    """Cut instruction-like sentences out of `text`. Returns (clean text, findings)."""
    spans: list[tuple[int, int, str]] = []
    for name, pattern in INJECTION_PATTERNS.items():
        for m in pattern.finditer(text):
            lo, hi = _sentence_bounds(text, m.start(), m.end())
            spans.append((lo, hi, name))
    if not spans:
        return text, []
    spans.sort()
    merged: list[tuple[int, int, set[str]]] = []
    for lo, hi, name in spans:
        if merged and lo <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], hi), prev[2] | {name})
        else:
            merged.append((lo, hi, {name}))
    findings: list[InjectionFinding] = []
    clean = text
    for lo, hi, names in reversed(merged):
        digest = hashlib.sha256(text[lo:hi].encode()).hexdigest()[:12]
        findings.extend(InjectionFinding(name, digest) for name in sorted(names))
        clean = clean[:lo] + MASK + clean[hi:]
    findings.reverse()
    return clean, findings
