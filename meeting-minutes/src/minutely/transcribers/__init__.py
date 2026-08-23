"""Transcribers: audio in, :class:`Transcript` out.

Speech recognition is the one part of this app that is genuinely hard, so it is
not reimplemented here. The default transcriber drives whatever whisper build
the user already trusts on their machine; the alternative is to bring your own
transcript from Teams, Zoom, Meet, or a typed-up set of notes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Transcript


class TranscriptionError(RuntimeError):
    """Raised when audio cannot be transcribed."""


class Transcriber(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this transcriber can run right now."""

    def transcribe(self, audio: Path, *, language: str = "") -> Transcript: ...


__all__ = ["Transcriber", "TranscriptionError"]
