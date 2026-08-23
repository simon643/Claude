"""The Claude engine's local half: grounding and mapping.

The API call itself is not exercised here — these tests cover what the module
does with what comes back, which is where a fabricated citation would slip
through.
"""

from __future__ import annotations

from datetime import date

import pytest

from minutely.engines import EngineError
from minutely.engines.claude import ClaudeEngine, _numbered, _QuoteIndex
from minutely.models import CERTAIN, POSSIBLE, Segment, Transcript


def transcript() -> Transcript:
    return Transcript(
        segments=[
            Segment(index=0, text="Let's talk about pricing.", speaker="Dana", start=10.0, end=13.0),
            Segment(index=1, text="I'll write the one-pager", speaker="Sam", start=61.0, end=63.0),
            Segment(index=2, text="for the board by Tuesday.", speaker="Sam", start=63.0, end=66.0),
        ]
    )


def test_a_quote_resolves_to_the_segment_it_came_from() -> None:
    index = _QuoteIndex(transcript())
    assert index.locate("I'll write the one-pager") == 1
    # Punctuation and casing differences must not defeat the match.
    assert index.locate("lets TALK about pricing") == 0


def test_a_quote_split_across_two_cues_still_resolves() -> None:
    index = _QuoteIndex(transcript())
    assert index.locate("write the one-pager for the board") in {1, 2}


def test_an_invented_quote_resolves_to_nothing() -> None:
    index = _QuoteIndex(transcript())
    assert index.locate("we agreed to cancel the project entirely") is None


def test_a_quote_too_short_to_locate_is_not_guessed() -> None:
    assert _QuoteIndex(transcript()).locate("yes") is None


def test_payload_is_mapped_and_grounded() -> None:
    engine = ClaudeEngine()
    payload = {
        "title": "Weekly sync",
        "summary": "They met.",
        "attendees": ["Dana", "Sam"],
        "topics": [{"title": "Pricing", "points": ["Mid tier moves to 65."]}],
        "decisions": [{"text": "Bundle analytics.", "quote": "Let's talk about pricing"}],
        "actions": [
            {
                "text": "Write the one-pager for the board",
                "owner": "Sam",
                "due": "2026-09-01",
                "confidence": 3,
                "quote": "I'll write the one-pager",
            },
            {
                "text": "Something nobody said",
                "owner": "",
                "due": "",
                "confidence": 1,
                "quote": "we agreed to cancel the project",
            },
        ],
        "open_questions": ["Do we grandfather recent signups?"],
    }
    minutes = engine._minutes(payload, transcript(), date(2026, 8, 24), "m1", "")

    assert minutes.title == "Weekly sync"
    assert minutes.meeting_id == "m1"
    assert minutes.engine == "claude"
    # The confident action is cited; the unsupported one carries no citation
    # and is held back for review rather than published.
    (action,) = minutes.actions
    assert action.segment_index == 1
    assert action.confidence == CERTAIN
    assert minutes.review[0].segment_index is None
    assert minutes.review[0].confidence == POSSIBLE
    assert minutes.decisions[0].segment_index == 0


def test_malformed_rows_are_skipped_not_crashed_on() -> None:
    minutes = ClaudeEngine()._minutes(
        {"actions": ["nonsense", {"text": ""}], "topics": [{"title": ""}], "decisions": [{}]},
        transcript(),
        date(2026, 8, 24),
        "m1",
        "Fallback title",
    )
    assert minutes.actions == []
    assert minutes.topics == []
    assert minutes.decisions == []
    assert minutes.title == "Fallback title"
    # With no attendee list from the model, the transcript's speakers stand in.
    assert minutes.attendees == ["Dana", "Sam"]


def test_the_prompt_carries_timestamps_and_speakers() -> None:
    rendered = _numbered(transcript())
    assert "[00:10] Dana: Let's talk about pricing." in rendered
    assert "[01:01] Sam:" in rendered


def test_an_empty_transcript_is_refused_before_any_api_call() -> None:
    with pytest.raises(EngineError, match="empty"):
        ClaudeEngine().summarise(Transcript())


def test_a_missing_dependency_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(EngineError, match=r"minutely\[llm\]"):
        ClaudeEngine().summarise(transcript())
