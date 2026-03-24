from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from hermes.ports.email import EmailReadPort, EmailThreadFilter, EmailThreadSummary
from hermes.ports.llm import Tool


@dataclass(slots=True)
class EmailReadService:
    """Read-side email orchestration for LLM-facing summary tools."""

    email_port: EmailReadPort

    def summarize_emails(
        self,
        *,
        unread_only: bool | None = None,
        limit: int = 50,
        from_date: str | None = None,
        to_date: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        subject_contains: str | None = None,
        has_attachment: bool | None = None,
        label_in: Sequence[str] | None = None,
        free_text: str | None = None,
        cursor: str | None = None,
        include_snippets: bool = True,
    ) -> dict[str, object]:
        """Fetch matching threads and return a structured summary payload."""

        # Convert LLM-facing tool arguments into the provider-agnostic filter
        # contract used by the email port.
        email_filter = EmailThreadFilter(
            unread=unread_only,
            from_contains=sender,
            to_contains=recipient,
            subject_contains=subject_contains,
            label_in=tuple(label_in) if label_in else None,
            has_attachment=has_attachment,
            after=self._parse_start_datetime(from_date),
            before=self._parse_end_datetime(to_date),
            free_text=free_text,
        )

        normalized_limit = max(1, min(100, limit))
        page = self.email_port.list_threads(
            filters=email_filter,
            limit=normalized_limit,
            cursor=cursor,
            include_snippets=include_snippets,
        )

        return {
            # Returning both the normalized filters and the matched threads gives
            # the LLM enough context to explain what it summarized.
            "filters": self._serialize_filter(
                unread_only=unread_only,
                limit=normalized_limit,
                from_date=from_date,
                to_date=to_date,
                sender=sender,
                recipient=recipient,
                subject_contains=subject_contains,
                has_attachment=has_attachment,
                label_in=label_in,
                free_text=free_text,
                include_snippets=include_snippets,
            ),
            "returned_count": len(page.items),
            "next_cursor": page.next_cursor,
            "threads": [self._serialize_thread(thread) for thread in page.items],
        }

    def handle_summarize_emails(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `summarize_emails`."""

        # Tool calls arrive as untyped dicts from the model, so normalize each
        # field before handing off to the typed service method.
        return self.summarize_emails(
            unread_only=self._as_optional_bool(arguments.get("unread_only")),
            limit=self._as_int(arguments.get("limit"), default=50),
            from_date=self._as_str(arguments.get("from_date")),
            to_date=self._as_str(arguments.get("to_date")),
            sender=self._as_str(arguments.get("sender")),
            recipient=self._as_str(arguments.get("recipient")),
            subject_contains=self._as_str(arguments.get("subject_contains")),
            has_attachment=self._as_optional_bool(arguments.get("has_attachment")),
            label_in=self._as_str_list(arguments.get("label_in")),
            free_text=self._as_str(arguments.get("free_text")),
            cursor=self._as_str(arguments.get("cursor")),
            include_snippets=self._as_bool(arguments.get("include_snippets"), default=True),
        )

    @staticmethod
    def summarize_emails_tool() -> Tool:
        """Return the tool definition exposed to the language model."""

        return Tool(
            name="summarize_emails",
            description=(
                "List email threads for summarization. Supports filters for unread mail, "
                "sender, recipient, subject, attachment presence, labels, and date range."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only return unread threads.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of threads to inspect.",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Inclusive start date in YYYY-MM-DD or ISO-8601 format.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Inclusive end date in YYYY-MM-DD or ISO-8601 format.",
                    },
                    "sender": {
                        "type": "string",
                        "description": "Match sender name or email address.",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Match recipient name or email address.",
                    },
                    "subject_contains": {
                        "type": "string",
                        "description": "Match text in the email subject.",
                    },
                    "has_attachment": {
                        "type": "boolean",
                        "description": "Filter based on attachment presence.",
                    },
                    "label_in": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Match any of the supplied labels.",
                    },
                    "free_text": {
                        "type": "string",
                        "description": "Provider-specific raw query text, if needed.",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque cursor returned by a previous call.",
                    },
                    "include_snippets": {
                        "type": "boolean",
                        "description": "Include the latest snippet for each thread.",
                        "default": True,
                    },
                },
            },
        )

    @staticmethod
    def _serialize_thread(thread: EmailThreadSummary) -> dict[str, object]:
        """Convert a thread summary into a JSON-friendly dict for tool output."""

        return {
            "id": thread.id,
            "subject": thread.subject,
            "last_updated": thread.last_updated.isoformat(),
            "message_count": len(thread.message_ids),
            "participants": [
                {
                    "email": participant.email,
                    "name": participant.name,
                }
                for participant in thread.participants
            ],
            "snippet": thread.snippet,
            "labels": list(thread.labels),
            "unread": thread.unread,
        }

    @staticmethod
    def _serialize_filter(
        *,
        unread_only: bool | None,
        limit: int,
        from_date: str | None,
        to_date: str | None,
        sender: str | None,
        recipient: str | None,
        subject_contains: str | None,
        has_attachment: bool | None,
        label_in: Sequence[str] | None,
        free_text: str | None,
        include_snippets: bool,
    ) -> dict[str, object]:
        """Return the normalized filter values that were applied to the query."""

        return {
            "unread_only": unread_only,
            "limit": limit,
            "from_date": from_date,
            "to_date": to_date,
            "sender": sender,
            "recipient": recipient,
            "subject_contains": subject_contains,
            "has_attachment": has_attachment,
            "label_in": list(label_in) if label_in else [],
            "free_text": free_text,
            "include_snippets": include_snippets,
        }

    @staticmethod
    def _parse_start_datetime(value: str | None) -> datetime | None:
        """Parse a lower-bound timestamp, expanding bare dates to midnight UTC."""

        if value is None:
            return None

        # A bare date like 2026-03-24 should mean "starting at the beginning of
        # that day" when used as the lower bound of a search window.
        parsed = EmailReadService._parse_datetime(value)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            return datetime.combine(parsed, time.min, tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _parse_end_datetime(value: str | None) -> datetime | None:
        """Parse an upper-bound timestamp, treating bare dates as inclusive."""

        if value is None:
            return None

        # For the upper bound we treat a bare date as inclusive, so 2026-03-24
        # becomes midnight at the start of 2026-03-25.
        parsed = EmailReadService._parse_datetime(value)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            next_day = parsed + timedelta(days=1)
            return datetime.combine(next_day, time.min, tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _parse_datetime(value: str) -> datetime | date:
        """Parse either an ISO timestamp or a plain ISO date string."""

        # Accept either a full ISO timestamp or a plain YYYY-MM-DD date. The
        # boundary helpers above decide how date-only values should be expanded.
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
    def _as_optional_bool(value: object, default: bool | None = None) -> bool | None:
        """Coerce a raw value to an optional boolean for tri-state filters."""

        # Used for filters where "unset" is meaningful and should stay as None.
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return default
    
    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        """Coerce a raw value to a boolean, falling back to the provided default."""

        # Used when the caller needs an actual bool, not an optional one.
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return default

    @staticmethod
    def _as_int(value: object, *, default: int) -> int:
        """Coerce a raw value to an integer, ignoring invalid inputs."""

        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return default
        return default

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
