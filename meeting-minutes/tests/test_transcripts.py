from __future__ import annotations

from pathlib import Path

import pytest

from minutely import transcripts
from minutely.models import Transcript

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Alice>Morning all.

2
00:00:04.500 --> 00:00:09.250
<v Bob>I'll send the deck after this.

00:00:09.500 --> 00:00:10.000
[BLANK_AUDIO]
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Alice: We should ship on Friday.

2
00:00:03,500 --> 00:00:06,000
Bob: Agreed.
"""


def test_parse_vtt_reads_speakers_and_timings() -> None:
    transcript = transcripts.parse(VTT, suffix=".vtt")
    assert transcript.speakers() == ["Alice", "Bob"]
    assert transcript.segments[0].start == 1.0
    assert transcript.segments[1].end == 9.25
    # The [BLANK_AUDIO] cue is noise, not speech.
    assert len(transcript.segments) == 2


def test_parse_srt_handles_comma_timings_and_inline_labels() -> None:
    transcript = transcripts.parse(SRT, suffix=".srt")
    assert transcript.speakers() == ["Alice", "Bob"]
    assert transcript.segments[0].text == "We should ship on Friday."
    assert transcript.segments[1].start == 3.5


def test_inline_label_is_not_stolen_from_a_voice_tag() -> None:
    # "One open question:" reads like a speaker label but the cue already named
    # its speaker, so the label must be ignored.
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Priya>One open question: do we ship?\n"
    transcript = transcripts.parse(text, suffix=".vtt")
    assert transcript.speakers() == ["Priya"]
    assert transcript.segments[0].text.startswith("One open question")


def test_parse_openai_whisper_json() -> None:
    payload = '{"language": "en", "segments": [{"start": 0.0, "end": 2.5, "text": " Hello there."}]}'
    transcript = transcripts.parse(payload, suffix=".json")
    assert transcript.language == "en"
    assert transcript.segments[0].text == "Hello there."
    assert transcript.segments[0].end == 2.5


def test_parse_whisper_cpp_json_uses_millisecond_offsets() -> None:
    payload = '{"transcription": [{"offsets": {"from": 1500, "to": 4000}, "text": "Right."}]}'
    transcript = transcripts.parse(payload, suffix=".json")
    assert transcript.segments[0].start == 1.5
    assert transcript.segments[0].end == 4.0


def test_plain_text_keeps_speaker_until_the_next_label() -> None:
    transcript = Transcript.from_text("Ada: First point.\nStill Ada talking.\nBob: My turn.")
    assert [s.speaker for s in transcript.segments] == ["Ada", "Ada", "Bob"]


def test_merge_utterances_joins_split_sentences() -> None:
    transcript = transcripts.parse(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<v Bob>I'll send the deck\n\n"
        "00:00:02.200 --> 00:00:04.000\n<v Bob>by Thursday.\n",
        suffix=".vtt",
    )
    merged = transcripts.merge_utterances(transcript)
    assert len(merged.segments) == 1
    assert merged.segments[0].text == "I'll send the deck by Thursday."
    assert merged.segments[0].end == 4.0


def test_merge_utterances_does_not_join_across_speakers() -> None:
    transcript = transcripts.parse(SRT, suffix=".srt")
    assert len(transcripts.merge_utterances(transcript).segments) == 2


def test_round_trip_through_vtt(tmp_path: Path, demo_path: Path) -> None:
    original = transcripts.load(demo_path)
    written = tmp_path / "out.vtt"
    written.write_text(transcripts.to_vtt(original), encoding="utf-8")
    again = transcripts.load(written)
    assert [s.text for s in again.segments] == [s.text for s in original.segments]
    assert again.speakers() == original.speakers()


def test_empty_and_broken_inputs_raise() -> None:
    with pytest.raises(transcripts.TranscriptError):
        transcripts.parse("   ")
    with pytest.raises(transcripts.TranscriptError):
        transcripts.parse("{not json", suffix=".json")
    with pytest.raises(transcripts.TranscriptError):
        transcripts.load(Path("/nonexistent/transcript.vtt"))
