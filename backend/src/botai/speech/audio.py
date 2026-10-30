"""What an uploaded recording is, decided from its bytes (never from the name or type the client sent).

The browser records WebM/Opus (Chrome, Edge), Ogg/Opus (Firefox) or MP4/AAC (Safari). The provider
picks its decoder by the file name, so the name sent upstream is built from the detected
container (`EXTENSIONS`), not taken from the client.
"""

from __future__ import annotations

# Brands of ISO-BMFF files that are pictures or other video, not a recording of a voice.
_NOT_AUDIO_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
)


# mime -> the file extension a provider's decoder is chosen by
EXTENSIONS: dict[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/flac": "flac",
}


def sniff_audio(data: bytes) -> str | None:
    """The mime of the container of a recording by its first bytes, or None for anything else."""
    if data[:4] == b"\x1a\x45\xdf\xa3":  # EBML: what MediaRecorder writes in Chromium
        return "audio/webm"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[4:8] == b"ftyp" and data[8:12] not in _NOT_AUDIO_BRANDS:
        return "audio/mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:3] == b"ID3" or _mpeg_frame(data):
        return "audio/mpeg"
    return None


def _mpeg_frame(data: bytes) -> bool:
    """A valid header of an MPEG audio layer III frame (an mp3 without an ID3 tag)."""
    if len(data) < 4 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
        return False
    version, layer = (data[1] >> 3) & 3, (data[1] >> 1) & 3
    bitrate, rate = data[2] >> 4, (data[2] >> 2) & 3
    return version != 1 and layer == 1 and bitrate not in (0, 15) and rate != 3
