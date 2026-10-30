"""Dictation endpoint (docs/development/speech-to-text.md): a recording in, plain text out."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from fastapi.routing import APIRoute

from botai.auth.deps import AuthUser
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.settings import get_settings
from botai.speech.limits import MAX_AUDIO_BYTES
from botai.speech.schemas import TranscriptOut
from botai.speech.service import UNAVAILABLE, SpeechService, build_speech_service


@lru_cache
def _service() -> SpeechService:
    return build_speech_service(get_settings())


def get_speech() -> SpeechService:
    """The process-wide dictation service: its quota lives as long as the process."""
    return _service()


SpeechDep = Annotated[SpeechService, Depends(get_speech)]


class DictationRoute(APIRoute):
    """Answers 503 before FastAPI reads the multipart body when dictation is off.

    A dependency cannot do this: the framework parses a form body before it resolves dependencies,
    so a switched-off service would still take a recording of any size first."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            service = request.app.dependency_overrides.get(get_speech, get_speech)()
            if not service.available:
                diagnostic_event("speech.refused", outcome="refused", reason="disabled")
                raise BotaiError(ErrorCode.STT_UNAVAILABLE, UNAVAILABLE)
            return await handler(request)

        return guarded


router = APIRouter(prefix="/api/v1/speech", tags=["speech"], route_class=DictationRoute)


@router.post("/transcribe", response_model=TranscriptOut)
async def transcribe(
    audio: UploadFile,
    user: AuthUser,
    speech: SpeechDep,
    language: Annotated[str | None, Form(max_length=8)] = None,
    duration_ms: Annotated[int | None, Form(alias="durationMs", ge=0, le=86_400_000)] = None,
) -> TranscriptOut:
    """A recording up to 5 MiB and three minutes → the recognised text. The recording is kept
    nowhere: not in the database, not in a file, not in a log. `language` is an ISO 639-1 code
    (Russian by default); `durationMs` is what the page measured: the daily limit is charged by
    the length the provider measures, so a false claim does not help."""
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    text = await speech.transcribe(
        user.id, data, language=language or None, duration_ms=duration_ms
    )
    return TranscriptOut(text=text)
