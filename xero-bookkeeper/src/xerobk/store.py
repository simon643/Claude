"""Local SQLite store.

Holds three things:

* **coding decisions** — how each bank line was actually coded. This is what
  :func:`xerobk.rules.learn_rules` learns from, and what lets a re-imported
  statement recognise lines it has already seen.
* **close runs** — a dated history of checklist results, so it is possible to
  show that a finding was present last month and is now resolved.
* **an audit log** — every write sent to Xero, recorded before it is sent.

The audit log is written ahead of the API call rather than after it. If a POST
succeeds and the process dies before recording it, an after-the-fact log would
lose the only local record of a change that exists in Xero.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .config import db_path, ensure_dirs
from .models import BankLine
from .money import to_money

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS coding_decisions (
    line_id       TEXT PRIMARY KEY,
    line_date     TEXT,
    description   TEXT NOT NULL,
    reference     TEXT NOT NULL DEFAULT '',
    amount        TEXT NOT NULL,
    account_code  TEXT NOT NULL DEFAULT '',
    tax_type      TEXT NOT NULL DEFAULT '',
    contact_name  TEXT NOT NULL DEFAULT '',
    invoice_ids   TEXT NOT NULL DEFAULT '[]',
    decided_by    TEXT NOT NULL DEFAULT 'user',
    confidence    INTEGER NOT NULL DEFAULT 0,
    posted        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coding_account ON coding_decisions(account_code);

CREATE TABLE IF NOT EXISTS close_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period_label  TEXT NOT NULL,
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    critical      INTEGER NOT NULL DEFAULT 0,
    warning       INTEGER NOT NULL DEFAULT 0,
    info          INTEGER NOT NULL DEFAULT 0,
    report        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_close_period ON close_runs(period_label);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT NOT NULL,
    target        TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '{}',
    outcome       TEXT NOT NULL DEFAULT 'pending',
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Thin wrapper over the SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            ensure_dirs()
            path = db_path()
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the UI server handles requests on a
        # thread pool, so the connection outlives the thread that opened it.
        # Python's sqlite3 only permits that if every use is serialised, which
        # `self._lock` below does — sharing the connection without the lock is
        # the bug this pairing exists to prevent.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # WAL keeps the UI's reads from blocking behind a CLI write.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, serialised across threads."""
        try:
            with self._lock, self._conn:
                yield self._conn
        except sqlite3.Error as exc:
            raise RuntimeError(f"local database error: {exc}") from exc

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """A read, serialised against concurrent writes on the same connection."""
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"local database error: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- coding decisions -------------------------------------------------

    def record_coding(
        self,
        line: BankLine,
        account_code: str = "",
        tax_type: str = "",
        contact_name: str = "",
        invoice_ids: list[str] | None = None,
        decided_by: str = "user",
        confidence: int = 0,
        posted: bool = False,
    ) -> None:
        """Save (or update) how a bank line was coded."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO coding_decisions
                    (line_id, line_date, description, reference, amount, account_code,
                     tax_type, contact_name, invoice_ids, decided_by, confidence, posted, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(line_id) DO UPDATE SET
                    account_code=excluded.account_code,
                    tax_type=excluded.tax_type,
                    contact_name=excluded.contact_name,
                    invoice_ids=excluded.invoice_ids,
                    decided_by=excluded.decided_by,
                    confidence=excluded.confidence,
                    posted=excluded.posted
                """,
                (
                    line.line_id,
                    line.date.isoformat() if line.date else None,
                    line.description,
                    line.reference,
                    str(line.amount),
                    account_code,
                    tax_type,
                    contact_name,
                    json.dumps(invoice_ids or []),
                    decided_by,
                    int(confidence),
                    int(posted),
                    _now(),
                ),
            )

    def coded_line_ids(self) -> set[str]:
        rows = self._query("SELECT line_id FROM coding_decisions")
        return {row["line_id"] for row in rows}

    def coding_history(self, limit: int = 5000) -> list[tuple[BankLine, str]]:
        """Past decisions as ``(line, account_code)`` pairs for rule learning."""
        rows = self._query(
            """
            SELECT line_id, line_date, description, reference, amount, account_code
            FROM coding_decisions
            WHERE account_code != ''
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

        history: list[tuple[BankLine, str]] = []
        for row in rows:
            when: date | None = None
            if row["line_date"]:
                try:
                    when = date.fromisoformat(row["line_date"])
                except ValueError:
                    when = None
            history.append(
                (
                    BankLine(
                        line_id=row["line_id"],
                        date=when,
                        description=row["description"],
                        amount=to_money(row["amount"]),
                        reference=row["reference"] or "",
                    ),
                    row["account_code"],
                )
            )
        return history

    def account_usage(self) -> list[tuple[str, int]]:
        """Account codes ordered by how often they have been used."""
        rows = self._query(
            """
            SELECT account_code, COUNT(*) AS uses
            FROM coding_decisions
            WHERE account_code != ''
            GROUP BY account_code
            ORDER BY uses DESC
            """
        )
        return [(row["account_code"], row["uses"]) for row in rows]

    # -- close runs -------------------------------------------------------

    def record_close(self, report: dict[str, Any]) -> int:
        period = report.get("period") or {}
        counts = report.get("counts") or {}
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO close_runs
                    (period_label, period_start, period_end, as_of, critical, warning, info, report, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(period.get("label") or ""),
                    str(period.get("start") or ""),
                    str(period.get("end") or ""),
                    str(report.get("as_of") or ""),
                    int(counts.get("critical") or 0),
                    int(counts.get("warning") or 0),
                    int(counts.get("info") or 0),
                    json.dumps(report),
                    _now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def close_history(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT id, period_label, as_of, critical, warning, info, created_at
            FROM close_runs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def previous_close(self, period_label: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT report FROM close_runs WHERE period_label=? ORDER BY id DESC LIMIT 1",
            (period_label,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            return json.loads(row["report"])  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return None

    # -- audit log --------------------------------------------------------

    def begin_write(self, action: str, target: str, payload: dict[str, Any]) -> int:
        """Log an intended write BEFORE it is sent. Returns the audit row id."""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (action, target, payload, outcome, created_at) VALUES (?,?,?,?,?)",
                (action, target, json.dumps(payload, default=str), "pending", _now()),
            )
            return int(cursor.lastrowid or 0)

    def finish_write(self, audit_id: int, outcome: str, detail: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE audit_log SET outcome=?, detail=? WHERE id=?",
                (outcome, detail[:2000], audit_id),
            )

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, action, target, outcome, detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]
