"""Links to documents that anyone with the link can open: Google Docs, Sheets, Slides and Drive.

The link a student pastes is a page of an editor; the content is behind an export address. This is
a service for any link, not for a university: the link is turned into the public export (xlsx or
csv for sheets, docx for documents, pdf for slides, the file itself for Drive), and a document
that is not shared by link ends in an honest message instead of an empty result.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from botai.core.errors import BotaiError, ErrorCode

GOOGLE_DOCS = "docs.google.com"
GOOGLE_DRIVE = "drive.google.com"
GOOGLE_HOSTS = frozenset({GOOGLE_DOCS, GOOGLE_DRIVE, "drive.usercontent.google.com"})
# A document that is not shared by link sends the visitor here.
SIGN_IN_HOSTS = frozenset({"accounts.google.com", "accounts.youtube.com"})
# Exports are served from a host named after the file, so it cannot be listed in advance.
_CONTENT_HOST = re.compile(
    r"^doc-[a-z0-9]+-[a-z0-9]+-(?:docs|sheets|slides)\.googleusercontent\.com$"
)
PRIVATE_MESSAGE = "Документ закрыт: нужен публичный доступ по ссылке (Все, у кого есть ссылка)"

_EDITOR = re.compile(r"^/(document|spreadsheets|presentation)/d/(e/)?([A-Za-z0-9_-]{10,})")
_DRIVE_FILE = re.compile(r"^/file/d/([A-Za-z0-9_-]{10,})")
_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_NOT_GOOGLE = "Это не похоже на ссылку на документ Google"


def is_google(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in GOOGLE_HOSTS


def _gid(url: str) -> str | None:
    parts = urlsplit(url)
    for source in (parts.query, parts.fragment):
        values = parse_qs(source).get("gid") or []
        if values and values[0].isdigit():
            return values[0]
    return None


def export_url(url: str) -> str:
    """The address that serves the content of a Google link as a file; any other link as it is."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host == GOOGLE_DRIVE:
        drive_id = _drive_id(parts.path, parts.query)
        if not drive_id:
            raise BotaiError(
                ErrorCode.VALIDATION,
                "Нужна ссылка на файл Google Диска, а не на папку или раздел",
            )
        return f"https://{GOOGLE_DRIVE}/uc?export=download&id={drive_id}"
    if host != GOOGLE_DOCS:
        return url.strip()
    match = _EDITOR.match(parts.path)
    if not match:
        raise BotaiError(ErrorCode.VALIDATION, _NOT_GOOGLE)
    editor, published, doc_id = match.group(1), match.group(2), match.group(3)
    base = f"https://{GOOGLE_DOCS}/{editor}/d/{'e/' if published else ''}{doc_id}"
    gid = _gid(url)
    if editor == "spreadsheets":
        if published:
            return (
                f"{base}/pub?output=csv&gid={gid}&single=true" if gid else f"{base}/pub?output=xlsx"
            )
        return f"{base}/export?format=csv&gid={gid}" if gid else f"{base}/export?format=xlsx"
    if editor == "presentation":
        if published:
            raise BotaiError(ErrorCode.VALIDATION, _NOT_GOOGLE)
        return f"{base}/export/pdf"
    return f"{base}/pub" if published else f"{base}/export?format=docx"


def _drive_id(path: str, query: str) -> str | None:
    match = _DRIVE_FILE.match(path)
    if match:
        return match.group(1)
    if path in {"/open", "/uc"}:
        values = parse_qs(query).get("id") or []
        if values and _ID.match(values[0]):
            return values[0]
    return None


def hop_allowed(url: str) -> bool:
    """May a redirect of a Google export lead here? Google hosts and its file hosts only."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host in GOOGLE_HOSTS or bool(_CONTENT_HOST.match(host)))


def is_sign_in(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in SIGN_IN_HOSTS
