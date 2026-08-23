"""Local SQLite store.

Holds four things: the meetings, their transcripts, each generated set of
minutes, and — the part that outlives any single meeting — the action register.

Regenerating minutes is expected (a better engine, a corrected transcript), so
actions are *synced* rather than replaced: an item that already exists keeps
its id and its status. Marking something done and then re-running the engine
must not quietly reopen it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import db_path, ensure_dirs
from .models import ActionItem, Meeting, Minutes, Transcript

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    held_on         TEXT NOT NULL DEFAULT '',
    duration        REAL,
    audio_path      TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    participants    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'new',
    source          TEXT NOT NULL DEFAULT 'local',
    external_id     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_held_on ON meetings(held_on);
CREATE INDEX IF NOT EXISTS idx_meetings_external ON meetings(source, external_id);

CREATE TABLE IF NOT EXISTS transcripts (
    meeting_id  TEXT PRIMARY KEY REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,
    words       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    engine      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_minutes_meeting ON minutes(meeting_id, id DESC);

CREATE TABLE IF NOT EXISTS actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id     TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    dedupe_key     TEXT NOT NULL,
    text           TEXT NOT NULL,
    owner          TEXT NOT NULL DEFAULT '',
    due            TEXT NOT NULL DEFAULT '',
    confidence     INTEGER NOT NULL DEFAULT 2,
    segment_index  INTEGER,
    quote          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open',
    created_at     TEXT NOT NULL,
    closed_at      TEXT NOT NULL DEFAULT '',
    UNIQUE(meeting_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_owner ON actions(owner);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _migrate(cur: sqlite3.Cursor, from_version: int) -> None:
    """Bring an older database up to the current schema.

    `CREATE TABLE IF NOT EXISTS` gives a new install the current shape but
    silently leaves an existing table alone, so added columns need an explicit
    ALTER. Each step is guarded by what is actually in the table rather than by
    the version alone, so a half-applied migration can be re-run.
    """
    if from_version < 2:
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(meetings)")}
        if "source" not in columns:
            cur.execute("ALTER TABLE meetings ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
        if "external_id" not in columns:
            cur.execute("ALTER TABLE meetings ADD COLUMN external_id TEXT NOT NULL DEFAULT ''")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_meetings_external ON meetings(source, external_id)"
        )


class Store:
    """Thin wrapper over the SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            ensure_dirs()
            path = db_path()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._cursor() as cur:
            cur.executescript(_SCHEMA)
            row = cur.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                cur.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) < SCHEMA_VERSION:
                _migrate(cur, int(row["version"]))
                cur.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    # -- plumbing ---------------------------------------------------------

    @property
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self._conn
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- meetings ---------------------------------------------------------

    def upsert_meeting(self, meeting: Meeting) -> Meeting:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO meetings (meeting_id, title, held_on, duration, audio_path,
                                      transcript_path, participants, status, source,
                                      external_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                    title = excluded.title,
                    held_on = excluded.held_on,
                    duration = COALESCE(excluded.duration, meetings.duration),
                    audio_path = CASE WHEN excluded.audio_path = '' THEN meetings.audio_path
                                      ELSE excluded.audio_path END,
                    transcript_path = CASE WHEN excluded.transcript_path = ''
                                           THEN meetings.transcript_path
                                           ELSE excluded.transcript_path END,
                    participants = excluded.participants,
                    status = excluded.status,
                    source = excluded.source,
                    -- A later write that knows nothing about the origin must
                    -- not erase it, the same way it must not erase the audio.
                    external_id = CASE WHEN excluded.external_id = ''
                                       THEN meetings.external_id
                                       ELSE excluded.external_id END,
                    updated_at = excluded.updated_at
                """,
                (
                    meeting.meeting_id,
                    meeting.title,
                    meeting.held_on,
                    meeting.duration,
                    meeting.audio_path,
                    meeting.transcript_path,
                    json.dumps(meeting.participants),
                    meeting.status,
                    meeting.source,
                    meeting.external_id,
                    meeting.created_at or _now(),
                    _now(),
                ),
            )
        found = self.get_meeting(meeting.meeting_id)
        assert found is not None  # just written
        return found

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        return _meeting(row) if row else None

    def resolve_meeting(self, needle: str) -> Meeting | None:
        """Look up by exact id, then by unique id prefix, then by title match."""
        exact = self.get_meeting(needle)
        if exact:
            return exact
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM meetings WHERE meeting_id LIKE ? OR lower(title) LIKE ?"
                " ORDER BY created_at DESC",
                (f"{needle}%", f"%{needle.lower()}%"),
            ).fetchall()
        return _meeting(rows[0]) if rows else None

    def find_external(self, source: str, external_id: str) -> Meeting | None:
        """The meeting already imported for this external id, if any."""
        if not external_id:
            return None
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM meetings WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return _meeting(row) if row else None

    def list_meetings(self, limit: int = 50) -> list[Meeting]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM meetings ORDER BY COALESCE(NULLIF(held_on, ''), created_at) DESC,"
                " created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_meeting(row) for row in rows]

    def delete_meeting(self, meeting_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute("DELETE FROM actions WHERE meeting_id = ?", (meeting_id,))
            cur.execute("DELETE FROM minutes WHERE meeting_id = ?", (meeting_id,))
            cur.execute("DELETE FROM transcripts WHERE meeting_id = ?", (meeting_id,))
            cur.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
            return cur.rowcount > 0

    # -- transcripts ------------------------------------------------------

    def save_transcript(self, meeting_id: str, transcript: Transcript) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO transcripts (meeting_id, payload, words, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                    payload = excluded.payload,
                    words = excluded.words,
                    created_at = excluded.created_at
                """,
                (meeting_id, json.dumps(transcript.to_dict()), transcript.word_count, _now()),
            )

    def get_transcript(self, meeting_id: str) -> Transcript | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT payload FROM transcripts WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        if row is None:
            return None
        return Transcript.from_dict(json.loads(row["payload"]))

    # -- minutes ----------------------------------------------------------

    def save_minutes(self, minutes: Minutes) -> Minutes:
        """Persist minutes and sync their actions into the register."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO minutes (meeting_id, engine, payload, created_at) VALUES (?, ?, ?, ?)",
                (minutes.meeting_id, minutes.engine, json.dumps(minutes.to_dict()), _now()),
            )
        self.sync_actions(minutes.meeting_id, minutes.actions)
        minutes.actions = self.list_actions(meeting_id=minutes.meeting_id)
        return minutes

    def get_minutes(self, meeting_id: str) -> Minutes | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT payload FROM minutes WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
        if row is None:
            return None
        minutes = Minutes.from_dict(json.loads(row["payload"]))
        # The stored payload is a snapshot; the register is the live truth.
        tracked = self.list_actions(meeting_id=meeting_id)
        if tracked:
            minutes.actions = tracked
        return minutes

    def minutes_history(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT id, engine, created_at FROM minutes WHERE meeting_id = ? ORDER BY id DESC",
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- actions ----------------------------------------------------------

    def sync_actions(self, meeting_id: str, actions: list[ActionItem]) -> None:
        """Insert new actions, refresh known ones, keep human status decisions."""
        with self._cursor() as cur:
            for item in actions:
                cur.execute(
                    """
                    INSERT INTO actions (meeting_id, dedupe_key, text, owner, due, confidence,
                                         segment_index, quote, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    ON CONFLICT(meeting_id, dedupe_key) DO UPDATE SET
                        text = excluded.text,
                        owner = excluded.owner,
                        due = excluded.due,
                        confidence = excluded.confidence,
                        segment_index = excluded.segment_index,
                        quote = excluded.quote
                    """,
                    (
                        meeting_id,
                        item.key(),
                        item.text,
                        item.owner,
                        item.due,
                        item.confidence,
                        item.segment_index,
                        item.quote,
                        _now(),
                    ),
                )

    def list_actions(
        self,
        meeting_id: str | None = None,
        status: str | None = None,
        owner: str | None = None,
    ) -> list[ActionItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if meeting_id:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if owner:
            clauses.append("lower(owner) = ?")
            params.append(owner.lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM actions {where} ORDER BY"
                " CASE WHEN due = '' THEN 1 ELSE 0 END, due, id",
                params,
            ).fetchall()
        return [_action(row) for row in rows]

    def action_rows(
        self, status: str | None = None, owner: str | None = None
    ) -> list[dict[str, Any]]:
        """Actions with their meeting context, for the cross-meeting register."""
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("a.status = ?")
            params.append(status)
        if owner:
            clauses.append("lower(a.owner) = ?")
            params.append(owner.lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            rows = cur.execute(
                f"""
                SELECT a.*, m.title AS meeting_title, m.held_on AS meeting_held_on
                FROM actions a
                JOIN meetings m ON m.meeting_id = a.meeting_id
                {where}
                ORDER BY CASE WHEN a.due = '' THEN 1 ELSE 0 END, a.due, a.id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_action_status(self, action_id: int, status: str) -> bool:
        if status not in {"open", "done", "dropped"}:
            raise ValueError(f"unknown action status {status!r}")
        with self._cursor() as cur:
            cur.execute(
                "UPDATE actions SET status = ?, closed_at = ? WHERE id = ?",
                (status, "" if status == "open" else _now(), action_id),
            )
            return cur.rowcount > 0

    def stats(self) -> dict[str, int]:
        with self._cursor() as cur:
            meetings = cur.execute("SELECT COUNT(*) AS n FROM meetings").fetchone()["n"]
            open_actions = cur.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE status = 'open'"
            ).fetchone()["n"]
            done_actions = cur.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE status = 'done'"
            ).fetchone()["n"]
        return {"meetings": int(meetings), "open_actions": int(open_actions), "done_actions": int(done_actions)}


def _meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        meeting_id=row["meeting_id"],
        title=row["title"],
        held_on=row["held_on"],
        duration=row["duration"],
        audio_path=row["audio_path"],
        transcript_path=row["transcript_path"],
        participants=json.loads(row["participants"] or "[]"),
        status=row["status"],
        source=row["source"],
        external_id=row["external_id"],
        created_at=row["created_at"],
    )


def _action(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        text=row["text"],
        owner=row["owner"],
        due=row["due"],
        confidence=int(row["confidence"]),
        segment_index=row["segment_index"],
        quote=row["quote"],
        status=row["status"],
        action_id=int(row["id"]),
    )
