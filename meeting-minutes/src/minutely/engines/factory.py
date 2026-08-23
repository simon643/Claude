"""Pick an engine by name."""

from __future__ import annotations

from ..config import Settings
from . import EngineError, MinutesEngine
from .claude import ClaudeEngine
from .rules import RulesEngine

ENGINES = ("rules", "claude")


def get_engine(name: str | None = None, settings: Settings | None = None) -> MinutesEngine:
    settings = settings or Settings.load()
    chosen = (name or settings.engine or "rules").strip().lower()
    if chosen == "rules":
        return RulesEngine(non_owners=settings.non_owners)
    if chosen == "claude":
        return ClaudeEngine(model=settings.model)
    raise EngineError(f"unknown engine {chosen!r} (choose from: {', '.join(ENGINES)})")
