"""Redaction of what a university showed before any of it reaches a prompt (security.md L5).

The model must see the *structure* of pages and responses, not the student: names, e-mails,
phones, identifiers and tokens are replaced by typed placeholders, long lists are cut, strings are
truncated. The same identifier gets the same placeholder everywhere (`<v1>` in a URL and in a
JSON value), so the model can tell that a path parameter comes from another response and write a
`bootstrap` step instead of copying the value.

Redaction is best effort for free text; the hard guarantee that a shared spec holds no personal
value is the lint (`learn/lint.py`), which also receives the raw observed values collected here.
Text that tries to instruct the model is dropped and counted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.sources.extract import clean_text, collapse

MAX_STRING = 120
MAX_ITEMS = 3
MAX_DEPTH = 8
MAX_HTML = 6000
MAX_SIMILAR = 3

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(
    r"(?<!\d)(?:\+7|8|7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)|\+\d{10,15}"
)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[ -]\d{2}\b")
LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}\b")
LONG_DIGITS = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")
# "Фамилия И. О." and "Фамилия Имя Отчество" (a patronymic ending gives the latter away)
FIO_INITIALS = re.compile(r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")
FIO_FULL = re.compile(
    r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ович|евич|ьич|ична|овна|евна|инична|ич|ыч)\b"
)
PERSONAL_KEYS = re.compile(
    r"(?i)e-?mail|phone|mobile|\btel\b|passport|snils|\binn\b|birth|login|user_?name|"
    r"full_?name|first_?name|last_?name|sur_?name|patronymic|middle_?name|\bfio\b|address|"
    r"photo|avatar|password|token|secret|cookie|csrf|session"
)
# A value that names a record rather than describes it: has a digit, no spaces, not a date/number.
ID_LIKE = re.compile(r"^(?=\S{3,64}$)(?=.*\d)(?![\d:.,+\-TZ/ ]+$)[\w@.~-]+$")
ID_KEY = re.compile(r"(?i)(?:^|[_.-])(?:id|uid|uuid|guid|sub)$|[a-z]Id$")
ID_SEGMENT = re.compile(r"^(?=.*\d)(?!v\d+$)(?!\d{1,3}$)[\w@.~%-]{2,}$")
UNIX_RANGE = (946_684_800, 4_102_444_800)

INJECTION = re.compile(
    r"(?i)ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)|disregard\s+(?:the\s+)?"
    r"(?:previous|above|instructions)|you\s+are\s+now|new\s+instructions|system\s+prompt|"
    r"(?:send|post|forward|email|upload)\b.{0,60}\b(?:token|cookie|password|credential|session)|"
    r"игнорируй\s+(?:все\s+|любые\s+)?(?:предыдущ|прежн|выше)|забудь\s+(?:все\s+)?(?:предыдущ|инструкц)|"
    r"(?:отправь|перешли|передай|пришли)\b.{0,60}\b(?:токен|cookie|куки|парол|сессию)|"
    r"(?:новые|твои)\s+инструкции|системн\w+\s+промпт"
)


def looks_like_injection(text: str) -> bool:
    return INJECTION.search(text) is not None


@dataclass(slots=True)
class Redactor:
    """Per-observation state: known personal values, stable pseudonyms, what was dropped."""

    known: set[str] = field(default_factory=set, repr=False)  # exact personal strings
    pseudonyms: dict[str, str] = field(default_factory=dict, repr=False)
    injections: int = 0
    observed: set[str] = field(default_factory=set, repr=False)  # raw values, for the lint

    # --- setup -------------------------------------------------------------------------------

    def know(self, *values: str | None) -> None:
        for value in values:
            if not value:
                continue
            clean = collapse(value)
            if len(clean) >= 3:
                self.known.add(clean)
                self.observed.add(clean)
            for part in re.split(r"[\s,;]+", clean):
                if len(part) >= 3 and part.isalpha():
                    self.known.add(part)
                    self.observed.add(part)

    def pseudonym(self, raw: str) -> str:
        self.observed.add(raw)
        return self.pseudonyms.setdefault(raw, f"<v{len(self.pseudonyms) + 1}>")

    # --- text --------------------------------------------------------------------------------

    def text(self, value: str, limit: int = MAX_STRING) -> str | None:
        """Redacted text, or None if it is an attempt to instruct the model."""
        value = clean_text(collapse(value), 4000)
        if not value:
            return ""
        if looks_like_injection(value):
            self.injections += 1
            return None
        if len(value) <= 200:
            self.observed.add(value)
        value = self._scrub(value)
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def _scrub(self, value: str) -> str:
        for known in sorted(self.known, key=len, reverse=True):
            if known in value:
                self.observed.add(known)
                value = value.replace(known, "<student>")
            elif known.casefold() in value.casefold():
                value = re.sub(re.escape(known), "<student>", value, flags=re.IGNORECASE)
        for raw in sorted(self.pseudonyms, key=len, reverse=True):
            if raw in value:
                value = value.replace(raw, self.pseudonyms[raw])
        for label, pattern in (
            ("jwt", JWT),
            ("email", EMAIL),
            ("snils", SNILS),
            ("uuid", UUID),
            ("phone", PHONE),
            ("person", FIO_FULL),
            ("person", FIO_INITIALS),
        ):
            for match in pattern.findall(value):
                self.observed.add(match)
            value = pattern.sub(f"<{label}>", value)
        for match in LONG_TOKEN.findall(value):
            self.observed.add(match)
        value = LONG_TOKEN.sub("<token>", value)
        return LONG_DIGITS.sub(self._digits, value)

    def _digits(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        self.observed.add(raw)
        if len(raw) in (10, 13) and UNIX_RANGE[0] <= int(raw[:10]) <= UNIX_RANGE[1]:
            return raw  # a timestamp: the model needs its format, it identifies nobody
        return "<number>"

    # --- URLs --------------------------------------------------------------------------------

    def url(self, url: str, origin: str) -> str:
        """Path and query of a same-origin URL with ids pseudonymised; other hosts keep the host."""
        parts = urlsplit(url)
        same = f"{parts.scheme}://{parts.netloc}" == origin
        segments = []
        for seg in parts.path.split("/"):
            if seg and ID_SEGMENT.match(seg):
                segments.append(self.pseudonym(seg))
            else:
                segments.append(seg)
        path = "/".join(segments) or "/"
        query = ""
        if parts.query:
            pairs = []
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                self.observed.add(value)
                shown = self._query_value(key, value)
                pairs.append(f"{key}={shown}")
            query = "?" + "&".join(pairs)
        return (path if same else f"{parts.scheme}://{parts.hostname}{path}") + query

    def _query_value(self, key: str, value: str) -> str:
        if PERSONAL_KEYS.search(key):
            return "<personal>"
        if value in self.pseudonyms:
            return self.pseudonyms[value]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ][\d:.+Z-]*)?|\d{1,5}", value):
            return value  # dates and small numbers are how a recipe learns its templates
        return self.text(value, 60) or "<text>"

    # --- JSON --------------------------------------------------------------------------------

    def json(self, node: Any, depth: int = 0, key: str = "") -> Any:
        if depth > MAX_DEPTH:
            return "<deep>"
        if isinstance(node, dict):
            return {
                str(k)[:60]: self.json(v, depth + 1, str(k)) for k, v in list(node.items())[:40]
            }
        if isinstance(node, list):
            kept = [self.json(v, depth + 1, key) for v in node[:MAX_ITEMS]]
            if len(node) > MAX_ITEMS:
                kept.append(f"<+{len(node) - MAX_ITEMS} more>")
            return kept
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, (int, float)):
            if ID_KEY.search(key) and not PERSONAL_KEYS.search(key):
                return self.pseudonym(str(node))
            return self._number(node, key)
        if isinstance(node, str):
            if PERSONAL_KEYS.search(key):
                self.observed.add(node)
                return "<personal>"
            if ID_KEY.search(key) or ID_LIKE.match(node):
                return self.pseudonym(node)
            red = self.text(node)
            return "<instruction removed>" if red is None else red
        return None

    def _number(self, value: int | float, key: str) -> Any:
        self.observed.add(str(value))
        if PERSONAL_KEYS.search(key):
            return "<personal>"
        if isinstance(value, int) and abs(value) >= 100_000:
            raw = str(value)
            if (
                UNIX_RANGE[0] <= value <= UNIX_RANGE[1]
                or UNIX_RANGE[0] * 1000 <= value <= UNIX_RANGE[1] * 1000
            ):
                return value
            return self.pseudonyms.get(raw) or "<number>"
        return value

    # --- HTML --------------------------------------------------------------------------------

    def html(self, html: str, limit: int = MAX_HTML) -> str:
        """A skeleton of the page: tags, classes, ids and hrefs kept; text redacted and cut;
        repeating siblings reduced to a few examples."""
        tree = LexborHTMLParser(html)
        for bad in tree.css("script,style,svg,noscript,iframe,template,head link,head meta"):
            bad.decompose()
        root = tree.body or tree.root
        out: list[str] = []
        if root is not None:
            self._node(root, out, 0)
        text = "".join(out)
        return text if len(text) <= limit else text[:limit] + "…"

    _KEEP_ATTRS = ("id", "class", "href", "datetime", "type", "name", "role", "data-id")

    def _node(self, node: LexborNode, out: list[str], depth: int) -> None:
        if depth > 14:
            return
        tag = node.tag or ""
        if tag in {"-text", "#text"}:
            red = self.text(node.text() or "", 80)
            if red:
                out.append(red)
            elif red is None:
                out.append("<instruction removed>")
            return
        if tag in {"-comment", "#comment", "-undef"}:
            return
        attrs = []
        for name in self._KEEP_ATTRS:
            value = node.attributes.get(name)
            if value is None:
                continue
            if name == "href":
                value = self.url(value, "") if value.startswith(("/", "http")) else value[:40]
            elif name in {"datetime"}:
                value = value[:30]
            elif name == "data-id" or (name == "name" and ID_LIKE.match(value)):
                value = self.pseudonym(value)
            else:
                value = self._scrub(value)[:60]
            attrs.append(f'{name}="{value}"')
        for name, value in node.attributes.items():
            if name.startswith("data-") and name != "data-id" and value:
                shown = self.pseudonym(value) if ID_LIKE.match(value) else self._scrub(value)[:30]
                attrs.append(f'{name}="{shown}"')
        out.append(f"<{tag}{' ' + ' '.join(attrs) if attrs else ''}>")
        seen: dict[str, int] = {}
        dropped = 0
        for child in node.iter(include_text=True):
            ctag = child.tag or ""
            if ctag not in {"-text", "#text"}:
                signature = f"{ctag}.{child.attributes.get('class') or ''}"
                seen[signature] = seen.get(signature, 0) + 1
                if seen[signature] > MAX_SIMILAR:
                    dropped += 1
                    continue
            self._node(child, out, depth + 1)
        if dropped:
            out.append(f"<!-- +{dropped} similar -->")
        out.append(f"</{tag}>")


def collect_values(node: Any) -> Iterable[str]:
    """Every scalar of a JSON document as text (for the lint's observed set)."""
    if isinstance(node, dict):
        for v in node.values():
            yield from collect_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from collect_values(v)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (str, int, float)):
        yield str(node)
