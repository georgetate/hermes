from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hermes.ports.email import EmailAddress, EmailWritePort, NewEmailDraft, ReplyDraft
from hermes.ports.llm import Tool


@dataclass(slots=True)
class EmailWriteService:
    """Write-side email orchestration for draft-focused LLM tools."""

    email_port: EmailWritePort

    def send_draft(
        self,
        *,
        draft_id: str,
    ) -> dict[str, object]:
        """Send an existing draft and return a compact confirmation payload."""

        normalized_draft_id = draft_id.strip()
        if not normalized_draft_id:
            raise ValueError("draft_id is required to send a draft.")

        message_id = self.email_port.send_draft(normalized_draft_id)
        return {
            "draft_id": normalized_draft_id,
            "message_id": message_id,
            "sent": True,
        }

    def draft_email(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str | None = None,
        body_html: str | None = None,
        cc: Sequence[str] | None = None,
        bcc: Sequence[str] | None = None,
        attachment_paths: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Create a new email draft and return a compact draft summary."""

        draft = NewEmailDraft(
            to=self._as_email_addresses(to),
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=self._as_optional_email_addresses(cc),
            bcc=self._as_optional_email_addresses(bcc),
            attachment_paths=list(attachment_paths) if attachment_paths else None,
        )
        draft_id = self.email_port.create_new_draft(draft)

        return {
            "draft_id": draft_id,
            "subject": draft.subject,
            "to": [address.email for address in draft.to],
            "cc": [address.email for address in draft.cc or []],
            "bcc": [address.email for address in draft.bcc or []],
            "body_preview": self._trim_text(
                draft.body_text or draft.body_html or "",
                600,
            ),
            "attachment_paths": list(draft.attachment_paths or []),
        }

    def handle_draft_email(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `draft_email`."""

        to = self._as_str_list(arguments.get("to"))
        subject = self._as_str(arguments.get("subject"))
        if not to:
            raise ValueError("At least one recipient is required to draft an email.")
        if subject is None:
            raise ValueError("subject is required to draft an email.")

        return self.draft_email(
            to=to,
            subject=subject,
            body_text=self._as_str(arguments.get("body_text")),
            body_html=self._as_str(arguments.get("body_html")),
            cc=self._as_str_list(arguments.get("cc")),
            bcc=self._as_str_list(arguments.get("bcc")),
            attachment_paths=self._as_str_list(arguments.get("attachment_paths")),
        )

    def draft_reply_email(
        self,
        *,
        thread_id: str,
        body_text: str | None = None,
        body_html: str | None = None,
        reply_all: bool = True,
        reference_message_id: str | None = None,
        attachment_paths: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Create a reply draft within an existing thread."""

        draft = ReplyDraft(
            thread_id=thread_id,
            body_text=body_text,
            body_html=body_html,
            reply_all=reply_all,
            reference_message_id=reference_message_id,
            attachment_paths=list(attachment_paths) if attachment_paths else None,
        )
        draft_id = self.email_port.create_reply_draft(draft)

        return {
            "draft_id": draft_id,
            "thread_id": draft.thread_id,
            "reply_all": draft.reply_all,
            "reference_message_id": draft.reference_message_id,
            "body_preview": self._trim_text(
                draft.body_text or draft.body_html or "",
                600,
            ),
            "attachment_paths": list(draft.attachment_paths or []),
        }

    def handle_draft_reply_email(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `draft_reply_email`."""

        thread_id = self._as_str(arguments.get("thread_id"))
        if thread_id is None:
            raise ValueError("thread_id is required to draft a reply email.")

        reply_all = self._as_bool(arguments.get("reply_all"), default=True)
        reference_message_id = self._as_str(arguments.get("reference_message_id"))
        if not reply_all and reference_message_id is None:
            raise ValueError(
                "reference_message_id is required when reply_all is false."
            )

        return self.draft_reply_email(
            thread_id=thread_id,
            body_text=self._as_str(arguments.get("body_text")),
            body_html=self._as_str(arguments.get("body_html")),
            reply_all=reply_all,
            reference_message_id=reference_message_id,
            attachment_paths=self._as_str_list(arguments.get("attachment_paths")),
        )

    def handle_send_draft(self, arguments: dict[str, object]) -> dict[str, object]:
        """Normalize raw tool-call arguments and run `send_draft`."""

        draft_id = self._as_str(arguments.get("draft_id"))
        if draft_id is None:
            raise ValueError("draft_id is required to send a draft.")

        return self.send_draft(draft_id=draft_id)

    @staticmethod
    def draft_email_tool() -> Tool:
        """Return the tool definition for creating a new email draft."""

        return Tool(
            name="draft_email",
            description="Create a new email draft.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject line.",
                    },
                    "body_text": {
                        "type": "string",
                        "description": "Plain-text email body.",
                    },
                    "body_html": {
                        "type": "string",
                        "description": "Optional HTML email body.",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipient email addresses.",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipient email addresses.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Local file paths to attach.",
                    },
                },
                "required": ["to", "subject"],
            },
        )

    @staticmethod
    def draft_reply_email_tool() -> Tool:
        """Return the tool definition for drafting a reply email."""

        return Tool(
            name="draft_reply_email",
            description="Create a reply draft inside an email thread.",
            input_schema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "The email thread to reply inside.",
                    },
                    "body_text": {
                        "type": "string",
                        "description": "Plain-text reply body.",
                    },
                    "body_html": {
                        "type": "string",
                        "description": "Optional HTML reply body.",
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "Reply to all participants.",
                        "default": True,
                    },
                    "reference_message_id": {
                        "type": "string",
                        "description": "Required when reply_all is false.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Local file paths to attach.",
                    },
                },
                "required": ["thread_id"],
            },
        )

    @staticmethod
    def send_draft_tool() -> Tool:
        """Return the tool definition for sending an existing draft."""

        return Tool(
            name="send_draft",
            description="Send an existing email draft by draft id.",
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "The draft id to send.",
                    },
                },
                "required": ["draft_id"],
                "additionalProperties": False,
            },
            requires_confirmation=True,
        )

    @staticmethod
    def _as_email_addresses(values: Sequence[str]) -> Sequence[EmailAddress]:
        """Convert email strings into provider-agnostic address DTOs."""

        if not values:
            return ()
        return tuple(
            EmailAddress(email=value.strip())
            for value in values
            if value.strip()
        )
    
    @staticmethod
    def _as_optional_email_addresses(values: Sequence[str] | None) -> Sequence[EmailAddress] | None:
        """Convert email strings into provider-agnostic address DTOs."""

        if not values:
            return None
        return tuple(
            EmailAddress(email=value.strip())
            for value in values
            if value.strip()
        )

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        """Coerce a raw value to a boolean with a default fallback."""

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
