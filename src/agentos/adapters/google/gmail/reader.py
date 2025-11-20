# src/agentos/adapters/google/gmail/reader.py
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TypeVar, cast, Any

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from agentos.logging_utils import get_logger
from agentos.config import settings
from agentos.adapters.google.gmail.client import GmailClient
from agentos.adapters.google.gmail.normalizer import (
    normalize_thread,
    summarize_thread,
)
from agentos.ports.email import (
    EmailThread,
    EmailThreadFilter,
    EmailThreadSummary,
    Page,
)

log = get_logger(__name__)


# -------- Helpers: backoff/retry & date formatting --------

def _should_retry_http_error(e: HttpError) -> bool:
    try:
        status = int(getattr(e, "status_code", None) or e.resp.status)  # type: ignore[attr-defined]
    except Exception:
        return False
    return status == 429 or 500 <= status <= 599

T = TypeVar('T')
def _execute_with_retries(request: HttpRequest, *, max_attempts: int = 3, base_delay: float = 0.5, cap_s: float = 8.0) -> T: # type: ignore
    """
    Execute a Google API request with exponential backoff + jitter for 429/5xx.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            result = request.execute()
            return cast(T, result)
        except HttpError as e:
            if attempt < max_attempts and _should_retry_http_error(e):
                # exponential backoff with jitter
                delay = min(cap_s, base_delay * (2 ** (attempt - 1)))
                delay = delay * (0.5 + random.random())  # jitter in [0.5x, 1.5x]
                log.warning(
                    "gmail.reader.retrying_http_error",
                    extra={"attempt": attempt, "max_attempts": max_attempts, "delay_s": round(delay, 3)},
                )
                time.sleep(delay)
                continue
            log.exception("gmail.reader.request_failed")
            raise


def _fmt_date_for_gmail(d: datetime) -> str:
    # Gmail's "after:" / "before:" accept YYYY/MM/DD (local tz not critical for day-granularity)
    # Use date in the user's configured timezone if desired; here we just format as UTC date.
    return d.astimezone(timezone.utc).strftime("%Y/%m/%d")


# -------- Filter → Gmail query builder --------

def _build_gmail_query(f: Optional[EmailThreadFilter]) -> Optional[str]:
    if not f:
        return None

    terms: list[str] = []

    # Booleans
    if f.unread is True:
        terms.append("is:unread")
    elif f.unread is False:
        terms.append("-is:unread")

    if f.starred is True:
        terms.append("is:starred")
    elif f.starred is False:
        terms.append("-is:starred")

    if f.has_attachment is True:
        terms.append("has:attachment")
    elif f.has_attachment is False:
        terms.append("-has:attachment")

    # Address/subject contains
    if f.from_contains:
        terms.append(f'from:"{f.from_contains}"')
    if f.to_contains:
        terms.append(f'to:"{f.to_contains}"')
    if f.subject_contains:
        terms.append(f'subject:"{f.subject_contains}"')

    # Labels (any-of)
    if f.label_in:
        label_terms = [f'label:"{lab}"' for lab in f.label_in if lab]
        if label_terms:
            if len(label_terms) == 1:
                terms.append(label_terms[0])
            else:
                # Group with OR so any label matches
                terms.append("(" + " OR ".join(label_terms) + ")")

    # Dates
    if f.after:
        terms.append(f"after:{_fmt_date_for_gmail(f.after)}")
    if f.before:
        terms.append(f"before:{_fmt_date_for_gmail(f.before)}")

    # Escape hatch
    if f.free_text:
        terms.append(f.free_text)

    return " ".join(terms) if terms else None


# -------- Reader implementation --------

@dataclass
class GmailReader:
    """
    Gmail adapter read-side implementation mapped to the EmailPort.
    - list_threads: summaries via threads.list → threads.get(format="metadata")
    - get_thread: metadata or full, then normalize
    """

    def __init__(self, client: Optional[GmailClient] = None) -> None:
        self.client = client or GmailClient.from_settings()

    # --- thread syncs ---

    def sync_threads(
        self,
        *,
        history_id: str,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """
        Incremental sync using Gmail History API.
        - history_id: last stored historyId from previous full_sync/sync.
        - Exhausts all history pages and returns updated thread summaries
          plus the new historyId cursor.
        """
        svc = self.client.get_service()
        users = svc.users()
        history = users.history()
        threads = users.threads()

        page_size = 100
        page_token: Optional[str] = None

        changed_thread_ids: set[str] = set()
        last_history_id: Optional[int] = None

        # 1) Walk history to collect changed thread IDs and the newest historyId
        while True:
            params: dict[str, Any] = {
                "userId": settings.gmail_user_id,
                "startHistoryId": history_id,
                "maxResults": page_size,
            }
            if page_token:
                params["pageToken"] = page_token

            hist_req = history.list(**params)
            resp = _execute_with_retries(hist_req)

            # Track last historyId from response
            h_resp = resp.get("historyId")
            if h_resp is not None:
                try:
                    hi = int(h_resp)
                    if last_history_id is None or hi > last_history_id:
                        last_history_id = hi
                except (TypeError, ValueError):
                    pass

            for h in resp.get("history", []) or []:
                for entry_key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                    for m in h.get(entry_key, []) or []:
                        msg = m.get("message") or {}
                        tid = msg.get("threadId")
                        if tid:
                            changed_thread_ids.add(tid)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # If nothing changed, just return an empty page with the same cursor
        if not changed_thread_ids and last_history_id is None:
            return Page[EmailThreadSummary](
                items=tuple(),
                next_cursor=None,
                total=None,
                next_sync_token=history_id,
            )

        new_history_id = str(last_history_id) if last_history_id is not None else history_id

        # 2) Fetch current state for all changed threads and summarize
        summaries: list[EmailThreadSummary] = []
        total_skipped = 0

        for tid in changed_thread_ids:
            try:
                time.sleep(random.uniform(0.02, 0.06))
                get_req = threads.get(
                    userId=settings.gmail_user_id,
                    id=tid,
                    format="metadata",
                    metadataHeaders=[
                        "From",
                        "To",
                        "Cc",
                        "Bcc",
                        "Subject",
                        "Date",
                        "Message-Id",
                    ],
                )
                raw_thread = _execute_with_retries(get_req)

                thread = normalize_thread(raw_thread)
                summary = summarize_thread(thread)
                if not include_snippets:
                    summary = EmailThreadSummary(
                        id=summary.id,
                        subject=summary.subject,
                        last_updated=summary.last_updated,
                        message_ids=summary.message_ids,
                        participants=summary.participants,
                        snippet=None,
                        labels=summary.labels,
                        unread=summary.unread,
                    )
                summaries.append(summary)
            except HttpError as e:
                # 404 likely means the thread was deleted; caller/storage can interpret
                total_skipped += 1
                log.warning(
                    "gmail.reader.sync_thread_get_failed",
                    extra={"thread_id": tid, "status": getattr(e, "status_code", None)},
                )
            except Exception:
                total_skipped += 1
                log.exception(
                    "gmail.reader.sync_thread_get_exception",
                    extra={"thread_id": tid},
                )

        log.info(
            "gmail.reader.sync_threads_done",
            extra={
                "changed_threads": len(changed_thread_ids),
                "returned": len(summaries),
                "skipped": total_skipped,
                "new_history_id": new_history_id,
            },
        )

        return Page[EmailThreadSummary](
            items=tuple(summaries),
            next_cursor=None,
            total=None,
            # Again, treat next_sync_token as the generic "sync cursor" slot
            next_sync_token=new_history_id,
        )
    

    def full_sync_threads(
        self,
        *,
        include_spam_trash: bool = False,
        filters: Optional[EmailThreadFilter] = None,
        include_snippets: bool = True,
    ) -> Page[EmailThreadSummary]:
        """
        Initial full sync of threads (no history cursor). Exhausts all pages of threads.list
        and returns summaries + a historyId cursor for future incremental sync.
        """
        svc = self.client.get_service()
        users = svc.users()
        threads = users.threads()

        q = _build_gmail_query(filters)
        page_size = 100

        collected: list[EmailThreadSummary] = []
        total_skipped = 0
        page_token: Optional[str] = None
        max_history_id: Optional[int] = None

        while True:
            list_req = threads.list(
                userId=settings.gmail_user_id,
                q=q,
                pageToken=page_token,
                maxResults=page_size,
                includeSpamTrash=include_spam_trash,
            )
            list_resp = _execute_with_retries(list_req)
            ids = [t["id"] for t in (list_resp.get("threads") or [])]

            if not ids:
                page_token = None
                break

            for tid in ids:
                try:
                    time.sleep(random.uniform(0.02, 0.06))  # small jitter to avoid burstiness
                    get_req = threads.get(
                        userId=settings.gmail_user_id,
                        id=tid,
                        format="metadata",
                        metadataHeaders=[
                            "From",
                            "To",
                            "Cc",
                            "Bcc",
                            "Subject",
                            "Date",
                            "Message-Id",
                        ],
                    )
                    raw_thread = _execute_with_retries(get_req)

                    # Track max historyId seen (thread or message-level)
                    h = raw_thread.get("historyId")
                    if h is not None:
                        try:
                            hi = int(h)
                            if max_history_id is None or hi > max_history_id:
                                max_history_id = hi
                        except (TypeError, ValueError):
                            pass

                    thread = normalize_thread(raw_thread)
                    summary = summarize_thread(thread)
                    if not include_snippets:
                        summary = EmailThreadSummary(
                            id=summary.id,
                            subject=summary.subject,
                            last_updated=summary.last_updated,
                            message_ids=summary.message_ids,
                            participants=summary.participants,
                            snippet=None,
                            labels=summary.labels,
                            unread=summary.unread,
                        )
                    collected.append(summary)
                except HttpError as e:
                    total_skipped += 1
                    log.warning(
                        "gmail.reader.full_sync_thread_get_failed",
                        extra={"thread_id": tid, "status": getattr(e, "status_code", None)},
                    )
                except Exception:
                    total_skipped += 1
                    log.exception(
                        "gmail.reader.full_sync_thread_get_exception",
                        extra={"thread_id": tid},
                    )

            page_token = list_resp.get("nextPageToken")
            if not page_token:
                break

        next_history_id: Optional[str] = str(max_history_id) if max_history_id is not None else None

        log.info(
            "gmail.reader.full_sync_threads_done",
            extra={
                "returned": len(collected),
                "skipped": total_skipped,
                "query": q,
                "include_spam_trash": include_spam_trash,
                "next_history_id": next_history_id,
            },
        )

        # Reuse next_sync_token as the generic "sync cursor" field (here: Gmail historyId)
        return Page[EmailThreadSummary](
            items=tuple(collected),
            next_cursor=None,
            total=None,
            next_sync_token=next_history_id,
        )
    
    # --- Reads ---

    def list_threads(
        self,
        filters: Optional[EmailThreadFilter] = None,
        *,
        limit: int = 50,
        cursor: Optional[str] = None,
        include_snippets: bool = True,  # kept for signature parity; Gmail provides snippet in metadata
    ) -> Page[EmailThreadSummary]:
        svc = self.client.get_service()
        users = svc.users()
        threads = users.threads()

        # Clamp limit
        if limit is None or limit <= 0:
            limit = 50
        limit = max(1, min(500, int(limit)))

        q = _build_gmail_query(filters)
        collected: list[EmailThreadSummary] = []
        next_token = cursor
        total_skipped = 0

        # page size to keep latency predictable; fetch more pages if needed
        page_size = min(limit, 100)

        while len(collected) < limit:
            list_req = threads.list(
                userId=settings.gmail_user_id,
                q=q,
                pageToken=next_token,
                maxResults=page_size,
                includeSpamTrash=False,
            )
            list_resp = _execute_with_retries(list_req)
            ids = [t["id"] for t in (list_resp.get("threads") or [])]

            if not ids:
                next_token = None
                break

            # For each thread id, fetch metadata and build summary (serial, with jitter & retries)
            for tid in ids:
                if len(collected) >= limit:
                    break
                try:
                    time.sleep(random.uniform(0.02, 0.06))  # small jitter to avoid burstiness
                    get_req = threads.get(
                        userId=settings.gmail_user_id,
                        id=tid,
                        format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date", "Message-Id"],
                    )
                    raw_thread = _execute_with_retries(get_req)
                    # Normalize (metadata still has headers/internalDate/snippet)
                    thread = normalize_thread(raw_thread)
                    summary = summarize_thread(thread)
                    if not include_snippets:
                        summary = EmailThreadSummary(
                            id=summary.id,
                            subject=summary.subject,
                            last_updated=summary.last_updated,
                            message_ids=summary.message_ids,
                            participants=summary.participants,
                            snippet=None,  # force drop
                            labels=summary.labels,
                            unread=summary.unread,
                        )
                    collected.append(summary)
                except HttpError as e:
                    # Skip on failure, warn; count retries happened inside _execute_with_retries
                    total_skipped += 1
                    log.warning(
                        "gmail.reader.thread_get_failed",
                        extra={"thread_id": tid, "status": getattr(e, "status_code", None)},
                    )
                except Exception:
                    total_skipped += 1
                    log.exception("gmail.reader.thread_get_exception", extra={"thread_id": tid})

            # Decide if another list page needed
            next_token = list_resp.get("nextPageToken")
            if not next_token:
                break

        log.info(
            "gmail.reader.list_threads_done",
            extra={
                "requested_limit": limit,
                "returned": len(collected),
                "skipped": total_skipped,
                "query": q,
                "had_more": bool(next_token),
            },
        )

        return Page[EmailThreadSummary](
            items=tuple(collected),
            next_cursor=next_token,
            total=None,  # overall total unknown; len(items) is the # of total collected thread summaries
        )

    def get_thread(
        self,
        thread_id: str,
        *,
        include_bodies: bool = True,
    ) -> EmailThread:
        svc = self.client.get_service()
        fmt = "full" if include_bodies else "metadata"

        get_req = svc.users().threads().get(
            userId=settings.gmail_user_id,
            id=thread_id,
            format=fmt,
        )
        raw_thread = _execute_with_retries(get_req)

        thread = normalize_thread(raw_thread)
        log.info(
            "gmail.reader.get_thread_done",
            extra={"thread_id": thread_id, "include_bodies": include_bodies, "message_count": len(thread.messages)},
        )
        return EmailThread(
            id=thread.id,
            subject=thread.subject,
            last_updated=thread.last_updated,
            labels=thread.labels,
            messages=thread.messages)
    