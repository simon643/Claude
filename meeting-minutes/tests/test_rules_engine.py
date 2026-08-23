"""The engine's contract: find what was committed, invent nothing."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from minutely import transcripts
from minutely.engines.rules import RulesEngine
from minutely.models import CERTAIN, LIKELY, Minutes, Transcript

MONDAY = date(2026, 8, 24)


def minute(text: str, held_on: date = MONDAY) -> Minutes:
    return RulesEngine().summarise(Transcript.from_text(text), held_on=held_on)


def action_texts(minutes: Minutes) -> list[str]:
    return [a.text for a in minutes.actions]


def all_texts(minutes: Minutes) -> list[str]:
    return [a.text for a in minutes.actions + minutes.review]


# -- commitments ------------------------------------------------------------


def test_first_person_commitment_is_owned_by_the_speaker() -> None:
    minutes = minute("Bob: I'll send the revised forecast to Finance.")
    (action,) = minutes.actions
    assert action.text == "Send the revised forecast to Finance"
    assert action.owner == "Bob"
    assert action.confidence == CERTAIN


def test_vocative_request_is_owned_by_the_person_addressed() -> None:
    minutes = minute("Dana: Priya, can you rerun the migration?\nPriya: Sure.")
    (action,) = minutes.actions
    assert action.owner == "Priya"
    assert action.text == "Rerun the migration"


def test_named_assignment_is_owned() -> None:
    minutes = minute("Dana: Marcus will chase the two remaining customers.")
    (action,) = minutes.actions
    assert action.owner == "Marcus"
    assert action.text.startswith("Chase the two remaining")


def test_explicit_action_label_wins() -> None:
    minutes = minute("Dana: Action: publish the incident review.")
    (action,) = minutes.actions
    assert action.text == "Publish the incident review"
    assert action.confidence == CERTAIN


def test_unassigned_collective_intent_is_held_back_for_review() -> None:
    minutes = minute("Dana: We should probably rewrite the onboarding email.")
    assert minutes.actions == []
    assert "Rewrite the onboarding email" in [a.text for a in minutes.review]


def test_collective_intent_with_a_deadline_is_published() -> None:
    minutes = minute("Dana: We need to publish the changelog by Friday.")
    (action,) = minutes.actions
    assert action.confidence == LIKELY
    assert action.due == "2026-08-28"
    # The deadline lives in its own column, not twice.
    assert "friday" not in action.text.lower()


def test_an_interjection_is_never_mistaken_for_an_owner() -> None:
    minutes = minute("Priya: Agreed, let's reprioritise the billing bug.")
    assert all(a.owner != "Agreed" for a in minutes.actions + minutes.review)


# -- things that are not actions -------------------------------------------


def test_past_tense_is_not_an_action() -> None:
    assert minute("Bob: I sent the deck yesterday.").actions == []


def test_reporting_on_history_is_not_an_action() -> None:
    assert minute("Sam: We raised prices eighteen months ago and lost nine percent.").actions == []


def test_hypotheticals_are_not_actions() -> None:
    assert minute("Sam: If we bundled analytics we would need to credit them.").actions == []


def test_pleasantries_are_not_actions() -> None:
    assert minute("Dana: Can you hear me? Thanks everyone.").actions == []


def test_a_copular_sentence_is_not_a_task() -> None:
    # "Sam, this is yours" assigns nothing anyone can do.
    assert minute("Dana: Sam, this is yours.").actions == []


def test_a_contentless_commitment_is_only_a_suggestion() -> None:
    minutes = minute("Sam: I'll take that too.")
    assert minutes.actions == []
    assert [a.text for a in minutes.review] == ["Take that too"]


# -- deadlines --------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("by Friday", "2026-08-28"),
        ("by next Tuesday", "2026-09-01"),
        ("by tomorrow", "2026-08-25"),
        ("by the end of the week", "2026-08-28"),
        ("by the end of the month", "2026-08-31"),
        ("in two weeks", "2026-09-07"),
        ("by 2026-12-01", "2026-12-01"),
        ("by the 3rd of September", "2026-09-03"),
        ("by March 2", "2027-03-02"),
    ],
)
def test_deadlines_resolve_against_the_meeting_date(phrase: str, expected: str) -> None:
    minutes = minute(f"Bob: I'll circulate the report {phrase}.")
    assert minutes.actions[0].due == expected


def test_an_unresolvable_deadline_keeps_the_spoken_phrase() -> None:
    minutes = minute("Bob: I'll circulate the report by the end of the sprint.")
    assert minutes.actions[0].due == "end of the sprint"


def test_a_deadline_in_the_following_sentence_attaches_to_the_commitment() -> None:
    minutes = minute("Priya: I'll spin up a Keycloak instance. Give me until Thursday.")
    assert minutes.actions[0].due == "2026-08-27"


def test_a_deadline_from_someone_else_does_not_attach() -> None:
    minutes = minute("Priya: I'll spin up a Keycloak instance.\nDana: By Thursday.")
    assert minutes.actions[0].due == ""


# -- decisions, questions, topics ------------------------------------------


def test_decisions_are_recorded_once() -> None:
    minutes = minute(
        "Dana: Let's go with the sixty-five price.\nDana: Let's go with the sixty-five price."
    )
    assert len(minutes.decisions) == 1


def test_a_question_about_a_decision_is_not_a_decision() -> None:
    assert minute("Sam: Should we go with the sixty-five price?").decisions == []


def test_a_decision_is_not_repeated_as_a_vague_action() -> None:
    minutes = minute("Dana: Let's go with the sixty-five price and bundle analytics.")
    assert len(minutes.decisions) == 1
    assert all_texts(minutes) == []


def test_open_questions_exclude_ones_answered_by_an_action() -> None:
    minutes = minute("Dana: Sam, can you write the one-pager for the board?")
    assert minutes.open_questions == []
    assert minutes.actions[0].owner == "Sam"


def test_open_questions_capture_stated_uncertainty() -> None:
    minutes = minute("Priya: One open question: do we grandfather anyone who signed last month?")
    assert minutes.open_questions
    assert "grandfather" in minutes.open_questions[0]


def test_agenda_markers_become_topics(demo_path: Path) -> None:
    transcript = transcripts.load(demo_path)
    minutes = RulesEngine().summarise(transcript, held_on=MONDAY)
    titles = [t.title for t in minutes.topics]
    assert "Pilot rollout" in titles
    assert "Pricing" in titles
    assert "Support backlog" in titles
    # Every topic carries the timestamp it started at.
    assert all(t.segment_index is not None for t in minutes.topics)


def test_the_demo_meeting_produces_owned_actions(demo_path: Path) -> None:
    transcript = transcripts.load(demo_path)
    minutes = RulesEngine().summarise(transcript, held_on=MONDAY)
    owners = {a.owner for a in minutes.actions}
    assert {"Priya", "Marcus", "Sam"} <= owners
    assert len(minutes.decisions) >= 2
    # Every published action is traceable back to a line in the transcript.
    assert all(a.segment_index is not None and a.quote for a in minutes.actions)


def test_summary_counts_match_the_body(demo_path: Path) -> None:
    transcript = transcripts.load(demo_path)
    minutes = RulesEngine().summarise(transcript, held_on=MONDAY)
    assert f"{len(minutes.actions)} action point" in minutes.summary
    assert f"{len(minutes.decisions)} decision" in minutes.summary


def test_an_empty_transcript_is_not_a_crash() -> None:
    minutes = RulesEngine().summarise(Transcript(), held_on=MONDAY)
    assert minutes.actions == []
    assert minutes.summary


def test_the_engine_is_deterministic(demo_path: Path) -> None:
    transcript = transcripts.load(demo_path)
    first = RulesEngine().summarise(transcript, held_on=MONDAY).to_dict()
    second = RulesEngine().summarise(transcript, held_on=MONDAY).to_dict()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
