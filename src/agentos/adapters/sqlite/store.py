# adapters/sqlite/store.py

from __future__ import annotations

import sqlite3
import pickle
from dataclasses import asdict  # not strictly needed, but handy if you extend
from datetime import datetime
from pathlib import Path
from typing import Sequence
from datetime import datetime, timezone

from agentos.ports.storage import StoragePort
from agentos.ports.email import EmailThread
from agentos.ports.calendar import Event


class SQLiteStore:
    """
    SQLite-backed implementation of StoragePort.

    - Stores EmailThread and Event objects pickled in BLOB columns.
    - Adds simple scalar columns (timestamps) for efficient querying.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._enable_pragmas()
        self._init_schema()

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _enable_pragmas(self) -> None:
        cur = self._conn.cursor()
        # Better durability & concurrency for a local single-user app
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        self._conn.commit()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()

        # Emails (threads)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_threads (
                thread_id     TEXT PRIMARY KEY,
                payload       BLOB NOT NULL,
                last_updated  TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gmail_threads_last_updated
                ON gmail_threads(last_updated DESC)
            """
        )

        # Events
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gcal_events (
                event_id    TEXT PRIMARY KEY,
                payload     BLOB NOT NULL,
                start_time  TEXT NOT NULL,
                end_time    TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gcal_events_start_time
                ON gcal_events(start_time)
            """
        )

        # Sync cursors (gmail historyId, gcal syncToken, etc.)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                provider   TEXT PRIMARY KEY,
                cursor     TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        self._conn.commit()

    @staticmethod
    def _dt_to_iso(dt: datetime) -> str:
        return dt.isoformat()

    # -------------------------------------------------------------------------
    # EMAIL STORAGE
    # -------------------------------------------------------------------------

    def save_threads(self, threads: list[EmailThread]) -> None:
        if not threads:
            return

        rows = []
        for t in threads:
            # We store the full object pickled, plus a scalar for querying
            payload = pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL)
            last_updated = self._dt_to_iso(t.last_updated)
            rows.append((t.id, payload, last_updated))

        with self._conn:  # atomic transaction
            self._conn.executemany(
                """
                INSERT INTO gmail_threads (thread_id, payload, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    payload      = excluded.payload,
                    last_updated = excluded.last_updated
                """,
                rows,
            )

    def get_recent_threads(self, limit: int) -> list[EmailThread]:
        cur = self._conn.execute(
            """
            SELECT payload
              FROM gmail_threads
             ORDER BY last_updated DESC
             LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [pickle.loads(row["payload"]) for row in rows]

    def get_thread(self, thread_id: str) -> EmailThread | None:
        cur = self._conn.execute(
            """
            SELECT payload
              FROM gmail_threads
             WHERE thread_id = ?
            """,
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return pickle.loads(row["payload"])

    def delete_thread(self, thread_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM gmail_threads WHERE thread_id = ?",
                (thread_id,),
            )

    # -------------------------------------------------------------------------
    # EVENT STORAGE
    # -------------------------------------------------------------------------

    def save_events(self, events: list[Event]) -> None:
        if not events:
            return

        rows = []
        for ev in events:
            payload = pickle.dumps(ev, protocol=pickle.HIGHEST_PROTOCOL)

            # Assumes your Event dataclass has .start and .end as datetime
            start_time = self._dt_to_iso(ev.start)
            end_time = self._dt_to_iso(ev.end)

            rows.append((ev.id, payload, start_time, end_time))

        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO gcal_events (event_id, payload, start_time, end_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload    = excluded.payload,
                    start_time = excluded.start_time,
                    end_time   = excluded.end_time
                """,
                rows,
            )

    def get_events_between(
        self, start: datetime, end: datetime
    ) -> list[Event]:
        start_iso = self._dt_to_iso(start)
        end_iso = self._dt_to_iso(end)

        cur = self._conn.execute(
            """
            SELECT payload
              FROM gcal_events
             WHERE start_time >= ?
               AND start_time < ?
             ORDER BY start_time
            """,
            (start_iso, end_iso),
        )
        rows = cur.fetchall()
        return [pickle.loads(row["payload"]) for row in rows]

    def get_event(self, event_id: str) -> Event | None:
        cur = self._conn.execute(
            """
            SELECT payload
              FROM gcal_events
             WHERE event_id = ?
            """,
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return pickle.loads(row["payload"])

    def delete_event(self, event_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM gcal_events WHERE event_id = ?",
                (event_id,),
            )

    # -------------------------------------------------------------------------
    # SYNC CURSORS
    # -------------------------------------------------------------------------

    def get_cursor(self, provider: str) -> str | None:
        cur = self._conn.execute(
            """
            SELECT cursor
              FROM sync_state
             WHERE provider = ?
            """,
            (provider,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["cursor"]

    def save_cursor(self, provider: str, cursor: str) -> None:
        # change import

        # replace selection
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sync_state (provider, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    cursor     = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (provider, cursor, now_iso),
            )

    # -------------------------------------------------------------------------
    # Optional: cleanup
    # -------------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
