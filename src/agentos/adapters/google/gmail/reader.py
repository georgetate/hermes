# src/agentos/adapters/google/gmail/reader.py
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from googleapiclient.errors import HttpError

from agentos.logging import get_logger
from agentos.config import settings
from agentos.adapters.google.gmail.client import GmailClient, GmailClientConfig
from agentos.adapters.google.gmail.normalizer import (
    normalize_thread,
    summarize_thread,
)
from agentos.ports.email import (
    EmailPort,
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


def _execute_with_retries(request, *, max_attempts: int = 3, base_delay: float = 0.5, cap_s: float = 8.0):
    """
    Execute a Google API request with exponential backoff + jitter for 429/5xx.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return request.execute()
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
class GmailReader(EmailPort):
    """
    Gmail adapter read-side implementation mapped to the EmailPort.
    - list_threads: summaries via threads.list → threads.get(format="metadata")
    - get_thread: metadata or full, then normalize
    """
    client: GmailClient

    @classmethod
    def from_settings(cls) -> "GmailReader":
        return cls(client=GmailClient(GmailClientConfig.from_settings_or_env()))

    # --- Reads (threads-first) ---

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
        total_retried = 0

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
            total=None,  # overall total unknown; len(items) is page size
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
        return thread
