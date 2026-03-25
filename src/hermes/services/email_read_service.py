from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from typing import Any, Sequence

from hermes.ports.email import (
    AttachmentMeta,
    EmailMessage,
    EmailReadPort,
    EmailThread,
    EmailThreadFilter,
    EmailThreadSummary,
)
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

    def read_full_email(
        self,
        *,
        thread_id: str,
        include_bodies: bool = True,
        max_messages: int = 2,
        max_chars_per_message: int = 1200,
    ) -> dict[str, object]:
        """Fetch a trimmed thread excerpt for one email conversation."""

        thread = self.email_port.get_thread(
            thread_id,
            include_bodies=include_bodies,
        )

        normalized_max_messages = max(1, min(5, max_messages))
        normalized_max_chars = max(200, min(3000, max_chars_per_message))

        return {
            "thread": self._serialize_full_thread(
                thread,
                include_bodies=include_bodies,
                max_messages=normalized_max_messages,
                max_chars_per_message=normalized_max_chars,
            ),
            "include_bodies": include_bodies,
            "max_messages": normalized_max_messages,
            "max_chars_per_message": normalized_max_chars,
        }

    def handle_read_full_email(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `read_full_email`."""

        thread_id = self._as_str(arguments.get("thread_id"))
        if thread_id is None:
            raise ValueError("thread_id is required to read a full email thread.")

        return self.read_full_email(
            thread_id=thread_id,
            include_bodies=self._as_bool(
                arguments.get("include_bodies"),
                default=True,
            ),
            max_messages=self._as_int(
                arguments.get("max_messages"),
                default=2,
            ),
            max_chars_per_message=self._as_int(
                arguments.get("max_chars_per_message"),
                default=1200,
            ),
        )

    @staticmethod
    def summarize_emails_tool() -> Tool:
        """Return the tool definition exposed to the language model."""

        return Tool(
            name="summarize_emails",
            description=(
                "List email threads with lightweight summaries and filters."
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
    def read_full_email_tool() -> Tool:
        """Return the tool definition for reading one full email thread."""

        return Tool(
            name="read_full_email",
            description=(
                "Read a trimmed excerpt of one email thread by thread id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "The thread id to retrieve.",
                    },
                    "include_bodies": {
                        "type": "boolean",
                        "description": "Include text and HTML bodies when available.",
                        "default": True,
                    },
                    "max_messages": {
                        "type": "integer",
                        "description": "Newest messages to include.",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 2,
                    },
                    "max_chars_per_message": {
                        "type": "integer",
                        "description": "Max cleaned body chars per message.",
                        "minimum": 200,
                        "maximum": 3000,
                        "default": 1200,
                    },
                },
                "required": ["thread_id"],
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
    def _serialize_full_thread(
        thread: EmailThread,
        *,
        include_bodies: bool,
        max_messages: int,
        max_chars_per_message: int,
    ) -> dict[str, object]:
        """Convert a thread into a trimmed, recent-message excerpt."""

        selected_messages = list(thread.messages[-max_messages:])
        return {
            "id": thread.id,
            "subject": thread.subject,
            "last_updated": thread.last_updated.isoformat(),
            "labels": list(thread.labels),
            "message_count": len(thread.messages),
            "included_message_count": len(selected_messages),
            "omitted_older_message_count": max(
                0,
                len(thread.messages) - len(selected_messages),
            ),
            "messages": [
                EmailReadService._serialize_message(
                    message,
                    include_bodies=include_bodies,
                    max_chars_per_message=max_chars_per_message,
                )
                for message in selected_messages
            ],
        }

    @staticmethod
    def _serialize_message(
        message: EmailMessage,
        *,
        include_bodies: bool = True,
        max_chars_per_message: int = 1200,
    ) -> dict[str, object]:
        """Convert one message into a trimmed JSON-friendly dict."""

        cleaned_body = (
            EmailReadService._extract_message_excerpt(
                message,
                max_chars=max_chars_per_message,
            )
            if include_bodies
            else None
        )

        return {
            "id": message.id,
            "thread_id": message.thread_id,
            "subject": message.subject,
            "from": EmailReadService._serialize_address(
                message.from_.email,
                message.from_.name,
            ),
            "to": [
                EmailReadService._serialize_address(address.email, address.name)
                for address in message.to
            ],
            "cc": [
                EmailReadService._serialize_address(address.email, address.name)
                for address in message.cc
            ],
            "bcc": [
                EmailReadService._serialize_address(address.email, address.name)
                for address in message.bcc
            ],
            "snippet": message.snippet,
            "body_excerpt": cleaned_body,
            "internal_ts": message.internal_ts.isoformat(),
            "labels": list(message.labels),
            "has_attachments": message.has_attachments,
            "attachments": [
                EmailReadService._serialize_attachment(attachment)
                for attachment in message.attachments
            ],
        }

    @staticmethod
    def _serialize_attachment(attachment: AttachmentMeta) -> dict[str, object]:
        """Convert attachment metadata into a JSON-friendly dict."""

        return {
            "id": attachment.id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": attachment.size_bytes,
            "content_id": attachment.content_id,
        }

    @staticmethod
    def _serialize_address(email: str, name: str | None) -> dict[str, object]:
        """Convert an email address into a JSON-friendly dict."""

        return {
            "email": email,
            "name": name,
        }

    @staticmethod
    def _extract_message_excerpt(
        message: EmailMessage,
        *,
        max_chars: int,
    ) -> str | None:
        """Return a cleaned plain-text excerpt from the most useful message body."""

        source = message.body_text
        if not source and message.body_html:
            source = EmailReadService._html_to_text(message.body_html)
        if not source:
            return None

        cleaned = EmailReadService._clean_email_text(source)
        if not cleaned:
            return None
        return EmailReadService._trim_text(cleaned, max_chars)

    @staticmethod
    def _html_to_text(value: str) -> str:
        """Convert lightweight HTML content into readable plain text."""

        without_tags = re.sub(r"<[^>]+>", " ", value)
        return unescape(without_tags)

    @staticmethod
    def _clean_email_text(value: str) -> str:
        """Strip common email noise such as quotes, signatures, and spacing."""

        lines: list[str] = []
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lower = line.lower()

            if line.startswith(">"):
                continue
            if lower.startswith("on ") and " wrote:" in lower:
                break
            if lower.startswith("from:") and "@" in lower:
                break
            if line.startswith("--"):
                break
            if "sent from my" in lower:
                break

            lines.append(line)

        return " ".join(lines)

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        """Collapse whitespace and trim text to a bounded size."""

        normalized = " ".join(text.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max_chars]
        return normalized[: max_chars - 3].rstrip() + "..."

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
