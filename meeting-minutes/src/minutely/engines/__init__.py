"""Minutes engines: transcript in, :class:`Minutes` out.

Two implementations ship:

* :mod:`minutely.engines.rules` — offline, deterministic, no network, no
  dependencies. It is the default because a meeting transcript is private and
  the obvious thing to do with private data is nothing.
* :mod:`minutely.engines.claude` — sends the transcript to the Anthropic API
  for a materially better summary. Opt in per run with ``--engine claude``.

Both satisfy :class:`MinutesEngine`, so ``minutely minutes`` does not care
which one it is holding. Both also take the notes the user typed during the
meeting: those are the highest-signal text in the system, and an engine's job
is to enhance them rather than to compete with them.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import Minutes, Transcript
from ..templates import Template


class EngineError(RuntimeError):
    """Raised when an engine cannot produce minutes."""


class MinutesEngine(Protocol):
    """The one thing an engine does."""

    name: str

    def summarise(
        self,
        transcript: Transcript,
        *,
        title: str = "",
        held_on: date | None = None,
        meeting_id: str = "",
        notes: str = "",
        template: Template | None = None,
    ) -> Minutes: ...


__all__ = ["EngineError", "MinutesEngine"]
