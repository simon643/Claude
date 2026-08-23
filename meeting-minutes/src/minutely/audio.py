"""Audio file handling.

Deliberately thin. The browser produces a container the browser chose, and the
only questions this app needs answered about it are "what should it be called
on disk", "how long is it", and "can whisper read it".
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

# MediaRecorder gives us a MIME type; we want a sensible extension for it.
_MIME_SUFFIX = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AudioError(RuntimeError):
    """Raised when an audio file cannot be handled."""


def suffix_for(mime: str) -> str:
    """Map a MediaRecorder MIME type to a file extension."""
    base = (mime or "").split(";")[0].strip().lower()
    return _MIME_SUFFIX.get(base, ".webm")


def safe_name(name: str, fallback: str = "meeting") -> str:
    """A filename that cannot escape its directory or surprise a shell."""
    cleaned = re.sub(r"-{2,}", "-", _SAFE.sub("-", Path(name).name)).strip("-._")
    return cleaned[:80] or fallback


def duration(path: str | Path) -> float | None:
    """Length in seconds, or None if it cannot be determined cheaply.

    Tries the WAV header first (no subprocess, always right for WAV), then
    ffprobe if it happens to be installed. A missing duration is not an error —
    it only affects a line of prose in the minutes.
    """
    p = Path(path)
    if not p.exists():
        return None
    if p.suffix.lower() == ".wav":
        with (
            contextlib.suppress(wave.Error, OSError, EOFError, struct.error),
            wave.open(str(p), "rb") as handle,
        ):
            rate = handle.getframerate()
            if rate:
                return handle.getnframes() / float(rate)
    return _ffprobe_duration(p)


def _ffprobe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
        value = payload.get("format", {}).get("duration")
        return float(value) if value is not None else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def to_wav16k(source: str | Path, target: str | Path) -> Path:
    """Transcode to 16 kHz mono WAV, which is what whisper.cpp accepts.

    Requires ffmpeg. Raises :class:`AudioError` with an actionable message when
    it is not installed, rather than failing deep inside the transcriber.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError(
            "ffmpeg is needed to convert browser audio for whisper.cpp — "
            "install it, or use an OpenAI-whisper style binary which reads the file directly"
        )
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(source), "-ac", "1", "-ar", "16000", "-vn", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not out.exists():
        detail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise AudioError(f"ffmpeg could not convert {Path(source).name}: {detail[0]}")
    return out
