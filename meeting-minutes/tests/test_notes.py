"""Typed notes, and what the engine makes of them.

The premise: a line someone wrote while the meeting was happening is better
evidence than anything a pattern can infer from the transcript, so it wins.
"""

from __future__ import annotations

from datetime import date

from minutely.engines.rules import RulesEngine, parse_notes
from minutely.models import CERTAIN, LIKELY, Minutes, Segment, Transcript

MONDAY = date(2026, 8, 24)


def minute(text: str, notes: str, **kwargs: object) -> Minutes:
    return RulesEngine().summarise(
        Transcript.from_text(text), held_on=MONDAY, notes=notes, **kwargs  # type: ignore[arg-type]
    )


# -- reading the notes ------------------------------------------------------


def test_a_short_unbulleted_line_is_a_heading() -> None:
    (note,) = parse_notes("Pricing")
    assert note.kind == "heading"
    assert note.text == "Pricing"


def test_a_bullet_is_a_point() -> None:
    notes = parse_notes("Pricing\n- mid tier moves to 65")
    assert [n.kind for n in notes] == ["heading", "point"]
    assert notes[1].text == "mid tier moves to 65"
    assert notes[1].parent is notes[0]


def test_todo_and_checkbox_and_arrow_are_actions() -> None:
    notes = parse_notes(
        "TODO: circulate the deck\n"
        "[ ] book the room\n"
        "[x] chase the invoice\n"
        "send the forecast -> Sam"
    )
    assert [n.kind for n in notes] == ["action", "action", "action", "action"]
    assert notes[0].text == "circulate the deck"
    assert notes[3].text == "send the forecast"
    assert notes[3].owner == "Sam"


def test_a_name_before_a_colon_assigns_the_action() -> None:
    (note,) = parse_notes("TODO: Priya: rerun the migration")
    assert note.owner == "Priya"
    assert note.text == "rerun the migration"


def test_an_arrow_between_two_non_names_is_not_an_assignment() -> None:
    # "49 -> 65" is a price change. Reading it as an action owned by "49" is
    # the kind of confident nonsense this engine exists to avoid.
    (note,) = parse_notes("- 49 -> 65, bundle analytics")
    assert note.kind == "point"
    assert note.owner == ""


def test_blank_lines_and_bullet_styles_do_not_confuse_it() -> None:
    notes = parse_notes("Pricing\n\n  * one thing\n  • another\n1. a third\n")
    assert [n.kind for n in notes] == ["heading", "point", "point", "point"]
    assert notes[1].text == "one thing"


# -- folding them into the minutes -----------------------------------------


def test_a_jotted_action_is_an_action_even_if_nobody_said_it_that_way() -> None:
    minutes = minute("Dana: We talked about the invoice for a while.", "TODO: chase the invoice")
    (action,) = minutes.actions
    assert action.text == "Chase the invoice"
    assert action.confidence == CERTAIN


def test_a_jotted_action_with_no_owner_stays_unassigned() -> None:
    # Guessing an owner from whoever was talking nearby would put a name
    # against work that person never agreed to.
    minutes = minute("Priya: The migration is going to need a rerun.", "[ ] rerun the migration")
    assert minutes.actions[0].owner == ""


def test_an_owner_written_in_the_note_is_used() -> None:
    minutes = minute("Dana: Someone should rerun the migration.", "TODO: Priya: rerun the migration")
    assert minutes.actions[0].owner == "Priya"


def test_a_jotted_deadline_is_resolved() -> None:
    minutes = minute("Dana: We should send it.", "TODO: send the forecast by Friday")
    assert minutes.actions[0].due == "2026-08-28"


def test_writing_something_down_promotes_the_engine_s_guess() -> None:
    spoken = "Dana: We should probably rewrite the onboarding email."
    unaided = minute(spoken, "")
    assert unaided.actions == []
    assert unaided.review

    confirmed = minute(spoken, "- rewrite the onboarding email")
    assert [a.text for a in confirmed.actions] == ["Rewrite the onboarding email"]
    assert confirmed.actions[0].confidence == LIKELY
    assert confirmed.review == []


def test_the_same_action_is_not_recorded_twice() -> None:
    minutes = minute("Sam: I'll write the one-pager.", "TODO: Sam: write the one-pager")
    assert len(minutes.actions) == 1


def test_headings_become_the_outline() -> None:
    transcript = Transcript(
        segments=[
            Segment(index=0, text="The pricing proposal moves the mid tier to sixty-five.", speaker="Sam"),
            Segment(index=1, text="Support backlog is at two hundred and forty tickets.", speaker="Marcus"),
        ]
    )
    minutes = RulesEngine().summarise(
        transcript,
        held_on=MONDAY,
        title="Weekly sync",
        notes="Pricing\n- mid tier to 65\n\nSupport backlog",
    )
    assert [t.title for t in minutes.topics] == ["Pricing", "Support backlog"]
    # The user's own point comes first; the transcript fills in around it.
    assert minutes.topics[0].points[0] == "mid tier to 65"
    assert any("sixty-five" in point for point in minutes.topics[0].points)
    assert any("tickets" in point for point in minutes.topics[1].points)


def test_a_title_line_above_the_headings_names_the_meeting() -> None:
    minutes = minute(
        "Dana: Pricing and the backlog and the rollout.",
        "Weekly product sync\n\nPricing\n\nSupport backlog\n\nPilot rollout",
    )
    assert minutes.title == "Weekly product sync"
    assert [t.title for t in minutes.topics] == ["Pricing", "Support backlog", "Pilot rollout"]


def test_the_first_heading_is_kept_as_a_topic_when_the_meeting_already_has_a_title() -> None:
    minutes = minute(
        "Dana: Pricing and the backlog and the rollout.",
        "Pricing\n\nSupport backlog\n\nPilot rollout",
        title="Weekly product sync",
    )
    assert minutes.title == "Weekly product sync"
    assert [t.title for t in minutes.topics] == ["Pricing", "Support backlog", "Pilot rollout"]


def test_the_notes_are_kept_verbatim() -> None:
    notes = "Pricing\n- 49 -> 65\nTODO: Sam: circulate mechanics"
    minutes = minute("Dana: Right, pricing.", notes)
    # Nobody should have to trust a summary of their own words.
    assert minutes.notes == notes


def test_no_notes_means_the_engine_behaves_exactly_as_before() -> None:
    spoken = "Sam: I'll write the one-pager by Friday."
    with_empty = minute(spoken, "")
    assert with_empty.actions[0].owner == "Sam"
    assert with_empty.notes == ""
    assert with_empty.topics == RulesEngine().summarise(
        Transcript.from_text(spoken), held_on=MONDAY
    ).topics
