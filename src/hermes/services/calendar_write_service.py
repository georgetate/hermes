from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence

from hermes.config import Settings, get_settings
from hermes.ports.calendar import (
    Attendee,
    CalendarWritePort,
    Reminder,
)
from hermes.ports.llm import Tool


@dataclass(slots=True)
class CalendarWriteService:
    """Write-side calendar orchestration for LLM-facing create-event flows."""

    calendar_port: CalendarWritePort
    settings: Settings = field(default_factory=get_settings)

    def create_event(
        self,
        *,
        calendar_id: str | None = None,
        title: str,
        start: str,
        end: str,
        all_day: bool = False,
        timezone_name: str | None = None,
        location: str | None = None,
        description: str | None = None,
        attendee_emails: Sequence[str] | None = None,
        reminder_minutes: Sequence[int] | None = None,
        has_conference_link: bool | None = None,
    ) -> dict[str, object]:
        """Create one calendar event and return a compact event summary."""

        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Event title is required.")

        resolved_calendar_id = calendar_id or self.settings.gcal_calendar_id
        resolved_timezone_name = timezone_name or self.settings.timezone
        start_dt = self._parse_event_boundary(
            start,
            timezone_name=resolved_timezone_name,
            default_timezone_name=self.settings.timezone,
            all_day=all_day,
            is_end=False,
        )
        end_dt = self._parse_event_boundary(
            end,
            timezone_name=resolved_timezone_name,
            default_timezone_name=self.settings.timezone,
            all_day=all_day,
            is_end=True,
        )

        # For all-day events, callers often provide the same date for start/end.
        # Normalize that common case into a one-day exclusive-end window.
        if all_day and end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1)

        if end_dt <= start_dt:
            raise ValueError("Event end must be after the start.")

        attendees = self._as_attendees(attendee_emails)
        reminders = self._as_reminders(reminder_minutes)
        event = self.calendar_port._build_new_event(
            title=normalized_title,
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            timezone=resolved_timezone_name,
            location=location,
            description=description,
            attendees=attendees,
            reminders=reminders,
            has_conference_link=has_conference_link,
        )
        event_id = self.calendar_port.create_new_event(resolved_calendar_id, event)

        return {
            "event_id": event_id,
            "calendar_id": resolved_calendar_id,
            "title": event.title,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "timezone": event.timezone,
            "location": event.location,
            "description_preview": self._trim_text(event.description or "", 600)
            if event.description
            else None,
            "attendee_emails": [attendee.email for attendee in event.attendees or []],
            "reminder_minutes": [
                reminder.minutes_before_start for reminder in event.reminders or []
            ],
            "has_conference_link": event.has_conference_link,
        }

    def handle_create_event(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `create_event`."""

        calendar_id = self._as_str(arguments.get("calendar_id"))
        title = self._as_str(arguments.get("title"))
        start = self._as_str(arguments.get("start"))
        end = self._as_str(arguments.get("end"))
        if title is None:
            raise ValueError("title is required to create a calendar event.")
        if start is None:
            raise ValueError("start is required to create a calendar event.")
        if end is None:
            raise ValueError("end is required to create a calendar event.")

        return self.create_event(
            calendar_id=self._normalize_calendar_id(calendar_id),
            title=title,
            start=start,
            end=end,
            all_day=self._as_bool(arguments.get("all_day"), default=False),
            timezone_name=self._as_str(arguments.get("timezone")),
            location=self._as_str(arguments.get("location")),
            description=self._as_str(arguments.get("description")),
            attendee_emails=self._as_str_list(arguments.get("attendee_emails")),
            reminder_minutes=self._as_int_list(arguments.get("reminder_minutes")),
            has_conference_link=self._as_optional_bool(
                arguments.get("has_conference_link")
            ),
        )

    def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        """Delete one calendar event and return a compact confirmation payload."""

        normalized_event_id = event_id.strip()
        if not normalized_event_id:
            raise ValueError("event_id is required to delete a calendar event.")

        resolved_calendar_id = self._normalize_calendar_id(calendar_id)
        self.calendar_port.delete_event(
            resolved_calendar_id,
            normalized_event_id,
        )
        return {
            "deleted": True,
            "event_id": normalized_event_id,
            "calendar_id": resolved_calendar_id,
        }

    def handle_delete_event(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `delete_event`."""

        event_id = self._as_str(arguments.get("event_id"))
        if event_id is None:
            raise ValueError("event_id is required to delete a calendar event.")

        return self.delete_event(
            event_id=event_id,
            calendar_id=self._as_str(arguments.get("calendar_id")),
        )

    @staticmethod
    def create_event_tool() -> Tool:
        """Return the tool definition for creating a calendar event."""

        return Tool(
            name="create_calendar_event",
            description="Create one calendar event. Omit calendar_id to use the primary calendar.",
            input_schema={
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id. Omit to use the primary calendar.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Event title.",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start as ISO-8601 datetime or YYYY-MM-DD using today's date context.",
                    },
                    "end": {
                        "type": "string",
                        "description": "End as ISO-8601 datetime or YYYY-MM-DD using today's date context.",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "Whether this is an all-day event.",
                        "default": False,
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone like America/Denver.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional event location.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional event description or notes.",
                    },
                    "attendee_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email addresses to invite.",
                    },
                    "reminder_minutes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Reminder offsets in minutes before the start.",
                    },
                    "has_conference_link": {
                        "type": "boolean",
                        "description": "Whether to request a conference link if supported.",
                    },
                },
                "required": ["title", "start", "end"],
                "additionalProperties": False,
            },
        )

    @staticmethod
    def delete_event_tool() -> Tool:
        """Return the tool definition for deleting a calendar event."""

        return Tool(
            name="delete_calendar_event",
            description="Delete one calendar event by event id.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The event id to delete.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id. Omit to use the primary calendar.",
                    },
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
            requires_confirmation=True,
        )

    @staticmethod
    def _parse_event_boundary(
        value: str,
        *,
        timezone_name: str | None,
        default_timezone_name: str | None,
        all_day: bool,
        is_end: bool,
    ) -> datetime:
        """Parse a tool-provided event boundary into an aware datetime."""

        parsed = CalendarWriteService._parse_datetime(value)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            if all_day:
                target_date = parsed + timedelta(days=1) if is_end else parsed
                return CalendarWriteService._attach_timezone(
                    datetime.combine(target_date, time.min),
                    timezone_name,
                    default_timezone_name,
                )
            return CalendarWriteService._attach_timezone(
                datetime.combine(parsed, time.min),
                timezone_name,
                default_timezone_name,
            )
        return CalendarWriteService._attach_timezone(
            parsed,
            timezone_name,
            default_timezone_name,
        )

    @staticmethod
    def _attach_timezone(
        value: datetime,
        timezone_name: str | None,
        default_timezone_name: str | None,
    ) -> datetime:
        """Ensure a datetime is timezone-aware, defaulting to app local time."""

        if value.tzinfo is not None:
            return value
        try:
            from zoneinfo import ZoneInfo

            resolved_timezone_name = timezone_name or default_timezone_name
            return value.replace(
                tzinfo=(
                    ZoneInfo(resolved_timezone_name)
                    if resolved_timezone_name is not None
                    else timezone.utc
                )
            )
        except Exception:
            return value.replace(tzinfo=timezone.utc)

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
    def _as_attendees(values: Sequence[str] | None) -> Sequence[Attendee] | None:
        """Convert attendee email strings into calendar attendee DTOs."""

        if not values:
            return None
        attendees = [
            Attendee(name=None, email=value.strip())
            for value in values
            if value.strip()
        ]
        return attendees or None

    @staticmethod
    def _as_reminders(values: Sequence[int] | None) -> Sequence[Reminder] | None:
        """Convert integer minute offsets into reminder DTOs."""

        if not values:
            return None
        reminders = [
            Reminder(minutes_before_start=value)
            for value in values
            if value >= 0
        ]
        return reminders or None

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

        parsed = CalendarWriteService._as_optional_bool(value)
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
    def _as_int_list(value: object) -> list[int] | None:
        """Coerce a raw value to a list of integers."""

        if value is None:
            return None
        if isinstance(value, (int, str)):
            parsed = CalendarWriteService._as_optional_int(value)
            return [parsed] if parsed is not None else None
        if isinstance(value, Sequence):
            items: list[int] = []
            for item in value:
                parsed = CalendarWriteService._as_optional_int(item)
                if parsed is not None:
                    items.append(parsed)
            return items or None
        return None

    def _normalize_calendar_id(self, calendar_id: str | None) -> str:
        """Resolve a missing calendar id to the configured default calendar."""

        return calendar_id or self.settings.gcal_calendar_id

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        """Collapse whitespace and trim text to a bounded size."""

        normalized = " ".join(text.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max_chars]
        return normalized[: max_chars - 3].rstrip() + "..."
