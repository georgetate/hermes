# src/hermes/adapters/sqlite/store.py

from __future__ import annotations

import sqlite3
import pickle
from dataclasses import asdict  # not strictly needed, but handy if extending
from datetime import datetime
from pathlib import Path
from datetime import datetime, timezone

from hermes.config import settings
from hermes.ports.email import EmailThread
from hermes.ports.calendar import Event


class SQLiteStore:
    """
    SQLite-backed implementation of StoragePort.

    - Stores EmailThread and Event objects pickled in BLOB columns.
    - Adds simple scalar columns (timestamps) for efficient querying.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
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
        """Enables write ahead logging (WAL) and foreign key support. WAL allows for better durability and concurrency in a local single-user app. Foreign keys prevents data integrity issues that could arise later
        """
        cur = self._conn.cursor()
        # Better durability & concurrency for a local single-user app
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        self._conn.commit()

    def _init_schema(self) -> None:
        """
        Initializes the SQLite database schema.
        """
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
        """Converts a datetime to an ISO 8601 string. Assumes the datetime is timezone-aware. If it's naive, it will be treated as UTC.
        """
        return dt.isoformat()

    # -------------------------------------------------------------------------
    # EMAIL STORAGE
    # -------------------------------------------------------------------------

    def save_threads(self, threads: list[EmailThread]) -> None:
        """
        Saves a list of EmailThread objects to the database. 
        
        If a thread with the same ID already exists, it will be updated/replaced. Uses pickle to preserve custom python objects. Also stores a separate last_updated timestamp for efficient sorting and retrieval of recent threads.

        Returns:
            None: Updates the database in-place.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being pickled.
        """
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
        """
        Retrieves a list of EmailThread objects from the database, sorted by last_updated timestamp in newest-first order.

        Returns:
            list[EmailThread]: The list of EmailThread objects retrieved from the database.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being unpickled.
        """
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
        """
        Retrieves a single EmailThread object from the database by thread_id.

        Returns:
            list[EmailThread]: The list of EmailThread objects retrieved from the database.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being unpickled.
        """
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
        """
        Removes a single EmailThread object from the database by thread_id.

        Returns:
            None: Updates the database in-place.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being deleted.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM gmail_threads WHERE thread_id = ?",
                (thread_id,),
            )

    # -------------------------------------------------------------------------
    # EVENT STORAGE
    # -------------------------------------------------------------------------

    def save_events(self, events: list[Event]) -> None:
        """
        Saves a list of Event objects to the database. 
        
        If an event with the same ID already exists, it will be updated/replaced. Uses pickle to preserve custom python objects. Also stores a separate start_time and end_time timestamp for efficient sorting and retrieval of recent events.

        Returns:
            None: Updates the database in-place.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being pickled.
        """
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
        """
        Retrieves a list of Event objects from the database, sorted by start_time timestamp in oldest-first order.

        Returns:
            list[Event]: The list of Event objects retrieved from the database.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being unpickled.
        """
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
        """
        Retrieves a single Event object from the database by event_id.

        Returns:
            Event: The Event object retrieved from the database, or None if not found.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the data being unpickled.
        """
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
        """
        Retrieves the sync cursor for a given provider from the database.

        Args:
            provider (str): The provider name for which to retrieve the sync cursor. Eg: "gmail" or "gcal".

        Returns:
            str: The sync cursor string for the given provider, or None if not found.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the cursor being retrieved.
        """ 
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
        """
        Saves a sync cursor for a given provider to the database.

        Args:
            provider (str): The provider name.
            cursor (str): The sync cursor string to save.

        Returns:
            None: Updates the database in-place.

        Raises:
            Exception: Doesn't raise errors directly, but underlying sqlite3 exceptions may occur if the database file is inaccessible or if there are issues with the cursor string being saved.
        """ 
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
