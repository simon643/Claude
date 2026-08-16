"""Configuration and on-disk locations.

Everything this app writes lives under one directory so it is trivial to back
up or delete. Nothing sensitive is ever written into the repository itself.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "xerobk"

# Xero's OAuth 2.0 endpoints.
XERO_AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"

# Read-only by default. Write scopes are only requested when the user opts in
# with `--allow-writes`, so an accidental run can never mutate the ledger.
#
# A read-write scope already grants read access, so the two variants are
# alternatives, not additions. Requesting `accounting.transactions` alongside
# `accounting.transactions.read` is rejected by Xero's consent screen with
# `invalid_scope` — the write scope REPLACES its read twin rather than joining
# it. `accounting.settings.read` stays read-only in both modes because nothing
# here ever writes organisation settings.
BASE_SCOPES = (
    "offline_access",
    "accounting.reports.read",
    "accounting.settings.read",
)
READ_SCOPES = (
    *BASE_SCOPES,
    "accounting.transactions.read",
    "accounting.contacts.read",
)
WRITE_SCOPES = (
    *BASE_SCOPES,
    "accounting.transactions",
    "accounting.contacts",
)


def data_dir() -> Path:
    """Base directory for tokens, cache, and the local database."""
    override = os.environ.get("XEROBK_HOME")
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
    for sub in (base, base / "snapshots", base / "exports"):
        sub.mkdir(parents=True, exist_ok=True)
        # Financial data and OAuth tokens live here; keep other local users out.
        # chmod is a no-op on Windows and raises on some network filesystems, so
        # a failure to tighten permissions must not stop the app from starting.
        with suppress(OSError):
            sub.chmod(0o700)
    return base


def db_path() -> Path:
    return data_dir() / "xerobk.sqlite3"


def token_path() -> Path:
    return data_dir() / "token.json"


def config_path() -> Path:
    return data_dir() / "config.json"


@dataclass
class Settings:
    """User-adjustable settings, persisted as JSON.

    Credentials are deliberately NOT persisted here. ``client_id`` is read from
    the ``XERO_CLIENT_ID`` environment variable at runtime so it never lands in
    a file that might be synced or committed.
    """

    allow_writes: bool = False
    # Bank account code that payments and bank transactions post against.
    # Xero rejects both without one, so `xerobk post` refuses to run until it
    # is set rather than guessing which account the money moved through.
    bank_account_code: str = ""
    gst_registered: bool = True
    bas_frequency: str = "quarterly"  # quarterly | monthly | annual
    aged_debt_escalation_days: int = 60
    large_transaction_threshold: str = "10000.00"
    suspense_account_codes: list[str] = field(default_factory=lambda: ["877", "999"])
    ui_port: int = 0  # 0 = pick a free ephemeral port

    @property
    def client_id(self) -> str:
        return os.environ.get("XERO_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        return os.environ.get("XERO_CLIENT_SECRET", "").strip()

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id)

    def scopes(self) -> tuple[str, ...]:
        return WRITE_SCOPES if self.allow_writes else READ_SCOPES

    @classmethod
    def load(cls) -> Settings:
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> Path:
        ensure_dirs()
        path = config_path()
        payload = {k: getattr(self, k) for k in self.__dataclass_fields__}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with suppress(OSError):
            path.chmod(0o600)
        return path
