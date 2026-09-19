"""Configuration and on-disk locations.

Everything this app writes lives under one directory so it is trivial to back
up, audit, or delete. Nothing sensitive is ever written into the repository.

The approval matrix, the user directory and the budget are deliberately NOT
part of this file. They are versionable JSON documents that an organisation
will want in source control and change-reviewed — a delegation of authority is
a governance artefact, not an application preference.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

APP_NAME = "apflow"

XERO_AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"

# Read-only by default. Write scopes are requested only when the user opts in
# with `--allow-writes`, so an accidental run can never authorise a bill.
#
# A read-write scope already grants read access, so the two variants are
# alternatives, not additions: requesting `accounting.transactions` alongside
# `accounting.transactions.read` is rejected by Xero's consent screen with
# `invalid_scope`.
BASE_SCOPES = (
    "offline_access",
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
    "accounting.contacts.read",
)

# How a failed budget check is treated.
BUDGET_OFF = "off"
BUDGET_WARN = "warn"
BUDGET_BLOCK = "block"


def data_dir() -> Path:
    """Base directory for tokens, the local database, and exports."""
    override = os.environ.get("APFLOW_HOME")
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
    for sub in (base, base / "exports", base / "outbox"):
        sub.mkdir(parents=True, exist_ok=True)
        # Approval evidence and OAuth tokens live here; keep other local users
        # out. chmod is a no-op on Windows and raises on some network
        # filesystems, so a failure to tighten must not stop the app starting.
        with suppress(OSError):
            sub.chmod(0o700)
    return base


def db_path() -> Path:
    return data_dir() / "apflow.sqlite3"


def token_path() -> Path:
    return data_dir() / "token.json"


def config_path() -> Path:
    return data_dir() / "config.json"


def outbox_dir() -> Path:
    return data_dir() / "outbox"


@dataclass
class Settings:
    """User-adjustable settings, persisted as JSON.

    Credentials are deliberately not persisted. ``client_id`` is read from the
    environment at runtime so it never lands in a file that might be synced.
    """

    allow_writes: bool = False

    # Governance controls. These are the settings an auditor will ask about, so
    # each one is explicit rather than implied by code.
    separation_of_duties: bool = True
    """Whoever submits a document cannot also approve it."""

    block_supplier_self_approval: bool = True
    """An approver whose email matches the supplier contact cannot approve."""

    enforce_approval_limits: bool = True
    """An approver may not clear a document above their delegated limit."""

    budget_enforcement: str = BUDGET_WARN  # off | warn | block

    reminder_after_days: int = 2
    """Quiet chase for a step that has been open this long."""

    escalation_after_days: int = 5
    """Past this, the reminder also names the approver's escalation contact."""

    matrix_file: str = ""
    users_file: str = ""
    budget_file: str = ""

    # SMTP is opt-in and credential-free by default: without a host configured,
    # `apflow remind` writes .eml files to the outbox and sends nothing.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_starttls: bool = True

    ui_port: int = 0  # 0 = pick a free ephemeral port

    @property
    def client_id(self) -> str:
        return os.environ.get("XERO_CLIENT_ID", "").strip()

    @property
    def smtp_password(self) -> str:
        return os.environ.get("APFLOW_SMTP_PASSWORD", "")

    @property
    def smtp_user(self) -> str:
        return os.environ.get("APFLOW_SMTP_USER", "").strip()

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id)

    @property
    def can_send_mail(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

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
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> Path:
        ensure_dirs()
        path = config_path()
        payload = {k: getattr(self, k) for k in self.__dataclass_fields__}
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        with suppress(OSError):
            path.chmod(0o600)
        return path


def governance_path(settings: Settings, kind: str, explicit: str | None = None) -> Path:
    """Where to read the matrix / users / budget from.

    Resolution order: an explicit ``--matrix``-style flag, then the path stored
    in settings, then the data directory. A missing file is not an error here —
    callers decide whether the absence is fatal.
    """
    if explicit:
        return Path(explicit).expanduser()
    configured = {"matrix": settings.matrix_file, "users": settings.users_file,
                  "budget": settings.budget_file}.get(kind, "")
    if configured:
        return Path(configured).expanduser()
    return data_dir() / f"{kind}.json"


def decimal_or_none(value: object) -> Decimal | None:
    """Parse an optional monetary bound from config (``null`` means unlimited)."""
    if value is None or value == "":
        return None
    from .money import to_money

    return to_money(value)
