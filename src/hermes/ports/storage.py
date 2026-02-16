from typing import Protocol
from datetime import datetime
from hermes.ports.calendar import Event
from hermes.ports.email import EmailThread


class StoragePort(Protocol):

    # ----- EMAIL STORAGE -----
    def save_threads(self, threads: list[EmailThread]) -> None: ...
    """Saves threads to storage. If a thread with the same ID already exists, it should be updated/replaced."""

    def get_recent_threads(self, limit: int) -> list[EmailThread]: ...
    """Returns the most recently saved threads, up to `limit` threads."""

    def get_thread(self, thread_id: str) -> EmailThread | None: ...
    """Returns the specified thread from storage using its thread_id."""


    def delete_thread(self, thread_id: str) -> None: ...
    """Removes the specified thread from storage using its thread_id."""

    # ----- EVENT STORAGE -----
    def save_events(self, events: list[Event]) -> None: ...
    """Saves events to storage. If an event with the same ID already exists, it should be updated/replaced."""

    def get_events_between(self, start: datetime, end: datetime) -> list[Event]: ...
    """Returns all events that start or end between the specified start and end datetimes."""

    def get_event(self, event_id: str) -> Event | None: ...
    """Returns the specified event from storage using its event_id."""

    def delete_event(self, event_id: str) -> None: ...
    """Removes the specified event from storage using its event_id."""

    # ----- SYNC CURSORS -----
    def get_cursor(self, provider: str) -> str | None: ...
    """Returns the sync cursor for the specified provider, or None if no cursor is stored."""

    def save_cursor(self, provider: str, cursor: str) -> None: ...
    """Saves the sync cursor for the specified provider. If a cursor for that provider already exists, it should be updated/replaced.
    """
