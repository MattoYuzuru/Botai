"""Dictation: speech to text behind a replaceable provider (Groq today)."""

from botai.speech.provider import SpeechFailure, SpeechToText, Transcription
from botai.speech.service import SpeechService

__all__ = ["SpeechFailure", "SpeechService", "SpeechToText", "Transcription"]
