"""Provider-agnostic calendar domain types and port contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence, Generic, TypeVar, Literal

T = TypeVar("T")

# ---------- Paging ----------

@dataclass(frozen=True)
class Page(Generic[T]):
    """Generic page of results."""
    items: Sequence[T]
    next_cursor: Optional[str] = None
    total: Optional[int] = None
    next_sync_token: Optional[str] = None  # for incremental sync

# ---------- Core DTOs ----------

@dataclass(frozen=True)
class CalendarRef:
    """A user-visible calendar container (e.g., 'primary', 'Birthdays', 'Work')."""
    id: str
    name: str
    timezone: Optional[str] = None      # IANA tz, e.g., "America/Denver"
    is_primary: bool = False

@dataclass(frozen=True)
class Attendee:
    """Event participant details."""
    name: Optional[str]
    email: str
    optional: bool = False
    response_status: Optional[str] = None  # "accepted" | "declined" | "tentative" | "needsAction" | None

@dataclass(frozen=True)
class Reminder:
    """Relative reminder; adapters map method names to provider capabilities."""
    minutes_before_start: int
    method: Optional[str] = None  # e.g., "popup", "email"

@dataclass(frozen=True)
class Recurrence:
    """
    RFC 5545-inspired rule (keep flexible, provider-agnostic).
    - freq: DAILY | WEEKLY | MONTHLY | YEARLY
    - interval: every N freq units
    - byweekday: ["MO","TU","WE","TH","FR","SA","SU"]
    - bymonthday: e.g., [1, 15, -1]
    - count: number of occurrences (alternatively use until)
    - until: last occurrence boundary in event's timezone
    - tzid: timezone for evaluating the rule (IANA)
    """
    freq: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
    interval: int = 1
    byweekday: Optional[Sequence[str]] = None
    bymonthday: Optional[Sequence[int]] = None
    count: Optional[int] = None
    until: Optional[datetime] = None  # timezone-aware
    tzid: Optional[str] = None

@dataclass(frozen=True)
class EventSummary:
    """
    Lightweight row for list views.
    If expand="none" and this represents a recurring series, `is_recurring_series=True`,
    `recurrence` is populated, and `start`/`end` SHOULD represent the first occurrence
    within the requested window (adapter-resolved).
    If expand="instances", each item is a concrete instance with `series_id` set.
    """
    id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    last_updated: Optional[datetime]
    is_recurring_series: bool
    series_id: Optional[str]  # For instances: id of the series master; for masters: equal to id or None by adapter policy
    recurrence: Optional[Recurrence] = None
    has_conference_link: Optional[bool] = None
    status: Optional[str] = None  # (e.g. "confirmed", "cancelled")


@dataclass(frozen=True)
class Event:
    """
    Full event. For instances of a recurring series, `series_id` references the master.
    All datetime fields MUST be timezone-aware.
    For all-day events, adapters should set start at 00:00 and end at next-day 00:00
    in the event timezone (exclusive end), with all_day=True.
    """
    id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    timezone: Optional[str]  # IANA tz where the event is defined
    location: Optional[str]
    description: Optional[str]
    attendees: Sequence[Attendee]
    reminders: Sequence[Reminder]
    last_updated: Optional[datetime]
    has_conference_link: Optional[bool] = None
    recurrence: Optional[Recurrence] = None
    series_id: Optional[str] = None     # present if this is an instance of a series
    status: Optional[str] = None


@dataclass(frozen=True)
class NewEvent:
    """
    Input for creating a new event (one-off or recurring).
    `calendar_id` is provided to create_event() separately to avoid duplication.
    """
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    timezone: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    attendees: Optional[Sequence[Attendee]] = None
    reminders: Optional[Sequence[Reminder]] = None
    has_conference_link: Optional[bool] = None
    recurrence: Optional[Recurrence] = None


# ---------- Filters & Modes ----------

ExpandMode = Literal["none", "instances"]

@dataclass(frozen=True)
class EventFilter:
    """
    Optional filters; adapters translate into provider queries.
    """
    title_contains: Optional[str] = None
    attendee_contains: Optional[str] = None
    has_conference_link: Optional[bool] = None
    free_text: Optional[str] = None  # Escape hatch for provider-specific search


# ---------- Outbound Calendar Port ----------

class CalendarPort(Protocol):
    """
    Provider-agnostic calendar gateway.

    Listing behavior:
      - Always bounded by a TimeRange window.
      - By default (expand='none'), recurring series appear as ONE row each if they
        have any occurrence within the window. `start`/`end` should reflect the first
        occurrence in-window (adapter responsibility).
      - With expand='instances', return each concrete occurrence within the window.
    """

    # --- Discovery ---

    def list_calendars(self) -> Sequence[CalendarRef]:
        """Enumerate available calendars the user can read/write."""
        raise NotImplementedError
    
    # --- Syncs ---

    def sync_events(
        self,
        *,
        calendar_id: str,
        sync_token: str,
        include_cancelled: bool,
        filters: Optional[EventFilter],
    ) -> Page[EventSummary]:
        """
        Incremental sync from a prior sync_token.
        Returns a page of changed event summaries and a new sync_token.
        """
        raise NotImplementedError
    
    def full_sync_events(
        self,
        *,
        calendar_id: str,
        include_cancelled: bool,
        expand: ExpandMode = 'none',
        filters: Optional[EventFilter],
    ) -> Page[EventSummary]:
        """
        Initial full sync (no time bounds, no syncToken; single calendar). Always exhausts pages.
        """
        raise NotImplementedError

    # --- Reads ---

    def list_events(
        self,
        start: datetime,
        end: datetime,
        *,
        calendar_ids: Optional[Sequence[str]] = None,
        include_cancelled: bool = False,
        expand: ExpandMode = "none",
        filters: Optional[EventFilter] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Page[EventSummary]:
        """
        Return events (summaries) overlapping the window.
        - If expand='none': one row per recurring series.
        - If expand='instances': one row per concrete occurrence.
        Pagination is provider-agnostic via `cursor`.
        """
        raise NotImplementedError

    def get_event(self, event_id: str, calendar_id: str) -> Event:
        """
        Fetch a single event by id. If this id is an instance occurrence, return
        the concrete instance with `series_id` set to the master.
        """
        raise NotImplementedError
    
    # --- Convenience (windowed expansion) ---

    def find_between(
        self,
        start: datetime,
        end: datetime,
        *,
        calendar_ids: Optional[Sequence[str]] = None,
        include_cancelled: bool = False,
    ) -> Sequence[Event]:
        """
        Convenience wrapper: return fully realized Event instances overlapping the window
        (adapters may internally call list_events(expand='instances') + get_event or an optimized API).
        """
        raise NotImplementedError
    
    # --- Writes ---
    def _build_new_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool = False,
        timezone: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        attendees: Optional[Sequence[Attendee]] = None,
        reminders: Optional[Sequence[Reminder]] = None,
        has_conference_link: Optional[bool] = None,
        recurrence: Optional[Recurrence] = None,
    ) -> NewEvent:
        """
        Factory method for constructing a provider-agnostic `NewEvent` DTO.
        Adapters should normalize timezone awareness and safely populate optional fields.
        """
        raise NotImplementedError

    def create_event(self, calendar_id: str, event: NewEvent) -> str:
        """
        Create a one-off or recurring event under the specified calendar.
        Returns the created event id (series master id if recurring).
        """
        raise NotImplementedError

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """
        Delete a single event (or recurring master) by id.
        Implementations should propagate provider-specific deletion behavior.
        """
        raise NotImplementedError
    
    def delete_all_after(
        self,
        calendar_id: str,
        master_event_id: str,
        cutoff_start: datetime,
        *,
        send_updates: bool = True,
    ) -> None:
        """
        Delete a selected recurring instance and all following instances.
        """
        raise NotImplementedError
    
