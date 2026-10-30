"""The seam between dictation and a speech-to-text service.

Any provider implements `SpeechToText`. It receives the bytes of one recording and returns the
words. It sees nothing else of the student: no id, name, chat or university.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

FailureReason = Literal[
    "unavailable",  # the service did not answer or failed twice: try later
    "rate_limited",  # the service asked us to slow down
    "rejected",  # the service could not read this recording
    "misconfigured",  # no key, a wrong key, a closed account: an operator has to act
]


class SpeechFailure(Exception):
    """A provider could not transcribe. `retry_after_s` is the provider's own hint, if any.

    `refundable` is true only when the provider plainly refused (a 4xx, a 429) or nothing was sent:
    a timeout, a dropped connection or a server error may still have been processed, and the
    seconds of audio stay on the user's account.
    """

    def __init__(
        self,
        reason: FailureReason,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        refundable: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason: FailureReason = reason
        self.status = status
        self.retry_after_s = retry_after_s
        self.refundable = refundable


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    # The length of the audio as the service measured it; None when it did not say. The quota is
    # charged by this, not by what the client claimed.
    duration_s: float | None = None


class SpeechToText(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, mime: str, language: str | None) -> Transcription:
        """The recognised text (empty when nothing was said) and the length of the audio, if the
        service measured it. Raises `SpeechFailure`.

        `mime` is the container found in the bytes (`audio/webm`, `audio/ogg`, `audio/mp4`, ...),
        `language` an ISO 639-1 code or None to let the service detect it."""
        ...
