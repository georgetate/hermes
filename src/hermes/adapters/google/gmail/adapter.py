"""Composed Gmail adapter that satisfies the full EmailPort contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hermes.adapters.google.gmail.client import GmailClient
from hermes.adapters.google.gmail.reader import GmailReader
from hermes.adapters.google.gmail.writer import GmailWriter
from hermes.ports.email import (
    EmailPort,
    EmailThread,
    EmailThreadFilter,
    EmailThreadSummary,
    NewEmailDraft,
    Page,
    ReplyDraft,
)


@dataclass
class GmailAdapter(EmailPort):
    """Full Gmail email adapter composed from read and write implementations."""

    reader: GmailReader
    writer: GmailWriter

    def __init__(
        self,
        *,
        client: Optional[GmailClient] = None,
        reader: Optional[GmailReader] = None,
        writer: Optional[GmailWriter] = None,
    ) -> None:
        shared_client = client or GmailClient.from_settings()
        self.reader = reader or GmailReader(client=shared_client)
        self.writer = writer or GmailWriter(client=shared_client)

    def sync_threads(
        self,
        *,
        history_id: str,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """Delegate incremental sync operations to the read adapter."""
        return self.reader.sync_threads(
            history_id=history_id,
            include_snippets=include_snippets,
        )

    def full_sync_threads(
        self,
        *,
        include_spam_trash: bool = False,
        filters: EmailThreadFilter | None = None,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """Delegate full sync operations to the read adapter."""
        return self.reader.full_sync_threads(
            include_spam_trash=include_spam_trash,
            filters=filters,
            include_snippets=include_snippets,
        )

    def list_threads(
        self,
        filters: EmailThreadFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """Delegate thread listing to the read adapter."""
        return self.reader.list_threads(
            filters=filters,
            limit=limit,
            cursor=cursor,
            include_snippets=include_snippets,
        )

    def get_thread(
        self,
        thread_id: str,
        *,
        include_bodies: bool = True,
    ) -> EmailThread:
        """Delegate thread retrieval to the read adapter."""
        return self.reader.get_thread(
            thread_id,
            include_bodies=include_bodies,
        )

    def create_draft_new(self, draft: NewEmailDraft) -> str:
        """Delegate new draft creation to the write adapter."""
        return self.writer.create_new_draft(draft)

    def create_draft_reply(self, draft: ReplyDraft) -> str:
        """Delegate reply draft creation to the write adapter."""
        return self.writer.create_reply_draft(draft)

    def send_draft(self, draft_id: str) -> str:
        """Delegate draft sending to the write adapter."""
        return self.writer.send_draft(draft_id)

    def delete_draft(self, draft_id: str) -> None:
        """Delegate draft deletion to the write adapter."""
        self.writer.delete_draft(draft_id)

    def mark_thread_read(self, thread_id: str) -> None:
        """Delegate marking a thread as read to the write adapter."""
        self.writer.mark_thread_read(thread_id)

    def mark_thread_unread(self, thread_id: str) -> None:
        """Delegate marking a thread as unread to the write adapter."""
        self.writer.mark_thread_unread(thread_id)
