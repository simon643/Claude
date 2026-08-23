"""Rendering minutes for humans.

Markdown is the primary format because it pastes into every wiki, ticket, and
mail client without losing structure. HTML is a self-contained page for
printing or sending. Text is for the terminal.

Every renderer shows the same thing, including the parts that are easy to hide:
which engine produced the minutes, and which items the engine was not sure
about. Minutes that quietly drop their own uncertainty are how a wrong action
point ends up in someone's week.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from .models import CERTAIN, ActionItem, Minutes, Transcript, format_duration

FORMATS = ("md", "html", "txt", "json")


def render(
    minutes: Minutes,
    fmt: str = "md",
    transcript: Transcript | None = None,
    duration: float | None = None,
) -> str:
    chosen = fmt.lower().strip()
    if chosen in {"md", "markdown"}:
        return to_markdown(minutes, transcript, duration)
    if chosen == "html":
        return to_html(minutes, transcript, duration)
    if chosen in {"txt", "text"}:
        return to_text(minutes, transcript, duration)
    if chosen == "json":
        import json as _json

        return _json.dumps(minutes.to_dict(), indent=2)
    raise ValueError(f"unknown format {fmt!r} (choose from: {', '.join(FORMATS)})")


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def to_markdown(
    minutes: Minutes, transcript: Transcript | None = None, duration: float | None = None
) -> str:
    cite = _citer(transcript)
    out: list[str] = [f"# {minutes.title or 'Meeting minutes'}", ""]

    meta = [f"**Date:** {minutes.held_on}"] if minutes.held_on else []
    if duration is not None:
        meta.append(f"**Duration:** {format_duration(duration)}")
    if minutes.attendees:
        meta.append(f"**Attendees:** {', '.join(minutes.attendees)}")
    if meta:
        out.extend([" · ".join(meta), ""])

    if minutes.summary:
        out.extend(["## Summary", "", minutes.summary, ""])

    if minutes.topics:
        out.extend(["## Discussion", ""])
        for topic in minutes.topics:
            marker = cite(topic.segment_index)
            out.append(f"### {topic.title}{marker}")
            out.append("")
            out.extend(f"- {point}" for point in topic.points)
            out.append("")

    if minutes.decisions:
        out.extend(["## Decisions", ""])
        for i, decision in enumerate(minutes.decisions, start=1):
            out.append(f"{i}. {decision.text}{cite(decision.segment_index)}")
        out.append("")

    out.extend(["## Action points", ""])
    if minutes.actions:
        out.append("| # | Action | Owner | Due |")
        out.append("|---|--------|-------|-----|")
        for i, item in enumerate(minutes.actions, start=1):
            owner = item.owner or "_unassigned_"
            due = item.due or "—"
            status = " ✅" if item.status == "done" else ""
            text = _escape_pipes(item.text) + cite(item.segment_index) + status
            out.append(f"| {i} | {text} | {owner} | {due} |")
    else:
        out.append("_None recorded._")
    out.append("")

    if minutes.open_questions:
        out.extend(["## Open questions", ""])
        out.extend(f"- {q}" for q in minutes.open_questions)
        out.append("")

    if minutes.review:
        out.extend(
            [
                "## Possible actions (needs review)",
                "",
                "_Detected but not confidently a commitment — confirm before circulating._",
                "",
            ]
        )
        for item in minutes.review:
            owner = f" — {item.owner}" if item.owner else ""
            out.append(f"- {item.text}{owner}{cite(item.segment_index)}")
        out.append("")

    out.extend(["---", "", _footer(minutes), ""])
    return "\n".join(out)


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------


def to_text(
    minutes: Minutes, transcript: Transcript | None = None, duration: float | None = None
) -> str:
    cite = _citer(transcript)
    out: list[str] = [minutes.title or "Meeting minutes", "=" * 60]
    if minutes.held_on:
        out.append(f"Date      : {minutes.held_on}")
    if duration is not None:
        out.append(f"Duration  : {format_duration(duration)}")
    if minutes.attendees:
        out.append(f"Attendees : {', '.join(minutes.attendees)}")
    out.append("")

    if minutes.summary:
        out.extend([_wrap(minutes.summary), ""])

    for topic in minutes.topics:
        out.append(f"-- {topic.title}{cite(topic.segment_index)}")
        out.extend(_wrap(f"   * {p}", subsequent="     ") for p in topic.points)
        out.append("")

    if minutes.decisions:
        out.append("DECISIONS")
        for i, decision in enumerate(minutes.decisions, start=1):
            out.append(_wrap(f" {i}. {decision.text}{cite(decision.segment_index)}", subsequent="    "))
        out.append("")

    out.append("ACTION POINTS")
    if minutes.actions:
        for i, item in enumerate(minutes.actions, start=1):
            bits = [f" {i}. {item.text}{cite(item.segment_index)}"]
            detail = " / ".join(
                filter(None, [item.owner or "unassigned", f"due {item.due}" if item.due else ""])
            )
            if detail:
                bits.append(f"    [{detail}]")
            if item.status != "open":
                bits.append(f"    ({item.status})")
            out.extend(bits)
    else:
        out.append(" (none recorded)")
    out.append("")

    if minutes.open_questions:
        out.append("OPEN QUESTIONS")
        out.extend(_wrap(f" - {q}", subsequent="   ") for q in minutes.open_questions)
        out.append("")

    if minutes.review:
        out.append("POSSIBLE ACTIONS (needs review)")
        for item in minutes.review:
            owner = f" — {item.owner}" if item.owner else ""
            out.append(_wrap(f" ? {item.text}{owner}", subsequent="   "))
        out.append("")

    out.append(_footer(minutes))
    return "\n".join(out)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 46rem; margin: 2.5rem auto; padding: 0 1.25rem; color: #17181c; background: #fff; }
h1 { font-size: 1.7rem; margin-bottom: .25rem; }
h2 { font-size: 1.15rem; margin-top: 2rem; border-bottom: 1px solid #e4e6eb; padding-bottom: .3rem; }
h3 { font-size: 1rem; margin-bottom: .3rem; }
.meta { color: #5b6070; font-size: .9rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .95rem; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #e4e6eb; vertical-align: top; }
th { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; color: #5b6070; }
.cite { color: #8a8f9c; font-size: .8rem; font-variant-numeric: tabular-nums; }
.unassigned { color: #a4471f; }
.done { text-decoration: line-through; color: #6b7280; }
.review { background: #fff8e6; border: 1px solid #f0dfb0; border-radius: 8px; padding: .75rem 1rem; }
footer { margin-top: 2.5rem; color: #8a8f9c; font-size: .82rem; border-top: 1px solid #e4e6eb; padding-top: .75rem; }
@media (prefers-color-scheme: dark) {
  body { background: #16171b; color: #e8e9ed; }
  h2, th, td, footer { border-color: #2c2e36; }
  .meta, th, .cite, footer { color: #9aa0ae; }
  .review { background: #2a2410; border-color: #4a3f18; }
}
@media print { body { max-width: none; margin: 0; } .review { border: 1px solid #999; } }
"""


def to_html(
    minutes: Minutes, transcript: Transcript | None = None, duration: float | None = None
) -> str:
    cite = _citer(transcript, wrap='<span class="cite">{}</span>')
    esc = html.escape
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(minutes.title or 'Meeting minutes')}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{esc(minutes.title or 'Meeting minutes')}</h1>",
    ]

    meta = [esc(minutes.held_on)] if minutes.held_on else []
    if duration is not None:
        meta.append(esc(format_duration(duration)))
    if minutes.attendees:
        meta.append(esc(", ".join(minutes.attendees)))
    if meta:
        parts.append(f'<p class="meta">{" · ".join(meta)}</p>')

    if minutes.summary:
        parts.append(f"<h2>Summary</h2><p>{esc(minutes.summary)}</p>")

    if minutes.topics:
        parts.append("<h2>Discussion</h2>")
        for topic in minutes.topics:
            parts.append(f"<h3>{esc(topic.title)} {cite(topic.segment_index)}</h3>")
            if topic.points:
                items = "".join(f"<li>{esc(p)}</li>" for p in topic.points)
                parts.append(f"<ul>{items}</ul>")

    if minutes.decisions:
        rows = "".join(
            f"<li>{esc(d.text)} {cite(d.segment_index)}</li>" for d in minutes.decisions
        )
        parts.append(f"<h2>Decisions</h2><ol>{rows}</ol>")

    parts.append("<h2>Action points</h2>")
    if minutes.actions:
        action_rows: list[str] = []
        for i, item in enumerate(minutes.actions, start=1):
            owner = (
                esc(item.owner)
                if item.owner
                else '<span class="unassigned">unassigned</span>'
            )
            klass = ' class="done"' if item.status == "done" else ""
            action_rows.append(
                f"<tr{klass}><td>{i}</td><td>{esc(item.text)} {cite(item.segment_index)}</td>"
                f"<td>{owner}</td><td>{esc(item.due) or '—'}</td></tr>"
            )
        parts.append(
            "<table><thead><tr><th>#</th><th>Action</th><th>Owner</th><th>Due</th></tr></thead>"
            f"<tbody>{''.join(action_rows)}</tbody></table>"
        )
    else:
        parts.append("<p><em>None recorded.</em></p>")

    if minutes.open_questions:
        items = "".join(f"<li>{esc(q)}</li>" for q in minutes.open_questions)
        parts.append(f"<h2>Open questions</h2><ul>{items}</ul>")

    if minutes.review:
        items = "".join(
            f"<li>{esc(item.text)}"
            + (f" — {esc(item.owner)}" if item.owner else "")
            + f" {cite(item.segment_index)}</li>"
            for item in minutes.review
        )
        parts.append(
            '<h2>Possible actions (needs review)</h2><div class="review">'
            "<p>Detected but not confidently a commitment — confirm before circulating.</p>"
            f"<ul>{items}</ul></div>"
        )

    parts.append(f"<footer>{esc(_footer(minutes))}</footer></body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Shared bits
# --------------------------------------------------------------------------


def action_table_rows(actions: list[ActionItem]) -> list[dict[str, Any]]:
    """Flat rows for the UI and for `minutely actions`."""
    return [
        {
            "id": item.action_id,
            "text": item.text,
            "owner": item.owner,
            "due": item.due,
            "status": item.status,
            "certain": item.confidence >= CERTAIN,
        }
        for item in actions
    ]


def _citer(transcript: Transcript | None, wrap: str = "{}") -> Callable[[int | None], str]:
    """Build a function that turns a segment index into a timestamp marker."""

    def cite(index: int | None) -> str:
        if transcript is None or index is None:
            return ""
        marker = transcript.cited(index)
        return f" {wrap.format(marker)}" if marker else ""

    return cite


def _footer(minutes: Minutes) -> str:
    engine = {"rules": "offline rules engine", "claude": "Claude"}.get(
        minutes.engine, minutes.engine or "unknown engine"
    )
    stamp = minutes.generated_at.replace("T", " ").replace("+00:00", " UTC")
    return f"Drafted by minutely ({engine}) on {stamp}. Check it before you send it."


def _escape_pipes(text: str) -> str:
    return text.replace("|", "\\|")


def _wrap(text: str, width: int = 78, subsequent: str = "") -> str:
    import textwrap

    return "\n".join(
        textwrap.wrap(text, width=width, subsequent_indent=subsequent) or [text]
    )
