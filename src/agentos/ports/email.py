# src/agentos/ports/email.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence, Generic, TypeVar

T = TypeVar("T")

# ---------- Shared paging primitive ----------

@dataclass(frozen=True)
class Page(Generic[T]):
    """
    A single page of results.
    - items: results in this page
    - next_cursor: opaque token to fetch the next page (None = no more)
    - total: total number of results across ALL pages, if the provider supplies it;
             otherwise None. Not the size of this page.
    """
    items: Sequence[T]
    next_cursor: Optional[str] = None   # Opaque token for subsequent page
    total: Optional[int] = None         # Optional total count if provider can supply
    next_sync_token: Optional[str] =  None # for sync


# ---------- Core email DTOs (provider-agnostic) ----------

@dataclass(frozen=True)
class EmailAddress:
    """A single email address, optionally with a display name."""
    email: str
    name: Optional[str] = None


@dataclass(frozen=True)
class AttachmentMeta:
    """
    Metadata describing an attachment on a received message.
    (Adapters may use provider-specific IDs internally when downloading.)
    """
    id: str
    filename: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    content_id: Optional[str] = None    # For inline images (cid:...)


@dataclass(frozen=True)
class EmailMessage:
    """
    One message inside a thread. Bodies are optional to allow lightweight listing,
    and can be included by adapters when fetching a full thread.
    """
    id: str
    thread_id: str
    subject: str
    from_: EmailAddress
    to: Sequence[EmailAddress]
    cc: Sequence[EmailAddress]
    bcc: Sequence[EmailAddress]
    snippet: Optional[str]
    body_text: Optional[str]
    body_html: Optional[str]
    internal_ts: datetime               # Provider's internal timestamp (ms since epoch → datetime)
    labels: Sequence[str]               # e.g., ["INBOX", "UNREAD", "STARRED"]
    has_attachments: bool
    attachments: Sequence[AttachmentMeta]


@dataclass(frozen=True)
class EmailThreadSummary:
    """
    Lightweight representation for list views/triage.
    """
    id: str
    subject: str
    last_updated: datetime
    message_ids: Sequence[str]
    participants: Sequence[EmailAddress]  # Unique set from from_/to/cc across the thread
    snippet: Optional[str]                # Typically the latest message snippet
    labels: Sequence[str]                 # Thread-level labels if provider exposes them
    unread: bool                          # True if any message is unread in this thread


@dataclass(frozen=True)
class EmailThread:
    """Full conversation with messages in chronological order (oldest → newest)."""
    id: str
    subject: str
    last_updated: datetime
    labels: Sequence[str]
    messages: Sequence[EmailMessage]


# ---------- Query filters for listing threads ----------

@dataclass(frozen=True)
class EmailThreadFilter:
    """
    Structured filters that adapters translate to provider queries.
    Use free_text only when you need raw provider syntax (e.g., Gmail query).
    """
    unread: Optional[bool] = None
    starred: Optional[bool] = None
    from_contains: Optional[str] = None       # substring or address match
    to_contains: Optional[str] = None
    subject_contains: Optional[str] = None
    label_in: Optional[Sequence[str]] = None  # match any of these labels
    has_attachment: Optional[bool] = None
    after: Optional[datetime] = None          # inclusive
    before: Optional[datetime] = None         # exclusive
    free_text: Optional[str] = None           # raw provider syntax (escape hatch)


# ---------- Draft composition inputs ----------

@dataclass(frozen=True)
class NewEmailDraft:
    """Compose a brand-new draft (not a reply)."""
    to: Sequence[EmailAddress]
    subject: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    cc: Optional[Sequence[EmailAddress]] = None
    bcc: Optional[Sequence[EmailAddress]] = None
    attachment_paths: Optional[Sequence[str]] = None  # local file paths; adapters decide how to stream


@dataclass(frozen=True)
class ReplyDraft:
    """Compose a reply draft inside an existing thread."""
    thread_id: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    reply_all: bool = True
    reference_message_id: Optional[str] = None       # reply to a specific message in the thread
    attachment_paths: Optional[Sequence[str]] = None


# ---------- Outbound Email Port (gateway interface) ----------

class EmailPort(Protocol):
    """
    Provider-agnostic email gateway focused on THREADS as the primary unit.

    Adapters (e.g., GmailAdapter, OutlookAdapter) implement this interface and
    translate structured filters into provider-specific queries.
    """

    # --- syncs ---

    def sync_threads(
        self,
        *,
        history_id: str,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """
        Return a page of thread summaries representing changes since the given
        history_id. The next_cursor in the returned Page is the new history_id
        to use for subsequent syncs.
        """
        raise NotImplementedError
    
    def full_sync_threads(
        self,
        *,
        include_spam_trash: bool = False,
        filters: Optional[EmailThreadFilter] = None,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """
        Perform a full sync of threads, optionally filtered by the given criteria.
        The next_cursor in the returned Page is an opaque token to use for subsequent
        incremental syncs.
        """
        raise NotImplementedError

    # --- Reads (threads-first) ---

    def list_threads(
        self,
        filters: Optional[EmailThreadFilter] = None,
        *,
        limit: int = 50,
        cursor: Optional[str] = None,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """
        Return a page of thread summaries ordered by provider default
        (typically last-updated desc). `cursor` is an opaque token from a prior page.
        """
        raise NotImplementedError

    def get_thread(
        self,
        thread_id: str,
        *,
        include_bodies: bool = True,
    ) -> EmailThread:
        """
        Retrieve the full conversation for a given thread. If `include_bodies`
        is False, adapters may omit body_text/body_html for speed.
        """
        raise NotImplementedError

    # --- Writes (draft + send) ---

    def create_draft_new(self, draft: NewEmailDraft) -> str:
        """
        Create a brand-new draft (not a reply). Returns draft_id.
        """
        raise NotImplementedError

    def create_draft_reply(self, draft: ReplyDraft) -> str:
        """
        Create a reply draft within an existing thread. Returns draft_id.
        """
        raise NotImplementedError

    def send_draft(self, draft_id: str) -> str:
        """
        Send the draft. Returns provider message_id for the sent message.
        """
        raise NotImplementedError
