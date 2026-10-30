"""Operator smoke: transcribe one short recording through the configured provider and EgressProxy.

Prints the provider, the outcome and the words; the key is never printed. This is a paid call to an
external service: run it by hand on a fixed phrase, not in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from botai.browser.egress import close_egress
from botai.settings import get_settings
from botai.speech.audio import sniff_audio
from botai.speech.config import load_speech_settings
from botai.speech.limits import MAX_AUDIO_BYTES
from botai.speech.provider import SpeechFailure
from botai.speech.service import build_provider


async def smoke(path: Path, language: str) -> int:
    provider = build_provider(get_settings(), load_speech_settings())
    if provider is None:
        print(
            json.dumps({"status": "off", "reason": "no key, STT_PROVIDER=off or invalid settings"})
        )
        return 1
    audio = await asyncio.to_thread(path.read_bytes)
    mime = sniff_audio(audio)
    if mime is None or len(audio) > MAX_AUDIO_BYTES:
        print(json.dumps({"status": "rejected", "reason": "not a recording or too large"}))
        return 1
    try:
        result = await provider.transcribe(audio, mime=mime, language=language)
    except SpeechFailure as failure:
        print(json.dumps({"provider": provider.name, "status": "failed", "reason": failure.reason}))
        return 1
    finally:
        await close_egress()
    print(
        json.dumps(
            {
                "provider": provider.name,
                "status": "ok",
                "chars": len(result.text),
                "seconds": result.duration_s,
                "text": result.text,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="webm, ogg, m4a, wav, mp3 or flac")
    parser.add_argument("--language", default="ru")
    args = parser.parse_args()
    return asyncio.run(smoke(args.file, args.language))


if __name__ == "__main__":
    raise SystemExit(main())
