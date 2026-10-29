"""What a chat file is, decided from its bytes (never from the name or the type the client sent).

Images and audio are stored and shown; documents are read (`sources/documents.py`). Anything
else is refused when it is uploaded, so nothing unreadable is kept. The type a file is served
with is chosen here too: a page of HTML or an unknown text is never served as something a
browser would run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from botai.core.errors import BotaiError, ErrorCode
from botai.sources.documents import DocFormat, detect_format

AttachmentKind = Literal["image", "document", "audio", "other"]

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCUMENT_MIME: dict[DocFormat, str] = {
    "pdf": "application/pdf",
    "docx": _DOCX,
    "xlsx": _XLSX,
    "csv": "text/csv",
    "html": "text/html",
    "text": "text/plain",
}
SHEET_FORMATS: frozenset[DocFormat] = frozenset({"xlsx", "csv"})
_AUDIO_BRANDS = frozenset({b"M4A ", b"M4B ", b"M4P ", b"mp41", b"mp42", b"isom", b"iso2", b"dash"})
# The mimes a file may be served with as itself and shown in the page; the rest is a download.
_INLINE = frozenset({"application/pdf", "text/plain", "text/csv", "text/markdown"})


@dataclass(frozen=True, slots=True)
class Detected:
    kind: AttachmentKind
    mime: str
    format: DocFormat | None = None  # set for documents


def _is_utf16(data: bytes) -> bool:
    """A text with a byte order mark: what Excel's "Unicode text" export writes."""
    return data[:2] in (b"\xff\xfe", b"\xfe\xff")


def _starts(data: bytes, *prefixes: bytes) -> bool:
    return any(data.startswith(p) for p in prefixes)


def _media(data: bytes) -> tuple[AttachmentKind, str] | None:
    """Image or audio by the first bytes, else None."""
    if _starts(data, b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if _starts(data, b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if _starts(data, b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if _starts(data, b"OggS"):
        return "audio", "audio/ogg"
    if _starts(data, b"\x1a\x45\xdf\xa3"):  # EBML: what MediaRecorder writes
        return "audio", "audio/webm"
    if data[4:8] == b"ftyp" and data[8:12] in _AUDIO_BRANDS:  # not HEIC/AVIF pictures
        return "audio", "audio/mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if _starts(data, b"ID3") or _mpeg_frame(data):
        return "audio", "audio/mpeg"
    return None


def _mpeg_frame(data: bytes) -> bool:
    """A valid header of an MPEG audio layer III frame (an mp3 without an ID3 tag).

    The sync bits alone are not enough: a UTF-16 text starts with the byte order mark FF FE,
    which looks like a sync. The version, layer, bitrate and sample rate must be real ones.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
        return False
    version, layer = (data[1] >> 3) & 3, (data[1] >> 1) & 3
    bitrate, rate = data[2] >> 4, (data[2] >> 2) & 3
    return version != 1 and layer == 1 and bitrate not in (0, 15) and rate != 3


def detect(data: bytes, filename: str | None, content_type: str | None) -> Detected:
    """Raises a validation error for an empty or unsupported file."""
    if not data:
        raise BotaiError(ErrorCode.VALIDATION, "Файл пустой")
    declared = (content_type or "").split(";")[0].strip().lower()
    media = None if _is_utf16(data) else _media(data)
    if media is not None:
        kind, mime = media
        if declared.startswith("video/"):  # the same containers carry video, which we do not play
            raise BotaiError(ErrorCode.VALIDATION, "Видео пока не поддерживается")
        return Detected(kind, mime)
    fmt = detect_format(data, filename, content_type)
    mime = DOCUMENT_MIME[fmt]
    lower = (filename or "").lower()
    if fmt == "csv" and lower.endswith(".tsv"):
        mime = "text/tab-separated-values"
    elif fmt == "text" and lower.endswith((".md", ".markdown")):
        mime = "text/markdown"
    return Detected("document", mime, fmt)


def served_as(name: str, mime: str) -> tuple[str, str]:
    """`(Content-Type, Content-Disposition)` for the stored bytes.

    HTML and anything a browser could interpret is served as plain text and as a download, so a
    file of a student can never run in the origin of the application.
    """
    if mime in _INLINE or mime.startswith(("image/", "audio/")):
        content_type = f"{mime}; charset=utf-8" if mime.startswith("text/") else mime
        return content_type, _disposition("inline", name)
    if mime == "text/html" or mime.startswith("text/"):
        return "text/plain; charset=utf-8", _disposition("attachment", name)
    return mime, _disposition("attachment", name)


def _disposition(kind: str, name: str) -> str:
    ascii_name = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name
    )
    return f"{kind}; filename=\"{ascii_name or 'file'}\"; filename*=UTF-8''{quote(name, safe='')}"


def clean_filename(name: str | None) -> str:
    """The leaf of a path, printable, at most 200 characters."""
    leaf = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in leaf if ch.isprintable()).strip()[:200]
