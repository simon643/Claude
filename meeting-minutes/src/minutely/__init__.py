"""minutely — record a meeting, transcribe it, and write the minutes.

The pipeline is three separable stages, each usable on its own:

    audio  ->  transcript  ->  minutes

Recording happens in the browser (`minutely record`), transcription shells out
to a local whisper binary or accepts a transcript you already have, and the
minutes engine turns a transcript into a summary, decisions, and owned action
points. Nothing leaves the machine unless you explicitly choose the Claude
engine.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
