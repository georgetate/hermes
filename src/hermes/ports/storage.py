"""Storage port contract for persisted hermes domain data."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from hermes.ports.calendar import Event
from hermes.ports.email import EmailThread


class StoragePort(Protocol):
    """Provider-agnostic persistence interface used by hermes services.

    Implementations should treat IDs as upsert keys, return `None` for missing
    objects, and persist cursors per provider namespace.
    """

    # ----- Email storage -----
    def save_threads(self, threads: list[EmailThread]) -> None:
        """Upsert email threads by `thread.id`."""
        ...

    def get_recent_threads(self, limit: int) -> list[EmailThread]:
        """Return up to `limit` most-recent threads in descending recency order."""
        ...

    def get_thread(self, thread_id: str) -> EmailThread | None:
        """Return one thread by id, or `None` when not found."""
        ...

    def delete_thread(self, thread_id: str) -> None:
        """Delete one thread by id. Missing ids should be treated as a no-op."""
        ...

    # ----- Event storage -----
    def save_events(self, events: list[Event]) -> None:
        """Upsert calendar events by `event.id`."""
        ...

    def get_events_between(self, start: datetime, end: datetime) -> list[Event]:
        """Return events for the requested `[start, end)` window."""
        ...

    def get_event(self, event_id: str) -> Event | None:
        """Return one event by id, or `None` when not found."""
        ...

    def delete_event(self, event_id: str) -> None:
        """Delete one event by id. Missing ids should be treated as a no-op."""
        ...

    # ----- Sync cursors -----
    def get_cursor(self, provider: str) -> str | None:
        """Return the persisted sync cursor for `provider`, or `None` if absent."""
        ...

    def save_cursor(self, provider: str, cursor: str) -> None:
        """Upsert the sync cursor for `provider`."""
        ...
