"""SQLite-backed storage adapter for hermes domain objects.

This adapter persists email threads, calendar events, and provider sync cursors.
Objects are stored as pickled payloads with indexed scalar timestamp fields for
efficient lookups by recency and time window.
"""

from __future__ import annotations

import pickle
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hermes.config import settings
from hermes.ports.calendar import Event
from hermes.ports.email import EmailThread


class SQLiteStore:
    """SQLite implementation of the `StoragePort` contract."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Create a store connected to `db_path`, then ensure schema exists."""
        if db_path is None:
            db_path = settings.db_path
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._enable_pragmas()
        self._init_schema()

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _enable_pragmas(self) -> None:
        """Enable SQLite settings for local durability and integrity."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        self._conn.commit()

    def _init_schema(self) -> None:
        """Create required tables and indexes when they do not yet exist."""
        cur = self._conn.cursor()

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
        """Serialize a datetime to ISO 8601 text for SQLite storage."""
        return dt.isoformat()

    # -------------------------------------------------------------------------
    # Email storage
    # -------------------------------------------------------------------------

    def save_threads(self, threads: list[EmailThread]) -> None:
        """Upsert thread payloads by `thread.id`."""
        if not threads:
            return

        rows = []
        for thread in threads:
            payload = pickle.dumps(thread, protocol=pickle.HIGHEST_PROTOCOL)
            last_updated = self._dt_to_iso(thread.last_updated)
            rows.append((thread.id, payload, last_updated))

        with self._conn:
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
        """Return up to `limit` threads ordered by newest `last_updated` first."""
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
        """Return one thread by id, or `None` when no row exists."""
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
        """Delete one thread by id."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM gmail_threads WHERE thread_id = ?",
                (thread_id,),
            )

    # -------------------------------------------------------------------------
    # Event storage
    # -------------------------------------------------------------------------

    def save_events(self, events: list[Event]) -> None:
        """Upsert event payloads by `event.id`."""
        if not events:
            return

        rows = []
        for event in events:
            payload = pickle.dumps(event, protocol=pickle.HIGHEST_PROTOCOL)
            start_time = self._dt_to_iso(event.start)
            end_time = self._dt_to_iso(event.end)
            rows.append((event.id, payload, start_time, end_time))

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

    def get_events_between(self, start: datetime, end: datetime) -> list[Event]:
        """Return events with `start_time` in the `[start, end)` window."""
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
        """Return one event by id, or `None` when no row exists."""
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
        """Delete one event by id."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM gcal_events WHERE event_id = ?",
                (event_id,),
            )

    # -------------------------------------------------------------------------
    # Sync cursors
    # -------------------------------------------------------------------------

    def get_cursor(self, provider: str) -> str | None:
        """Return the stored sync cursor for `provider`, if present."""
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
        """Upsert the sync cursor for `provider`."""
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

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
