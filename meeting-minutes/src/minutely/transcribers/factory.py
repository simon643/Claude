"""Pick a transcriber by name."""

from __future__ import annotations

from ..config import Settings
from . import Transcriber, TranscriptionError
from .whisper import WhisperTranscriber

TRANSCRIBERS = ("whisper", "none")


class NullTranscriber:
    """Refuses politely. Used when the user supplies transcripts by hand."""

    name = "none"

    def available(self) -> bool:
        return False

    def transcribe(self, audio: object, *, language: str = "") -> object:
        raise TranscriptionError(
            "transcription is disabled (transcriber = none). "
            "Import a transcript instead: minutely import meeting.vtt --meeting <id>"
        )


def get_transcriber(name: str | None = None, settings: Settings | None = None) -> Transcriber:
    settings = settings or Settings.load()
    chosen = (name or settings.transcriber or "whisper").strip().lower()
    if chosen in {"none", "off", "manual"}:
        return NullTranscriber()  # type: ignore[return-value]
    if chosen == "whisper":
        return WhisperTranscriber(
            binary=settings.whisper_bin,
            model=settings.whisper_model,
            language=settings.language,
        )
    raise TranscriptionError(f"unknown transcriber {chosen!r} (choose from: {', '.join(TRANSCRIBERS)})")
