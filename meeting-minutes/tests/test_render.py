from __future__ import annotations

import json

import pytest

from minutely import render
from minutely.models import ActionItem, Decision, Minutes, Segment, Topic, Transcript


def sample() -> tuple[Minutes, Transcript]:
    transcript = Transcript(
        segments=[
            Segment(index=0, text="Let's talk pricing.", speaker="Dana", start=61.0, end=64.0),
            Segment(index=1, text="I'll write the one-pager.", speaker="Sam", start=125.0, end=129.0),
        ]
    )
    minutes = Minutes(
        title="Weekly sync",
        held_on="2026-08-24",
        attendees=["Dana", "Sam"],
        summary="Two participants met.",
        topics=[Topic(title="Pricing", points=["Mid tier moves to 65."], segment_index=0)],
        decisions=[Decision(text="Bundle analytics into the mid tier.", segment_index=0)],
        actions=[ActionItem(text="Write the one-pager | for the board", owner="Sam", due="2026-09-01", segment_index=1)],
        review=[ActionItem(text="Credit the nineteen customers")],
        open_questions=["Do we grandfather recent signups?"],
        engine="rules",
    )
    return minutes, transcript


def test_markdown_has_every_section_and_cites_timestamps() -> None:
    minutes, transcript = sample()
    out = render.to_markdown(minutes, transcript, duration=2520)
    assert "# Weekly sync" in out
    assert "## Summary" in out
    assert "## Discussion" in out
    assert "## Decisions" in out
    assert "## Action points" in out
    assert "## Open questions" in out
    assert "needs review" in out.lower()
    assert "[01:01]" in out  # topic cite
    assert "[02:05]" in out  # action cite
    assert "42 min" in out


def test_markdown_escapes_pipes_so_the_table_survives() -> None:
    minutes, transcript = sample()
    row = next(
        line for line in render.to_markdown(minutes, transcript).splitlines()
        if "one-pager" in line
    )
    assert r"\|" in row
    # Four columns means five unescaped delimiters; the escaped one is content.
    assert row.replace(r"\|", "").count("|") == 5


def test_markdown_says_so_when_there_are_no_actions() -> None:
    out = render.to_markdown(Minutes(title="Quiet", engine="rules"))
    assert "_None recorded._" in out


def test_html_escapes_user_content() -> None:
    minutes = Minutes(title="<script>alert(1)</script>", summary="a & b", engine="rules")
    out = render.to_html(minutes)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "a &amp; b" in out


def test_html_is_self_contained() -> None:
    minutes, transcript = sample()
    out = render.to_html(minutes, transcript)
    assert out.startswith("<!doctype html>")
    assert "http://" not in out and "https://" not in out


def test_text_output_is_readable_without_markup() -> None:
    minutes, transcript = sample()
    out = render.to_text(minutes, transcript, duration=600)
    assert "ACTION POINTS" in out
    assert "Sam / due 2026-09-01" in out
    assert "<" not in out


def test_json_round_trips() -> None:
    minutes, _ = sample()
    restored = Minutes.from_dict(json.loads(render.render(minutes, "json")))
    assert restored.actions[0].owner == "Sam"
    assert restored.topics[0].title == "Pricing"
    assert restored.review[0].text == "Credit the nineteen customers"


def test_done_actions_are_marked() -> None:
    minutes, transcript = sample()
    minutes.actions[0].status = "done"
    assert "✅" in render.to_markdown(minutes, transcript)
    assert 'class="done"' in render.to_html(minutes, transcript)


def test_every_format_names_the_engine() -> None:
    minutes, transcript = sample()
    for fmt in ("md", "html", "txt"):
        assert "offline rules engine" in render.render(minutes, fmt, transcript)


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        render.render(Minutes(), "pdf")


def test_citations_are_omitted_without_a_transcript() -> None:
    minutes, _ = sample()
    assert "[02:05]" not in render.to_markdown(minutes)
