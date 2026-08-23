"""Domain objects: meetings, transcripts, and the minutes produced from them.

These are plain dataclasses with explicit ``to_dict`` / ``from_dict`` pairs
rather than anything clever, because they are serialised into SQLite, into JSON
exports, and into the API payloads the browser UI reads. One obvious shape,
used everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Action items carry a coarse confidence so the UI can separate "someone
# clearly committed to this" from "this might be an action". Anything below
# LIKELY is held back for review rather than published in the minutes.
CERTAIN = 3
LIKELY = 2
POSSIBLE = 1

_SPEAKER_SPLIT = re.compile(r"^\s*([A-Z][\w'.\- ]{0,40}?)\s*:\s+(.*)$")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Segment:
    """One utterance: who said it, when, and what."""

    index: int
    text: str
    speaker: str = ""
    start: float | None = None
    end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Segment:
        return cls(
            index=int(raw.get("index", 0)),
            text=str(raw.get("text", "")),
            speaker=str(raw.get("speaker", "")),
            start=_opt_float(raw.get("start")),
            end=_opt_float(raw.get("end")),
        )

    @property
    def timestamp(self) -> str:
        """``mm:ss`` (or ``h:mm:ss``) for citing a line in the minutes."""
        if self.start is None:
            return ""
        return format_clock(self.start)


@dataclass
class Transcript:
    """An ordered list of segments plus whatever we know about its origin."""

    segments: list[Segment] = field(default_factory=list)
    source: str = ""
    language: str = ""

    @property
    def text(self) -> str:
        lines = []
        for seg in self.segments:
            prefix = f"{seg.speaker}: " if seg.speaker else ""
            lines.append(f"{prefix}{seg.text}")
        return "\n".join(lines)

    @property
    def duration(self) -> float | None:
        ends = [s.end for s in self.segments if s.end is not None]
        return max(ends) if ends else None

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)

    def speakers(self) -> list[str]:
        """Distinct speakers in first-appearance order."""
        seen: dict[str, None] = {}
        for seg in self.segments:
            if seg.speaker:
                seen.setdefault(seg.speaker, None)
        return list(seen)

    def cited(self, index: int | None) -> str:
        """A ``[mm:ss]`` marker for a segment, or an empty string."""
        if index is None:
            return ""
        for seg in self.segments:
            if seg.index == index:
                return f"[{seg.timestamp}]" if seg.timestamp else ""
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "language": self.language,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Transcript:
        return cls(
            segments=[Segment.from_dict(s) for s in raw.get("segments", [])],
            source=str(raw.get("source", "")),
            language=str(raw.get("language", "")),
        )

    @classmethod
    def from_text(cls, text: str, source: str = "") -> Transcript:
        """Parse plain text, honouring ``Name: said something`` speaker labels.

        A line without a label continues the previous speaker, which is how
        people actually type up notes.
        """
        segments: list[Segment] = []
        speaker = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            match = _SPEAKER_SPLIT.match(stripped)
            if match and not looks_like_sentence_colon(match.group(1)):
                speaker = match.group(1).strip()
                body = match.group(2).strip()
            else:
                body = stripped
            if body:
                segments.append(Segment(index=len(segments), text=body, speaker=speaker))
        return cls(segments=segments, source=source)


@dataclass
class ActionItem:
    """Something a named person agreed to do."""

    text: str
    owner: str = ""
    due: str = ""
    confidence: int = LIKELY
    segment_index: int | None = None
    quote: str = ""
    status: str = "open"  # open | done | dropped
    action_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.action_id,
            "text": self.text,
            "owner": self.owner,
            "due": self.due,
            "confidence": self.confidence,
            "segment_index": self.segment_index,
            "quote": self.quote,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ActionItem:
        return cls(
            text=str(raw.get("text", "")).strip(),
            owner=str(raw.get("owner", "") or "").strip(),
            due=str(raw.get("due", "") or "").strip(),
            confidence=int(raw.get("confidence", LIKELY)),
            segment_index=_opt_int(raw.get("segment_index")),
            quote=str(raw.get("quote", "") or ""),
            status=str(raw.get("status", "open")),
            action_id=_opt_int(raw.get("id")),
        )

    def key(self) -> str:
        """Stable identity for de-duplication within a meeting."""
        words = re.sub(r"[^a-z0-9 ]+", " ", self.text.lower()).split()
        return f"{self.owner.lower()}|{' '.join(words)}"


@dataclass
class Decision:
    """A conclusion the meeting reached."""

    text: str
    segment_index: int | None = None
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "segment_index": self.segment_index, "quote": self.quote}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Decision:
        return cls(
            text=str(raw.get("text", "")).strip(),
            segment_index=_opt_int(raw.get("segment_index")),
            quote=str(raw.get("quote", "") or ""),
        )


@dataclass
class Topic:
    """A discussion item: what was talked about, and the gist of it."""

    title: str
    points: list[str] = field(default_factory=list)
    segment_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "points": self.points, "segment_index": self.segment_index}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Topic:
        return cls(
            title=str(raw.get("title", "")).strip(),
            points=[str(p).strip() for p in raw.get("points", []) if str(p).strip()],
            segment_index=_opt_int(raw.get("segment_index")),
        )


@dataclass
class Minutes:
    """The deliverable."""

    meeting_id: str = ""
    title: str = ""
    held_on: str = ""
    attendees: list[str] = field(default_factory=list)
    summary: str = ""
    topics: list[Topic] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    engine: str = ""
    generated_at: str = field(default_factory=utcnow)
    # Actions the engine was unsure about. Kept out of the minutes body so the
    # action register stays trustworthy, but surfaced for a human to promote.
    review: list[ActionItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "held_on": self.held_on,
            "attendees": self.attendees,
            "summary": self.summary,
            "topics": [t.to_dict() for t in self.topics],
            "decisions": [d.to_dict() for d in self.decisions],
            "actions": [a.to_dict() for a in self.actions],
            "review": [a.to_dict() for a in self.review],
            "open_questions": self.open_questions,
            "engine": self.engine,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Minutes:
        return cls(
            meeting_id=str(raw.get("meeting_id", "")),
            title=str(raw.get("title", "")),
            held_on=str(raw.get("held_on", "")),
            attendees=[str(a) for a in raw.get("attendees", [])],
            summary=str(raw.get("summary", "")),
            topics=[Topic.from_dict(t) for t in raw.get("topics", [])],
            decisions=[Decision.from_dict(d) for d in raw.get("decisions", [])],
            actions=[ActionItem.from_dict(a) for a in raw.get("actions", [])],
            review=[ActionItem.from_dict(a) for a in raw.get("review", [])],
            open_questions=[str(q) for q in raw.get("open_questions", [])],
            engine=str(raw.get("engine", "")),
            generated_at=str(raw.get("generated_at", "")) or utcnow(),
        )


@dataclass
class Meeting:
    """A recorded (or imported) meeting and where its files live."""

    meeting_id: str
    title: str = ""
    held_on: str = ""
    duration: float | None = None
    audio_path: str = ""
    transcript_path: str = ""
    participants: list[str] = field(default_factory=list)
    status: str = "new"  # new | recording | transcribed | minuted
    # Where the meeting came from, and its id in that system. Together these
    # are what stop a second `minutely teams pull` from importing everything
    # twice.
    source: str = "local"  # local | teams
    external_id: str = ""
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "held_on": self.held_on,
            "duration": self.duration,
            "audio_path": self.audio_path,
            "transcript_path": self.transcript_path,
            "participants": self.participants,
            "status": self.status,
            "source": self.source,
            "external_id": self.external_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Meeting:
        return cls(
            meeting_id=str(raw["meeting_id"]),
            title=str(raw.get("title", "")),
            held_on=str(raw.get("held_on", "")),
            duration=_opt_float(raw.get("duration")),
            audio_path=str(raw.get("audio_path", "")),
            transcript_path=str(raw.get("transcript_path", "")),
            participants=[str(p) for p in raw.get("participants", [])],
            status=str(raw.get("status", "new")),
            source=str(raw.get("source", "local")) or "local",
            external_id=str(raw.get("external_id", "")),
            created_at=str(raw.get("created_at", "")) or utcnow(),
        )


def format_clock(seconds: float) -> str:
    """Seconds as ``mm:ss``, or ``h:mm:ss`` once past an hour."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_duration(seconds: float | None) -> str:
    """Human phrasing for a meeting length ("42 min", "1 h 05 min")."""
    if seconds is None:
        return "unknown"
    total = round(seconds)
    if total < 60:
        return f"{total} s"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def looks_like_sentence_colon(candidate: str) -> bool:
    """Reject ``Note: ...`` style prefixes that are not speaker labels.

    A speaker label is short and reads like a name. Anything with more than
    three words, or an obvious document keyword, is prose that happens to
    contain a colon.
    """
    lowered = candidate.strip().lower()
    if lowered in {"note", "notes", "warning", "action", "actions", "todo", "agenda", "update"}:
        return True
    return len(candidate.split()) > 3


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
