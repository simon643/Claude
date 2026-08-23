"""Templates: which sections appear, in what order, under what heading."""

from __future__ import annotations

import pytest

from minutely import render, templates
from minutely.models import ActionItem, Decision, Minutes, Topic


def minutes(template: str = "") -> Minutes:
    return Minutes(
        title="Weekly sync",
        held_on="2026-08-24",
        summary="They met.",
        notes="Pricing\n- 49 -> 65",
        topics=[Topic(title="Pricing", points=["Mid tier moves to 65."])],
        decisions=[Decision(text="Bundle analytics.")],
        actions=[ActionItem(text="Write the one-pager", owner="Sam")],
        open_questions=["Do we grandfather recent signups?"],
        review=[ActionItem(text="Credit the nineteen customers")],
        engine="rules",
        template=template,
    )


def test_an_unknown_or_missing_name_falls_back_to_the_general_template() -> None:
    assert templates.get("").name == "default"
    assert templates.get("nonsense").name == "default"
    assert templates.get("STANDUP").name == "standup"


def test_every_template_only_names_sections_the_renderers_know() -> None:
    for template in templates.TEMPLATES.values():
        assert set(template.sections) <= set(templates.SECTIONS), template.name


def test_a_template_renames_its_headings() -> None:
    client = templates.get("client")
    assert client.heading("actions") == "Commitments"
    assert client.heading("decisions") == "Agreed with the client"
    # A section it does not rename keeps the standard name.
    assert client.heading("summary") == "Summary"


def test_the_standup_leaves_out_decisions_and_calls_blockers_blockers() -> None:
    out = render.to_markdown(minutes("standup"))
    assert "## Blockers" in out
    assert "## Updates" in out
    assert "## Decisions" not in out
    assert "Bundle analytics." not in out


def test_the_client_template_reorders_and_renames() -> None:
    out = render.to_markdown(minutes("client"))
    assert "## Commitments" in out
    assert out.index("## Agreed with the client") < out.index("## Commitments")


def test_the_default_template_shows_everything() -> None:
    out = render.to_markdown(minutes())
    for heading in ("Your notes", "Summary", "Discussion", "Decisions", "Action points"):
        assert f"## {heading}" in out


def test_typed_notes_are_reproduced_verbatim() -> None:
    out = render.to_markdown(minutes())
    assert "> Pricing" in out
    assert "> - 49 -> 65" in out


@pytest.mark.parametrize("fmt", ["md", "txt", "html"])
def test_every_renderer_honours_the_template(fmt: str) -> None:
    # The plain-text renderer upper-cases its headings, so compare loosely.
    out = render.render(minutes("1-1"), fmt).lower()
    assert "follow-ups" in out
    assert "agreed" in out
    # The one-to-one template deliberately omits the low-confidence list.
    assert "credit the nineteen customers" not in out


def test_the_email_body_carries_the_notes_too() -> None:
    assert "49 -&gt; 65" in render.to_email_html(minutes())
