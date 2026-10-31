"""What makes two v4 sources different inside one space.

`identity_key` is never an origin: it is a normalized address with the path (`/schedule` and
`/tasks` of one cabinet are one source, a second cabinet at another path is another), a hashed
private link (the link itself is a secret and is not stored here), a file digest or a mailbox.
Query parameters whose *names* say they are secrets (`token`, `key`, `session`, `csrf`, `code`,
`state`, `signature`, ...) are dropped before keying; every other pair is kept and sorted, so
`?view=week&group=1` and `?group=1&view=week` are the same record, a long non-secret id (a course
id, a group filter) still identifies its source, and a token never changes identity.

The private calendar link must go to the Vault, not to the database: `calendar_key` hashes the
normalized link and only the digest reaches `sources.identity_key`.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from botai.core.errors import BotaiError, ErrorCode

MAX_IDENTITY = 400  # matches sources.identity_key; longer addresses are rejected, not truncated

# Web calendars hand out `webcal://` links; for identity they are the same resource over https.
_SCHEMES = {"http": "http", "https": "https", "webcal": "https", "webcals": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Dropped from a key: the same page may carry a fresh session token on every visit.
_SECRET_NAMES = frozenset(
    {
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "key",
        "secret",
        "clientsecret",
        "password",
        "passwd",
        "pwd",
        "signature",
        "sig",
        "auth",
        "authorization",
        "session",
        "sessionid",
        "sid",
        "sesskey",
        "csrf",
        "csrftoken",
        "xsrf",
        "xsrftoken",
        "ticket",
        "nonce",
        "jwt",
        "bearer",
        "otp",
        "pin",
        "code",
        "state",
    }
)
_SECRET_SUFFIXES = (
    "token",
    "secret",
    "password",
    "signature",
    "session",
    "apikey",
    "sesskey",
    "csrf",
    "ticket",
)
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_MAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _fail(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _flatten(name: str) -> str:
    """`access_token` and `Access-Token` are the same key; separators do not hide a secret name."""
    return "".join(character for character in name.casefold() if character.isalnum())


def _secret_name(name: str) -> bool:
    flat = _flatten(name)
    return flat in _SECRET_NAMES or flat.endswith(_SECRET_SUFFIXES)


def normalize_url(raw: str) -> str:
    """A comparable address: scheme/host lowercased, IDN in punycode, no fragment, no secret query.

    A query pair is dropped only when its name names a secret (see `_SECRET_NAMES`/suffixes); a
    long id is data, not a secret, and stays in the identity.

    Raises `BotaiError(VALIDATION)` for anything that cannot identify a source, including an address
    that carries a login or password (that belongs in the Vault, not in a key).
    """
    text = raw.strip()
    if not text:
        raise _fail("Адрес пуст")
    parts = urlsplit(text)
    scheme = parts.scheme.casefold()
    if scheme not in _SCHEMES:
        raise _fail("Ссылка должна начинаться с http(s):// или webcal://")
    if parts.username is not None or parts.password is not None:
        raise _fail("В адресе есть логин или пароль: их место в защищённом поле, а не в ссылке")
    host = (parts.hostname or "").strip(".").casefold()
    if not host:
        raise _fail("В ссылке нет имени сайта")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise _fail("Имя сайта не распознано") from None
    try:
        port = parts.port
    except ValueError:
        raise _fail("В ссылке неверный порт") from None
    normalized_scheme = _SCHEMES[scheme]
    default_port = _DEFAULT_PORTS[normalized_scheme]
    netloc = host if port is None or port == default_port else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path)
    path = path.rstrip("/")
    query = _normalized_query(parts.query)
    address = f"{normalized_scheme}://{netloc}{path}"
    if query:
        address = f"{address}?{query}"
    if len(address) > MAX_IDENTITY:
        raise _fail("Слишком длинная ссылка: сократите её до адреса нужного раздела")
    return address


def _normalized_query(query: str) -> str:
    pairs = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not _secret_name(name)
    ]
    pairs.sort()
    return urlencode(pairs)


def site_key(entry_url: str) -> str:
    """The identity of a site/portal/LMS/API source: its normalized address."""
    return normalize_url(entry_url)


def calendar_key(link: str) -> str:
    """The identity of a calendar connected by a private export link: a digest, never the link."""
    normalized = normalize_url(link)
    return f"link:{hashlib.sha256(normalized.encode()).hexdigest()}"


def file_key(sha256: str) -> str:
    """The identity of an uploaded file: the digest of its bytes."""
    digest = sha256.strip().casefold()
    if not _HEX64.match(digest):
        raise _fail("Отпечаток файла должен быть sha256 в шестнадцатеричном виде")
    return f"file:{digest}"


def mail_key(mailbox: str) -> str:
    """The identity of a mailbox."""
    address = mailbox.strip().casefold()
    if not _MAIL.match(address):
        raise _fail("Почтовый ящик должен выглядеть как name@example.org")
    if len(address) + len("mail:") > MAX_IDENTITY:
        raise _fail("Слишком длинный адрес ящика")
    return f"mail:{address}"


def validate_identity(key: str) -> str:
    """Check a key the caller built itself (the agent may store a known one)."""
    value = key.strip()
    if not value:
        raise _fail("Пустой identity источника")
    if len(value) > MAX_IDENTITY:
        raise _fail("Слишком длинный identity источника")
    prefixes = ("http://", "https://", "link:", "file:", "mail:", "draft:")
    if not value.startswith(prefixes):
        raise _fail("identity источника должен быть адресом, link:, file:, mail: или draft:")
    return value


def provisional_key(source_id: str) -> str:
    """A draft source may have no address yet (the agent is still asking); it gets its own slot."""
    return f"draft:{source_id}"


def is_provisional(key: str) -> bool:
    return key.startswith("draft:")


def digest_sha256(data: bytes) -> str:
    """Content address for artifacts and uploaded files."""
    return hashlib.sha256(data).hexdigest()
