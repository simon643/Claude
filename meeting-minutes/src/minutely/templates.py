"""Minutes templates.

A standup, a one-to-one, and a client call want different documents out the
other end. A template names the sections, the order they appear in, and — for
the Claude engine — a sentence about what the meeting is for. It does not
invent content: a section with nothing behind it is dropped rather than filled.

Templates are deliberately few and fixed. A dozen of them would be a
configuration surface; four is a choice someone can make in a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every section a renderer knows how to produce. A template is a subset, in
# the order it wants them.
SECTIONS = ("notes", "summary", "topics", "decisions", "actions", "questions", "review")

_LABELS = {
    "notes": "Your notes",
    "summary": "Summary",
    "topics": "Discussion",
    "decisions": "Decisions",
    "actions": "Action points",
    "questions": "Open questions",
    "review": "Possible actions (needs review)",
}


@dataclass(frozen=True)
class Template:
    name: str
    label: str
    description: str
    sections: tuple[str, ...]
    # Section headings this template renames, e.g. a standup's "Blockers".
    headings: dict[str, str] = field(default_factory=dict)
    # One line of steer for the Claude engine. The rules engine ignores it:
    # patterns cannot be told what a meeting is for.
    guidance: str = ""

    def heading(self, section: str) -> str:
        return self.headings.get(section, _LABELS.get(section, section.title()))

    def includes(self, section: str) -> bool:
        return section in self.sections


DEFAULT = Template(
    name="default",
    label="General meeting",
    description="Summary, discussion, decisions, actions.",
    sections=("notes", "summary", "topics", "decisions", "actions", "questions", "review"),
)

STANDUP = Template(
    name="standup",
    label="Stand-up",
    description="Short, per-person, blockers to the front.",
    sections=("notes", "summary", "topics", "actions", "questions", "review"),
    headings={"topics": "Updates", "questions": "Blockers"},
    guidance=(
        "This is a stand-up. Keep the summary to two sentences. Organise the updates by "
        "person where the transcript makes that possible, and treat anything that sounds "
        "like a blocker as an open question rather than an action."
    ),
)

ONE_TO_ONE = Template(
    name="1-1",
    label="One-to-one",
    description="Discussion, agreements, follow-ups.",
    sections=("notes", "summary", "topics", "decisions", "actions", "questions"),
    headings={"topics": "Discussed", "decisions": "Agreed", "actions": "Follow-ups"},
    guidance=(
        "This is a one-to-one. It is a private conversation: keep the summary factual and "
        "unflattering to nobody, and do not editorialise about performance. Follow-ups "
        "belong to whichever of the two people took them."
    ),
)

CLIENT = Template(
    name="client",
    label="Client call",
    description="What they asked for, what we committed to.",
    sections=("notes", "summary", "topics", "decisions", "actions", "questions", "review"),
    headings={
        "topics": "What the client raised",
        "decisions": "Agreed with the client",
        "actions": "Commitments",
        "questions": "Awaiting an answer",
    },
    guidance=(
        "This is an external client call. Be precise about who committed to what, because "
        "these notes may be quoted back. Separate what the client asked for from what was "
        "actually promised, and never soften a commitment into a maybe or vice versa."
    ),
)

TEMPLATES: dict[str, Template] = {t.name: t for t in (DEFAULT, STANDUP, ONE_TO_ONE, CLIENT)}
NAMES = tuple(TEMPLATES)


def get(name: str | None) -> Template:
    """Look up a template, falling back to the general one."""
    return TEMPLATES.get((name or "").strip().lower(), DEFAULT)


def catalogue() -> list[dict[str, str]]:
    """The list the UI's template picker renders."""
    return [
        {"name": t.name, "label": t.label, "description": t.description}
        for t in TEMPLATES.values()
    ]
