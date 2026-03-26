from __future__ import annotations

from datetime import datetime, timezone

from hermes.app.cli import run_cli
from hermes.config import Settings, get_settings
from hermes.services.calendar_read_service import CalendarReadService
from hermes.services.calendar_write_service import CalendarWriteService
from hermes.services.conversation_service import ConversationService
from hermes.services.email_read_service import EmailReadService
from hermes.services.email_write_service import EmailWriteService


BASE_SYSTEM_PROMPT = (
    "You are Hermes, a careful assistant for email and calendar workflows. "
    "Prefer tool use over guessing when tools can answer the question. "
    "If a detail is ambiguous or unavailable, say you do not know or ask a concise "
    "follow-up question instead of inventing facts. "
    "Never claim that you completed an external action unless you actually called "
    "the corresponding tool and received a successful tool result. "
    "If the user requests an email or calendar action and no matching tool exists, "
    "say clearly that you cannot perform it in this environment rather than "
    "implying success. "
    "Some destructive tools require explicit user confirmation before execution. "
    "When a confirmation prompt is shown, wait for the user's confirm or cancel "
    "reply instead of assuming approval. "
    "Read tools can inform answers, but only write tools can justify claims that "
    "you changed external state. "
    "For write actions like creating calendar events or drafting email, think once, "
    "then make at most one tool call for the same action unless the user explicitly "
    "asks you to retry or create multiple items."
)


def build_system_prompt(
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    """Build the default system prompt with runtime date and timezone context."""

    resolved_settings = settings or get_settings()
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    try:
        from zoneinfo import ZoneInfo

        local_now = current_time.astimezone(ZoneInfo(resolved_settings.timezone))
    except Exception:
        local_now = current_time.astimezone(timezone.utc)

    runtime_context = (
        f"Runtime context: current local datetime is {local_now.isoformat()} in "
        f"timezone {resolved_settings.timezone}. Today's date is "
        f"{local_now.date().isoformat()}. Resolve relative dates like today, "
        "tomorrow, and next Friday against this date. "
        f"When the user does not name a calendar for a new event, prefer the "
        f"default calendar_id '{resolved_settings.gcal_calendar_id}'."
    )
    return f"{BASE_SYSTEM_PROMPT} {runtime_context}"


def build_conversation_service(
    *,
    settings: Settings | None = None,
    llm=None,
    email_reader=None,
    calendar_reader=None,
    email_writer=None,
    calendar_writer=None,
    system_prompt: str | None = None,
    now: datetime | None = None,
) -> ConversationService:
    """Compose the Hermes assistant from adapters, services, tools, and prompt."""

    resolved_settings = settings or get_settings()

    if llm is None:
        from hermes.adapters.local_openai_compatible.llm_engine import (
            LocalOpenAICompatibleLLM,
        )

        llm = LocalOpenAICompatibleLLM(settings=resolved_settings)
    if email_reader is None:
        from hermes.adapters.google.gmail.reader import GmailReader

        email_reader = GmailReader()
    if calendar_reader is None:
        from hermes.adapters.google.gcal.reader import GCalReader

        calendar_reader = GCalReader()
    if email_writer is None:
        from hermes.adapters.google.gmail.writer import GmailWriter

        email_writer = GmailWriter()
    if calendar_writer is None:
        from hermes.adapters.google.gcal.writer import GCalWriter

        calendar_writer = GCalWriter()

    email_read_service = EmailReadService(email_reader)
    calendar_read_service = CalendarReadService(calendar_reader)
    email_write_service = EmailWriteService(email_writer)
    calendar_write_service = CalendarWriteService(
        calendar_port=calendar_writer,
        settings=resolved_settings,
    )

    conversation_service = ConversationService(
        llm=llm,
        system_prompt=system_prompt or build_system_prompt(
            settings=resolved_settings,
            now=now,
        ),
    )
    conversation_service.register_tool(
        email_read_service.summarize_emails_tool(),
        email_read_service.handle_summarize_emails,
    )
    conversation_service.register_tool(
        email_read_service.read_full_email_tool(),
        email_read_service.handle_read_full_email,
    )
    conversation_service.register_tool(
        calendar_read_service.list_calendars_tool(),
        calendar_read_service.handle_list_calendars,
    )
    conversation_service.register_tool(
        calendar_read_service.summarize_calendar_tool(),
        calendar_read_service.handle_summarize_calendar,
    )
    conversation_service.register_tool(
        calendar_write_service.delete_event_tool(),
        calendar_write_service.handle_delete_event,
    )
    conversation_service.register_tool(
        calendar_read_service.read_calendar_event_tool(),
        calendar_read_service.handle_read_calendar_event,
    )
    conversation_service.register_tool(
        email_write_service.draft_email_tool(),
        email_write_service.handle_draft_email,
    )
    conversation_service.register_tool(
        email_write_service.draft_reply_email_tool(),
        email_write_service.handle_draft_reply_email,
    )
    conversation_service.register_tool(
        email_write_service.mark_thread_read_tool(),
        email_write_service.handle_mark_thread_read,
    )
    conversation_service.register_tool(
        email_write_service.mark_thread_unread_tool(),
        email_write_service.handle_mark_thread_unread,
    )
    conversation_service.register_tool(
        email_write_service.send_draft_tool(),
        email_write_service.handle_send_draft,
    )
    conversation_service.register_tool(
        email_write_service.delete_draft_tool(),
        email_write_service.handle_delete_draft,
    )
    conversation_service.register_tool(
        calendar_write_service.create_event_tool(),
        calendar_write_service.handle_create_event,
    )

    return conversation_service


def main() -> int:
    """Run the Hermes CLI with the default wired application stack."""

    return run_cli(build_conversation_service())


if __name__ == "__main__":
    raise SystemExit(main())
