"""Reading and writing transcripts.

Four input shapes cover essentially everything a user will have on disk:

* **WebVTT** — what whisper.cpp, Teams, Zoom, and Meet all export.
* **SRT** — the same thing with commas in the timestamps.
* **whisper JSON** — both the OpenAI (`segments`) and whisper.cpp
  (`transcription`) layouts.
* **Plain text / markdown** — a typed-up or pasted transcript, with optional
  ``Name:`` speaker labels.

Everything lands as a :class:`~minutely.models.Transcript`, so the minutes
engines never learn what a cue is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Segment, Transcript, looks_like_sentence_colon

_TIMING = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
)
# WebVTT marks the speaker with a voice span: <v Alice>text</v>.
_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"</?[^>]+>")
_INLINE_SPEAKER = re.compile(r"^\s*([A-Z][\w'.\- ]{0,40}?)\s*:\s+(.*)$", re.DOTALL)
# whisper emits these for silence and background sound; they are not speech.
_NOISE = re.compile(r"^[\[(\*](?:blank_audio|inaudible|music|silence|laughter)[\])\*]$", re.I)


class TranscriptError(ValueError):
    """Raised when a file cannot be read as a transcript."""


def load(path: str | Path) -> Transcript:
    """Read any supported transcript file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError(f"cannot read {p}: {exc}")
    return parse(text, suffix=p.suffix.lower(), source=str(p))


def parse(text: str, suffix: str = "", source: str = "") -> Transcript:
    """Parse transcript text, sniffing the format when the suffix does not say."""
    stripped = text.strip()
    if not stripped:
        raise TranscriptError("transcript is empty")

    if suffix == ".json" or stripped[0] in "[{":
        return parse_whisper_json(stripped, source=source)
    if suffix == ".vtt" or stripped.upper().startswith("WEBVTT"):
        return parse_cues(stripped, source=source)
    if suffix == ".srt" or _TIMING.search(stripped):
        return parse_cues(stripped, source=source)
    return Transcript.from_text(stripped, source=source)


def parse_cues(text: str, source: str = "") -> Transcript:
    """Parse WebVTT or SRT. The two differ only in decimal separator and header."""
    segments: list[Segment] = []
    speaker = ""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith("WEBVTT"):
            lines = lines[1:]
        timing = None
        body_lines: list[str] = []
        for line in lines:
            match = _TIMING.search(line)
            if match and timing is None:
                timing = match
                continue
            # A bare cue number (SRT) or a cue identifier (VTT) carries nothing.
            if timing is None and re.fullmatch(r"\d+", line.strip()):
                continue
            if timing is None and "-->" in line:
                continue
            body_lines.append(line.strip())
        body = " ".join(body_lines).strip()
        if not body:
            continue

        voice = _VOICE.match(body)
        if voice:
            speaker = voice.group(1).strip()
            body = voice.group(2)
        body = _TAG.sub("", body).strip()
        if not body or _NOISE.match(body):
            continue

        # Only look for a "Name: ..." label when the cue did not carry a voice
        # tag. Otherwise a sentence like "One open question: do we..." would
        # overwrite the speaker the file already told us.
        if not voice:
            inline = _INLINE_SPEAKER.match(body)
            if inline and _is_speaker_label(inline.group(1)):
                speaker = inline.group(1).strip()
                body = inline.group(2).strip()

        segments.append(
            Segment(
                index=len(segments),
                text=body,
                speaker=speaker,
                start=_seconds(timing.group("start")) if timing else None,
                end=_seconds(timing.group("end")) if timing else None,
            )
        )

    if not segments:
        raise TranscriptError("no cues found in transcript")
    return Transcript(segments=segments, source=source)


def parse_whisper_json(text: str, source: str = "") -> Transcript:
    """Parse the OpenAI-whisper and whisper.cpp JSON layouts."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"invalid JSON transcript: {exc}")

    language = ""
    if isinstance(raw, dict):
        language = str(raw.get("language", "") or "")
        rows = raw.get("segments") or raw.get("transcription") or []
        if not rows and isinstance(raw.get("text"), str):
            return Transcript.from_text(raw["text"], source=source)
    elif isinstance(raw, list):
        rows = raw
    else:
        raise TranscriptError("JSON transcript must be an object or a list")

    segments: list[Segment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = str(row.get("text", "")).strip()
        if not body or _NOISE.match(body):
            continue
        start, end = _json_timing(row)
        speaker = str(row.get("speaker", "") or "").strip()
        inline = _INLINE_SPEAKER.match(body)
        if not speaker and inline and _is_speaker_label(inline.group(1)):
            speaker = inline.group(1).strip()
            body = inline.group(2).strip()
        segments.append(
            Segment(index=len(segments), text=body, speaker=speaker, start=start, end=end)
        )

    if not segments:
        raise TranscriptError("JSON transcript contained no segments")
    return Transcript(segments=segments, source=source, language=language)


def merge_utterances(transcript: Transcript, max_gap: float = 1.5) -> Transcript:
    """Glue whisper's short cues back into utterances.

    Speech recognisers cut on breath, not on meaning: "I'll send the deck" and
    "by Thursday" often land in different cues. Anything reading for intent
    wants them in one place, so consecutive cues from the same speaker are
    merged until the text ends a sentence or the pause gets long.
    """
    merged: list[Segment] = []
    for seg in transcript.segments:
        if merged:
            prev = merged[-1]
            gap = (
                seg.start - prev.end
                if seg.start is not None and prev.end is not None
                else 0.0
            )
            same_speaker = prev.speaker == seg.speaker
            unfinished = not prev.text.rstrip().endswith((".", "!", "?", ":", "…"))
            if same_speaker and unfinished and gap <= max_gap and len(prev.text) < 400:
                prev.text = f"{prev.text.rstrip()} {seg.text.lstrip()}"
                prev.end = seg.end if seg.end is not None else prev.end
                continue
        merged.append(
            Segment(
                index=len(merged),
                text=seg.text.strip(),
                speaker=seg.speaker,
                start=seg.start,
                end=seg.end,
            )
        )
    return Transcript(segments=merged, source=transcript.source, language=transcript.language)


def to_vtt(transcript: Transcript) -> str:
    """Render back to WebVTT, so an imported transcript can be re-exported."""
    out = ["WEBVTT", ""]
    for seg in transcript.segments:
        if seg.start is not None and seg.end is not None:
            out.append(f"{_clock(seg.start)} --> {_clock(seg.end)}")
        body = f"<v {seg.speaker}>{seg.text}" if seg.speaker else seg.text
        out.extend([body, ""])
    return "\n".join(out)


def _is_speaker_label(candidate: str) -> bool:
    """A speaker label is a short name, not the first clause of a sentence."""
    words = candidate.split()
    if not (1 <= len(words) <= 3):
        return False
    return not looks_like_sentence_colon(candidate)


def _json_timing(row: dict[str, object]) -> tuple[float | None, float | None]:
    offsets = row.get("offsets")
    if isinstance(offsets, dict):
        # whisper.cpp reports milliseconds.
        start = offsets.get("from")
        end = offsets.get("to")
        return (
            float(start) / 1000.0 if isinstance(start, (int, float)) else None,
            float(end) / 1000.0 if isinstance(end, (int, float)) else None,
        )
    timestamps = row.get("timestamps")
    if isinstance(timestamps, dict):
        return (
            _seconds(str(timestamps.get("from", ""))) if timestamps.get("from") else None,
            _seconds(str(timestamps.get("to", ""))) if timestamps.get("to") else None,
        )
    start_raw, end_raw = row.get("start"), row.get("end")
    return (
        float(start_raw) if isinstance(start_raw, (int, float)) else None,
        float(end_raw) if isinstance(end_raw, (int, float)) else None,
    )


def _seconds(stamp: str) -> float | None:
    """``00:01:02.500`` or ``01:02,500`` to seconds."""
    stamp = stamp.strip().replace(",", ".")
    if not stamp:
        return None
    parts = stamp.split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for value in values:
        total = total * 60 + value
    return total


def _clock(seconds: float) -> str:
    total = max(0.0, seconds)
    hours, rest = divmod(int(total), 3600)
    minutes, secs = divmod(rest, 60)
    millis = round((total - int(total)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
