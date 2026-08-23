"""Microsoft Teams integration.

Teams already records and transcribes meetings, server-side, with the speaker
labels this app otherwise cannot get. Re-recording a Teams call locally would
be worse on every axis — audio quality, battery, consent, and the fact that
nobody in the room can tell your laptop is listening — so this integration does
not do that. It signs in as you, finds your Teams meetings, and pulls the
transcript Teams already produced, then runs it through the same pipeline as
anything else.

What it cannot do: start a recording. Nothing in Microsoft Graph can press the
Record button for you; a participant does that in the Teams client, or an
administrator sets a compliance-recording policy. If a meeting was never
recorded and transcribed, there is nothing here to pull — capture the audio
locally instead (`minutely record`).

Modules:

* :mod:`~minutely.teams.transport` — the one place urllib is called.
* :mod:`~minutely.teams.auth` — device code sign-in and token refresh.
* :mod:`~minutely.teams.graph` — a small Microsoft Graph client.
* :mod:`~minutely.teams.sync` — listing meetings and pulling them in.
"""

from __future__ import annotations


class TeamsError(RuntimeError):
    """Anything that stops the Teams integration doing its job."""


class TeamsAuthError(TeamsError):
    """Not signed in, or the sign-in is no longer good."""


__all__ = ["TeamsAuthError", "TeamsError"]
