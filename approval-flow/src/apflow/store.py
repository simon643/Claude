"""Local SQLite store.

This is the system of record for approvals. The ledger holds the document; this
holds the evidence that someone with authority agreed to it, and what they were
looking at when they did.

Three design rules, all of them audit-driven:

* **Decisions are append-only.** There is no UPDATE on the ``decisions`` table.
  A change of mind is a new row, not an edited one. Step and request state are
  recomputed from decisions rather than being the truth themselves, so a
  materialised state that ever disagreed with the decision log would be a bug
  in the materialiser, not a lost audit trail.
* **Every decision records the fingerprint it was made against.** That is what
  makes "approved, then edited, then posted" detectable rather than deniable.
* **Writes to Xero are logged before they are sent.** If a POST succeeds and
  the process dies before recording it, an after-the-fact log loses the only
  local record of a change that now exists in the ledger.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import db_path, ensure_dirs
from .models import Decision, Document, RequestState, StepState
from .money import ZERO, to_money

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reference      TEXT NOT NULL DEFAULT '',
    document_id    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    number         TEXT NOT NULL DEFAULT '',
    contact_name   TEXT NOT NULL DEFAULT '',
    contact_email  TEXT NOT NULL DEFAULT '',
    currency       TEXT NOT NULL DEFAULT 'AUD',
    amount         TEXT NOT NULL DEFAULT '0.00',
    base_amount    TEXT NOT NULL DEFAULT '0.00',
    document_date  TEXT,
    due_date       TEXT,
    fingerprint    TEXT NOT NULL,
    document       TEXT NOT NULL DEFAULT '{}',
    rule_name      TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'pending',
    requested_by   TEXT NOT NULL DEFAULT '',
    budget_status  TEXT NOT NULL DEFAULT 'not_checked',
    budget_detail  TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    closed_at      TEXT,
    pushed_at      TEXT,
    push_ref       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state);
CREATE INDEX IF NOT EXISTS idx_requests_doc ON requests(document_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_reference ON requests(reference)
    WHERE reference != '';

-- One live request per document. A rejected or cancelled request leaves the
-- document free to be resubmitted; a pending one must not be duplicated, or
-- two approvers could each clear a different copy of the same spend.
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_open_doc ON requests(document_id)
    WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS steps (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    quorum         TEXT NOT NULL DEFAULT 'any',
    state          TEXT NOT NULL DEFAULT 'pending',
    sla_days       INTEGER NOT NULL DEFAULT 0,
    opened_at      TEXT,
    closed_at      TEXT,
    note           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_steps_request ON steps(request_id, position);

CREATE TABLE IF NOT EXISTS step_approvers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id        INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    user_id        TEXT NOT NULL,
    delegated_from TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_step_approvers_step ON step_approvers(step_id);
CREATE INDEX IF NOT EXISTS idx_step_approvers_user ON step_approvers(user_id);

CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    step_id        INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    user_id        TEXT NOT NULL,
    acting_for     TEXT NOT NULL DEFAULT '',
    decision       TEXT NOT NULL,
    comment        TEXT NOT NULL DEFAULT '',
    fingerprint    TEXT NOT NULL,
    amount         TEXT NOT NULL DEFAULT '0.00',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_request ON decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER REFERENCES requests(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id, id);

CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER REFERENCES requests(id) ON DELETE CASCADE,
    step_id        INTEGER,
    user_id        TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL,
    subject        TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    sent_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_request ON notifications(request_id, kind);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER,
    action         TEXT NOT NULL,
    target         TEXT NOT NULL DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}',
    outcome        TEXT NOT NULL DEFAULT 'pending',
    detail         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class StoreError(RuntimeError):
    """A local database failure."""


class Store:
    """Thin wrapper over the SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            ensure_dirs()
            path = db_path()
        self.path = Path(path)
        if str(self.path) != ":memory:" and self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the UI server handles requests on a
        # thread pool, so the connection outlives the thread that opened it.
        # Python's sqlite3 only permits that if every use is serialised, which
        # `self._lock` does — sharing it without the lock is the bug this
        # pairing exists to prevent.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
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
        except sqlite3.IntegrityError as exc:
            raise StoreError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise StoreError(f"local database error: {exc}") from exc

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"local database error: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- requests ---------------------------------------------------------

    def create_request(
        self,
        document: Document,
        rule_name: str,
        requested_by: str,
        steps: list[dict[str, Any]],
        budget_status: str = "not_checked",
        budget_detail: dict[str, Any] | None = None,
    ) -> int:
        """Insert a request and its steps in one transaction.

        A half-written request — steps missing, or a request row with nobody
        attached to decide it — is worse than no request at all, so the whole
        shape goes in atomically or not at all.
        """
        stamp = now_iso()
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO requests
                    (document_id, kind, number, contact_name, contact_email, currency,
                     amount, base_amount, document_date, due_date, fingerprint, document,
                     rule_name, state, requested_by, budget_status, budget_detail,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    document.document_id,
                    str(document.kind),
                    document.number,
                    document.contact.name,
                    document.contact.email or "",
                    document.currency_code,
                    str(document.amount_total),
                    str(document.base_amount),
                    document.document_date.isoformat() if document.document_date else None,
                    document.due_date.isoformat() if document.due_date else None,
                    document.fingerprint,
                    json.dumps(document.as_dict()),
                    rule_name,
                    str(RequestState.PENDING),
                    requested_by,
                    budget_status,
                    json.dumps(budget_detail or {}, default=str),
                    stamp,
                    stamp,
                ),
            )
            request_id = int(cursor.lastrowid or 0)
            conn.execute(
                "UPDATE requests SET reference=? WHERE id=?", (f"AR-{request_id:04d}", request_id)
            )
            for index, step in enumerate(steps):
                # Only the first step opens; the rest wait their turn, which is
                # what makes the workflow sequential rather than a free-for-all.
                opened = stamp if index == 0 else None
                step_cursor = conn.execute(
                    """
                    INSERT INTO steps (request_id, position, name, quorum, state, sla_days, opened_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        request_id,
                        int(step["position"]),
                        str(step["name"]),
                        str(step.get("quorum") or "any"),
                        str(StepState.PENDING),
                        int(step.get("sla_days") or 0),
                        opened,
                    ),
                )
                step_id = int(step_cursor.lastrowid or 0)
                for approver in step.get("approvers") or []:
                    conn.execute(
                        "INSERT INTO step_approvers (step_id, user_id, delegated_from) VALUES (?,?,?)",
                        (step_id, str(approver["user_id"]), str(approver.get("delegated_from") or "")),
                    )
        return request_id

    def request_row(self, request_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM requests WHERE id=?", (request_id,))
        return dict(rows[0]) if rows else None

    def resolve_reference(self, handle: str) -> int | None:
        """Accept ``AR-0007``, ``7``, or a raw document id."""
        text = str(handle).strip()
        if not text:
            return None
        rows = self._query("SELECT id FROM requests WHERE reference=?", (text.upper(),))
        if rows:
            return int(rows[0]["id"])
        if text.isdigit():
            rows = self._query("SELECT id FROM requests WHERE id=?", (int(text),))
            if rows:
                return int(rows[0]["id"])
        rows = self._query(
            "SELECT id FROM requests WHERE document_id=? ORDER BY id DESC LIMIT 1", (text,)
        )
        if rows:
            return int(rows[0]["id"])
        rows = self._query(
            "SELECT id FROM requests WHERE number=? ORDER BY id DESC LIMIT 1", (text,)
        )
        return int(rows[0]["id"]) if rows else None

    def open_request_for_document(self, document_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM requests WHERE document_id=? AND state=? LIMIT 1",
            (document_id, str(RequestState.PENDING)),
        )
        return dict(rows[0]) if rows else None

    def latest_request_for_document(self, document_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM requests WHERE document_id=? ORDER BY id DESC LIMIT 1", (document_id,)
        )
        return dict(rows[0]) if rows else None

    def list_requests(
        self,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 500,
        pushed: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if pushed is True:
            clauses.append("pushed_at IS NOT NULL")
        elif pushed is False:
            clauses.append("pushed_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        )
        return [dict(row) for row in rows]

    def set_request_state(
        self, request_id: int, state: RequestState | str, closed: bool | None = None
    ) -> None:
        text = str(state)
        close_it = closed if closed is not None else text != str(RequestState.PENDING)
        with self._tx() as conn:
            conn.execute(
                "UPDATE requests SET state=?, updated_at=?, closed_at=? WHERE id=?",
                (text, now_iso(), now_iso() if close_it else None, request_id),
            )

    def update_document(self, request_id: int, document: Document) -> None:
        """Replace the stored document after a re-pull found it changed."""
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE requests SET
                    number=?, contact_name=?, contact_email=?, currency=?, amount=?,
                    base_amount=?, document_date=?, due_date=?, fingerprint=?, document=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    document.number,
                    document.contact.name,
                    document.contact.email or "",
                    document.currency_code,
                    str(document.amount_total),
                    str(document.base_amount),
                    document.document_date.isoformat() if document.document_date else None,
                    document.due_date.isoformat() if document.due_date else None,
                    document.fingerprint,
                    json.dumps(document.as_dict()),
                    now_iso(),
                    request_id,
                ),
            )

    def set_budget_result(self, request_id: int, status: str, detail: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE requests SET budget_status=?, budget_detail=?, updated_at=? WHERE id=?",
                (status, json.dumps(detail, default=str), now_iso(), request_id),
            )

    def mark_pushed(self, request_id: int, reference: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE requests SET pushed_at=?, push_ref=?, updated_at=? WHERE id=?",
                (now_iso(), reference, now_iso(), request_id),
            )

    def document_for(self, request_id: int) -> Document | None:
        rows = self._query("SELECT document FROM requests WHERE id=?", (request_id,))
        if not rows:
            return None
        try:
            return Document.from_dict(json.loads(rows[0]["document"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    # -- steps ------------------------------------------------------------

    def steps_for(self, request_id: int) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM steps WHERE request_id=? ORDER BY position", (request_id,))
        steps: list[dict[str, Any]] = []
        for row in rows:
            step = dict(row)
            step["approvers"] = [
                dict(a)
                for a in self._query(
                    "SELECT user_id, delegated_from FROM step_approvers WHERE step_id=? ORDER BY id",
                    (row["id"],),
                )
            ]
            steps.append(step)
        return steps

    def set_step_state(
        self, step_id: int, state: StepState | str, note: str = "", opened: bool = False
    ) -> None:
        text = str(state)
        closed = None if text == str(StepState.PENDING) else now_iso()
        with self._tx() as conn:
            if opened:
                conn.execute(
                    "UPDATE steps SET state=?, note=?, opened_at=COALESCE(opened_at, ?), closed_at=? "
                    "WHERE id=?",
                    (text, note, now_iso(), closed, step_id),
                )
            else:
                conn.execute(
                    "UPDATE steps SET state=?, note=?, closed_at=? WHERE id=?",
                    (text, note, closed, step_id),
                )

    def reopen_steps(self, request_id: int) -> None:
        """Send every step back to pending after a material document change."""
        stamp = now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE steps SET state=?, closed_at=NULL, note='', opened_at=NULL "
                "WHERE request_id=?",
                (str(StepState.PENDING), request_id),
            )
            conn.execute(
                "UPDATE steps SET opened_at=? WHERE request_id=? AND position="
                "(SELECT MIN(position) FROM steps WHERE request_id=?)",
                (stamp, request_id, request_id),
            )

    def pending_steps_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Open steps awaiting this user, newest request last.

        A step counts as awaiting the user only when every earlier step on the
        request has closed — otherwise an approver three steps down would see
        work that is not yet theirs.
        """
        rows = self._query(
            """
            SELECT s.id AS step_id, s.request_id, s.position, s.name, s.quorum,
                   s.sla_days, s.opened_at, r.reference, r.kind, r.number, r.contact_name,
                   r.currency, r.amount, r.base_amount, r.due_date, r.fingerprint,
                   r.requested_by, r.budget_status, r.created_at, a.delegated_from
            FROM steps s
            JOIN requests r ON r.id = s.request_id
            JOIN step_approvers a ON a.step_id = s.id
            WHERE s.state = 'pending'
              AND r.state = 'pending'
              AND LOWER(a.user_id) = LOWER(?)
              AND NOT EXISTS (
                  SELECT 1 FROM steps earlier
                  WHERE earlier.request_id = s.request_id
                    AND earlier.position < s.position
                    AND earlier.state = 'pending'
              )
            ORDER BY r.id
            """,
            (user_id,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            already = self._query(
                "SELECT decision FROM decisions WHERE step_id=? AND LOWER(user_id)=LOWER(?) "
                "AND fingerprint=? ORDER BY id DESC LIMIT 1",
                (row["step_id"], user_id, row["fingerprint"]),
            )
            # An "all" step keeps showing for colleagues who have not voted; it
            # must not keep nagging someone who already has.
            if already:
                continue
            out.append(entry)
        return out

    # -- decisions --------------------------------------------------------

    def record_decision(
        self,
        request_id: int,
        step_id: int,
        user_id: str,
        decision: Decision | str,
        fingerprint: str,
        amount: Decimal = ZERO,
        comment: str = "",
        acting_for: str = "",
    ) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO decisions
                    (request_id, step_id, user_id, acting_for, decision, comment,
                     fingerprint, amount, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    step_id,
                    user_id,
                    acting_for,
                    str(decision),
                    comment,
                    fingerprint,
                    str(amount),
                    now_iso(),
                ),
            )
            conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (now_iso(), request_id))
            return int(cursor.lastrowid or 0)

    def decisions_for(self, request_id: int, fingerprint: str | None = None) -> list[dict[str, Any]]:
        if fingerprint is None:
            rows = self._query("SELECT * FROM decisions WHERE request_id=? ORDER BY id", (request_id,))
        else:
            rows = self._query(
                "SELECT * FROM decisions WHERE request_id=? AND fingerprint=? ORDER BY id",
                (request_id, fingerprint),
            )
        return [dict(row) for row in rows]

    def all_decisions(self, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT d.*, r.reference, r.kind, r.base_amount, s.name AS step_name, s.opened_at
            FROM decisions d
            JOIN requests r ON r.id = d.request_id
            JOIN steps s ON s.id = d.step_id
            ORDER BY d.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    # -- events -----------------------------------------------------------

    def add_event(
        self,
        request_id: int | None,
        kind: str,
        actor: str = "",
        detail: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (request_id, kind, actor, detail, payload, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, kind, actor, detail, json.dumps(payload or {}, default=str), now_iso()),
            )

    def events_for(self, request_id: int) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,))
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT e.*, r.reference FROM events e
            LEFT JOIN requests r ON r.id = e.request_id
            ORDER BY e.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    # -- notifications ----------------------------------------------------

    def add_notification(
        self,
        request_id: int | None,
        user_id: str,
        email: str,
        kind: str,
        subject: str,
        body: str,
        step_id: int | None = None,
        path: str = "",
        sent: bool = False,
    ) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notifications
                    (request_id, step_id, user_id, email, kind, subject, body, path, created_at, sent_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    step_id,
                    user_id,
                    email,
                    kind,
                    subject,
                    body,
                    path,
                    now_iso(),
                    now_iso() if sent else None,
                ),
            )
            return int(cursor.lastrowid or 0)

    def mark_notification_sent(self, notification_id: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE notifications SET sent_at=? WHERE id=?", (now_iso(), notification_id)
            )

    def last_notification(self, request_id: int, step_id: int, kind: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM notifications WHERE request_id=? AND step_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (request_id, step_id, kind),
        )
        return dict(rows[0]) if rows else None

    def notifications(self, limit: int = 100, unsent_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE sent_at IS NULL" if unsent_only else ""
        rows = self._query(
            f"SELECT * FROM notifications {where} ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    # -- budget commitments ----------------------------------------------

    def commitments(self, include_pushed: bool = True) -> list[dict[str, Any]]:
        """Money already spoken for: every request not rejected or cancelled.

        Pending requests count. That is the whole point — two approvers working
        the queue at the same time must not each clear a $40,000 bill against a
        $50,000 remaining budget because neither sees the other's in-flight
        commitment.
        """
        clause = "" if include_pushed else "AND pushed_at IS NULL"
        rows = self._query(
            f"""
            SELECT id, reference, kind, number, contact_name, base_amount, document_date,
                   due_date, state, document
            FROM requests
            WHERE state IN ('pending','approved') {clause}
            ORDER BY id
            """
        )
        return [dict(row) for row in rows]

    # -- audit log --------------------------------------------------------

    def begin_write(
        self, action: str, target: str, payload: dict[str, Any], request_id: int | None = None
    ) -> int:
        """Log an intended write BEFORE it is sent. Returns the audit row id."""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (request_id, action, target, payload, outcome, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, action, target, json.dumps(payload, default=str), "pending", now_iso()),
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
            "SELECT id, request_id, action, target, outcome, detail, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    # -- aggregates -------------------------------------------------------

    def totals_by_state(self) -> dict[str, dict[str, Any]]:
        """Count and value of requests in each state.

        Summed in Python rather than with SQL's SUM: SQLite would have to CAST
        the stored text to REAL to add it, and a float total is exactly the
        thing this codebase refuses to let money become.
        """
        rows = self._query("SELECT state, base_amount FROM requests")
        totals: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = totals.setdefault(str(row["state"]), {"count": 0, "total": ZERO})
            bucket["count"] += 1
            bucket["total"] += to_money(row["base_amount"])
        return totals
