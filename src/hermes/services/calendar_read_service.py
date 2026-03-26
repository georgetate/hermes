from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence

from hermes.ports.calendar import (
    Attendee,
    CalendarRef,
    CalendarReadPort,
    Event,
    EventFilter,
    EventSummary,
    Reminder,
)
from hermes.ports.llm import Tool


@dataclass(slots=True)
class CalendarReadService:
    """Read-side calendar orchestration for LLM-facing tools."""

    calendar_port: CalendarReadPort
    default_window_days: int = 14

    def list_calendars(self) -> dict[str, object]:
        """Return readable calendars available to the assistant."""

        calendars = self.calendar_port.list_calendars()
        return {
            "returned_count": len(calendars),
            "calendars": [
                self._serialize_calendar_ref(calendar)
                for calendar in calendars
            ],
        }

    def handle_list_calendars(self, arguments: dict[str, object]) -> dict[str, object]:
        """Ignore raw tool arguments and return the available calendars."""

        del arguments
        return self.list_calendars()

    def summarize_calendar(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        days_ahead: int | None = None,
        calendar_ids: Sequence[str] | None = None,
        title_contains: str | None = None,
        attendee_contains: str | None = None,
        has_conference_link: bool | None = None,
        include_cancelled: bool = False,
        expand_instances: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Fetch calendar event summaries for a bounded time window."""

        start, end = self._resolve_window(
            from_date=from_date,
            to_date=to_date,
            days_ahead=days_ahead,
        )
        normalized_limit = max(1, min(100, limit))

        event_filter = EventFilter(
            title_contains=title_contains,
            attendee_contains=attendee_contains,
            has_conference_link=has_conference_link,
            free_text=None,
        )

        page = self.calendar_port.list_events(
            start=start,
            end=end,
            calendar_ids=calendar_ids,
            include_cancelled=include_cancelled,
            expand="instances" if expand_instances else "none",
            filters=event_filter,
            limit=normalized_limit,
            cursor=cursor,
        )

        return {
            "filters": {
                "from_date": start.isoformat(),
                "to_date": end.isoformat(),
                "days_ahead": days_ahead,
                "calendar_ids": list(calendar_ids) if calendar_ids else [],
                "title_contains": title_contains,
                "attendee_contains": attendee_contains,
                "has_conference_link": has_conference_link,
                "include_cancelled": include_cancelled,
                "expand_instances": expand_instances,
                "limit": normalized_limit,
            },
            "returned_count": len(page.items),
            "next_cursor": page.next_cursor,
            "events": [self._serialize_event_summary(event) for event in page.items],
        }

    def handle_summarize_calendar(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `summarize_calendar`."""

        return self.summarize_calendar(
            from_date=self._as_str(arguments.get("from_date")),
            to_date=self._as_str(arguments.get("to_date")),
            days_ahead=self._as_optional_int(arguments.get("days_ahead")),
            calendar_ids=self._as_str_list(arguments.get("calendar_ids")),
            title_contains=self._as_str(arguments.get("title_contains")),
            attendee_contains=self._as_str(arguments.get("attendee_contains")),
            has_conference_link=self._as_optional_bool(
                arguments.get("has_conference_link")
            ),
            include_cancelled=self._as_bool(
                arguments.get("include_cancelled"),
                default=False,
            ),
            expand_instances=self._as_bool(
                arguments.get("expand_instances"),
                default=False,
            ),
            limit=self._as_int(arguments.get("limit"), default=50),
            cursor=self._as_str(arguments.get("cursor")),
        )

    def read_calendar_event(
        self,
        *,
        event_id: str,
        calendar_id: str,
        max_description_chars: int = 1200,
    ) -> dict[str, object]:
        """Fetch one event and return a trimmed, JSON-friendly payload."""

        event = self.calendar_port.get_event(event_id, calendar_id)
        normalized_max = max(200, min(3000, max_description_chars))
        return {
            "event": self._serialize_full_event(
                event,
                max_description_chars=normalized_max,
            ),
            "max_description_chars": normalized_max,
        }

    def handle_read_calendar_event(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `read_calendar_event`."""

        event_id = self._as_str(arguments.get("event_id"))
        calendar_id = self._as_str(arguments.get("calendar_id"))
        if event_id is None:
            raise ValueError("event_id is required to read a calendar event.")
        if calendar_id is None:
            raise ValueError("calendar_id is required to read a calendar event.")

        return self.read_calendar_event(
            event_id=event_id,
            calendar_id=calendar_id,
            max_description_chars=self._as_int(
                arguments.get("max_description_chars"),
                default=1200,
            ),
        )

    @staticmethod
    def summarize_calendar_tool() -> Tool:
        """Return the tool definition exposed to the language model."""

        return Tool(
            name="summarize_calendar",
            description="List calendar events in a time window with lightweight summaries.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Inclusive start date in YYYY-MM-DD or ISO-8601 format.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Exclusive end date in YYYY-MM-DD or ISO-8601 format.",
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "If no to_date is given, search this many days ahead.",
                        "minimum": 1,
                        "maximum": 60,
                    },
                    "calendar_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific calendars to search.",
                    },
                    "title_contains": {
                        "type": "string",
                        "description": "Match text in the event title.",
                    },
                    "attendee_contains": {
                        "type": "string",
                        "description": "Match attendee email or name.",
                    },
                    "has_conference_link": {
                        "type": "boolean",
                        "description": "Filter based on conference link presence.",
                    },
                    "include_cancelled": {
                        "type": "boolean",
                        "description": "Include cancelled events.",
                        "default": False,
                    },
                    "expand_instances": {
                        "type": "boolean",
                        "description": "Expand recurring series into concrete instances.",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to inspect.",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque cursor returned by a previous call.",
                    },
                },
            },
        )

    @staticmethod
    def read_calendar_event_tool() -> Tool:
        """Return the tool definition for reading one calendar event."""

        return Tool(
            name="read_calendar_event",
            description="Read one calendar event by event id and calendar id.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The event id to retrieve.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "The calendar containing the event.",
                    },
                    "max_description_chars": {
                        "type": "integer",
                        "description": "Max characters to keep from the event description.",
                        "minimum": 200,
                        "maximum": 3000,
                        "default": 1200,
                    },
                },
                "required": ["event_id", "calendar_id"],
            },
        )

    @staticmethod
    def list_calendars_tool() -> Tool:
        """Return the tool definition for listing available calendars."""

        return Tool(
            name="list_calendars",
            description="List readable calendars with ids and names.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    def _resolve_window(
        self,
        *,
        from_date: str | None,
        to_date: str | None,
        days_ahead: int | None,
    ) -> tuple[datetime, datetime]:
        """Resolve the requested time window into aware datetimes."""

        start = self._parse_start_datetime(from_date) or self._start_of_today()
        if to_date is not None:
            end = self._parse_end_datetime(to_date)
        else:
            window_days = days_ahead or self.default_window_days
            end = start + timedelta(days=max(1, min(60, window_days)))

        if end <= start:
            raise ValueError("Calendar window end must be after the start.")
        return start, end

    @staticmethod
    def _serialize_event_summary(event: EventSummary) -> dict[str, object]:
        """Convert an event summary into a JSON-friendly dict."""

        return {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "title": event.title,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "last_updated": event.last_updated.isoformat() if event.last_updated else None,
            "is_recurring_series": event.is_recurring_series,
            "series_id": event.series_id,
            "has_conference_link": event.has_conference_link,
            "status": event.status,
        }

    @staticmethod
    def _serialize_calendar_ref(calendar: CalendarRef) -> dict[str, object]:
        """Convert a calendar reference into a JSON-friendly dict."""

        return {
            "id": calendar.id,
            "name": calendar.name,
            "timezone": calendar.timezone,
            "is_primary": calendar.is_primary,
        }

    @staticmethod
    def _serialize_full_event(
        event: Event,
        *,
        max_description_chars: int,
    ) -> dict[str, object]:
        """Convert a full event into a trimmed JSON-friendly dict."""

        return {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "title": event.title,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "timezone": event.timezone,
            "location": event.location,
            "description": CalendarReadService._trim_text(
                event.description or "",
                max_description_chars,
            )
            if event.description
            else None,
            "attendees": [
                CalendarReadService._serialize_attendee(attendee)
                for attendee in event.attendees
            ],
            "reminders": [
                CalendarReadService._serialize_reminder(reminder)
                for reminder in event.reminders
            ],
            "last_updated": event.last_updated.isoformat() if event.last_updated else None,
            "has_conference_link": event.has_conference_link,
            "series_id": event.series_id,
            "status": event.status,
        }

    @staticmethod
    def _serialize_attendee(attendee: Attendee) -> dict[str, object]:
        """Convert an attendee into a JSON-friendly dict."""

        return {
            "name": attendee.name,
            "email": attendee.email,
            "optional": attendee.optional,
            "response_status": attendee.response_status,
        }

    @staticmethod
    def _serialize_reminder(reminder: Reminder) -> dict[str, object]:
        """Convert a reminder into a JSON-friendly dict."""

        return {
            "minutes_before_start": reminder.minutes_before_start,
            "method": reminder.method,
        }

    @staticmethod
    def _start_of_today() -> datetime:
        """Return midnight UTC for the current day."""

        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _parse_start_datetime(value: str | None) -> datetime | None:
        """Parse a lower-bound timestamp, expanding bare dates to midnight UTC."""

        if value is None:
            return None
        parsed = CalendarReadService._parse_datetime(value)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            return datetime.combine(parsed, time.min, tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _parse_end_datetime(value: str) -> datetime:
        """Parse an upper-bound timestamp, treating bare dates as exclusive next day."""

        parsed = CalendarReadService._parse_datetime(value)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            next_day = parsed + timedelta(days=1)
            return datetime.combine(next_day, time.min, tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _parse_datetime(value: str) -> datetime | date:
        """Parse either an ISO timestamp or a plain ISO date string."""

        normalized = value.strip()
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            try:
                return date.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid date value '{value}'. Use YYYY-MM-DD or ISO-8601."
                ) from exc

    @staticmethod
    def _as_optional_bool(value: object) -> bool | None:
        """Coerce a raw value to an optional boolean."""

        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return None

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        """Coerce a raw value to a boolean with a default fallback."""

        parsed = CalendarReadService._as_optional_bool(value)
        return default if parsed is None else parsed

    @staticmethod
    def _as_optional_int(value: object) -> int | None:
        """Coerce a raw value to an optional integer."""

        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _as_int(value: object, *, default: int) -> int:
        """Coerce a raw value to an integer, ignoring invalid inputs."""

        parsed = CalendarReadService._as_optional_int(value)
        return default if parsed is None else parsed

    @staticmethod
    def _as_str(value: object) -> str | None:
        """Coerce a raw value to a stripped string or `None`."""

        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return str(value)

    @staticmethod
    def _as_str_list(value: object) -> list[str] | None:
        """Coerce a raw value to a list of non-empty strings."""

        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else None
        if isinstance(value, Sequence):
            items: list[str] = []
            for item in value:
                if isinstance(item, str):
                    stripped = item.strip()
                    if stripped:
                        items.append(stripped)
            return items or None
        return None

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        """Collapse whitespace and trim text to a bounded size."""

        normalized = " ".join(text.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max_chars]
        return normalized[: max_chars - 3].rstrip() + "..."
