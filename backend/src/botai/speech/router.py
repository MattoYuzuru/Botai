"""Dictation endpoint (docs/development/speech-to-text.md): a recording in, plain text out."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Form, UploadFile

from botai.auth.deps import AuthUser
from botai.settings import get_settings
from botai.speech.schemas import TranscriptOut
from botai.speech.service import MAX_AUDIO_BYTES, SpeechService

router = APIRouter(prefix="/api/v1/speech", tags=["speech"])


@lru_cache
def _service() -> SpeechService:
    return SpeechService(get_settings())


def get_speech() -> SpeechService:
    """The process-wide dictation service: its quota lives as long as the process."""
    return _service()


SpeechDep = Annotated[SpeechService, Depends(get_speech)]


@router.post("/transcribe", response_model=TranscriptOut)
async def transcribe(
    audio: UploadFile,
    user: AuthUser,
    speech: SpeechDep,
    language: Annotated[str | None, Form(max_length=8)] = None,
    duration_ms: Annotated[int | None, Form(alias="durationMs", ge=0, le=86_400_000)] = None,
) -> TranscriptOut:
    """A recording up to 20 MiB and three minutes → the recognised text. The recording is kept
    nowhere: not in the database, not in a file, not in a log. `language` is an ISO 639-1 code
    (Russian by default); `durationMs` is what the page measured, used for the daily limit."""
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    text = await speech.transcribe(
        user.id, data, language=language or None, duration_ms=duration_ms
    )
    return TranscriptOut(text=text)
