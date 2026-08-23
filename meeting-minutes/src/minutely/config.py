"""Configuration and on-disk locations.

Everything this app writes — recordings, transcripts, the database — lives
under one directory so it is trivial to back up, move, or delete. A meeting
recording is a room full of people's voices; keeping it in one predictable
place is what makes "delete all of it" a thing the user can actually do.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "minutely"

# Microsoft identity platform, for the Teams integration. These are the public
# endpoints, not credentials.
MS_AUTHORITY = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Delegated scopes, least-privilege first. Calendars.Read finds the meetings,
# OnlineMeetings.Read turns a join URL into a meeting id, and the transcript
# scope reads what Teams already wrote. The recording scope is deliberately
# NOT in the default set: pulling a transcript needs no access to anybody's
# video, so asking for it by default would be asking for more than the job
# needs — `minutely teams login --with-recordings` opts in.
TEAMS_SCOPES = (
    "offline_access",
    "User.Read",
    "Calendars.Read",
    "OnlineMeetings.Read",
    "OnlineMeetingTranscript.Read.All",
)
TEAMS_RECORDING_SCOPE = "OnlineMeetingRecording.Read.All"

# Recording containers the browser can produce and whisper can read.
AUDIO_SUFFIXES = frozenset({".webm", ".ogg", ".oga", ".m4a", ".mp4", ".mp3", ".wav", ".flac"})
TRANSCRIPT_SUFFIXES = frozenset({".vtt", ".srt", ".txt", ".md", ".json"})


def data_dir() -> Path:
    """Base directory for recordings, transcripts, and the local database."""
    override = os.environ.get("MINUTELY_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def ensure_dirs() -> Path:
    """Create the data directory tree with owner-only permissions."""
    base = data_dir()
    for sub in (base, recordings_dir(), transcripts_dir(), exports_dir()):
        sub.mkdir(parents=True, exist_ok=True)
        # Other users on the machine have no business reading meeting audio.
        # chmod is a no-op on Windows and raises on some network filesystems,
        # so failing to tighten permissions must not stop the app.
        with suppress(OSError):
            sub.chmod(0o700)
    return base


def recordings_dir() -> Path:
    return data_dir() / "recordings"


def transcripts_dir() -> Path:
    return data_dir() / "transcripts"


def exports_dir() -> Path:
    return data_dir() / "exports"


def teams_token_path() -> Path:
    return data_dir() / "teams-token.json"


def db_path() -> Path:
    return data_dir() / "minutely.sqlite3"


def config_path() -> Path:
    return data_dir() / "config.json"


@dataclass
class Settings:
    """User-adjustable settings, persisted as JSON.

    No API key is stored here. The Claude engine reads its credentials from the
    environment the way every other Anthropic tool does, so a key never lands
    in a file that might be synced or committed.
    """

    # "rules" is offline and deterministic; "claude" calls the Anthropic API.
    engine: str = "rules"
    # "whisper" shells out to a local binary; "none" means transcripts are
    # supplied by hand (`minutely import` on a .vtt/.srt/.txt file).
    transcriber: str = "whisper"
    whisper_bin: str = "whisper-cli"
    whisper_model: str = ""
    language: str = "en"
    model: str = "claude-opus-5"
    # Names that should never be treated as an action-item owner, because they
    # are the organisation rather than a person ("the team will follow up").
    non_owners: tuple[str, ...] = ("team", "we", "everyone", "all", "group")
    ui_port: int = 0  # 0 = pick a free ephemeral port
    # Microsoft Entra application (client) id for the Teams integration. This
    # is a public identifier, not a secret — every user of a public client app
    # ships the same one — so unlike an API key it is fine on disk. The
    # environment variable still wins, for anyone who would rather not.
    teams_client_id: str = ""
    # "organizations" covers work and school accounts, which are the only ones
    # the Teams meeting APIs support. A tenant GUID narrows it further.
    teams_tenant: str = "organizations"
    # Automatically run transcribe + minutes when a browser recording stops.
    auto_process: bool = True

    @property
    def has_api_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    @property
    def client_id(self) -> str:
        return os.environ.get("MINUTELY_TEAMS_CLIENT_ID", "").strip() or self.teams_client_id.strip()

    @classmethod
    def load(cls) -> Settings:
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in raw.items() if k in known}
        if "non_owners" in values:
            values["non_owners"] = tuple(values["non_owners"])
        return cls(**values)

    def save(self) -> Path:
        ensure_dirs()
        path = config_path()
        payload = {k: getattr(self, k) for k in self.__dataclass_fields__}
        payload["non_owners"] = list(self.non_owners)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with suppress(OSError):
            path.chmod(0o600)
        return path
