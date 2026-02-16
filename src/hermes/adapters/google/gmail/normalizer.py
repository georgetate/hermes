"""Normalization helpers for Gmail payloads.

Maps Gmail thread/message payloads into provider-agnostic email DTOs used by
the hermes email port.
"""

from __future__ import annotations

import email.utils
import re
from base64 import urlsafe_b64decode
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from hermes.logging_utils import get_logger
from hermes.ports.email import (
    AttachmentMeta,
    EmailAddress,
    EmailMessage,
    EmailThread,
    EmailThreadSummary,
)

log = get_logger(__name__)

# -------------------- header helpers --------------------

HEADER_NORMALIZE = {
    "subject": "Subject",
    "from": "From",
    "to": "To",
    "cc": "Cc",
    "bcc": "Bcc",
    "reply-to": "Reply-To",
    "date": "Date",
    "message-id": "Message-Id",
    "in-reply-to": "In-Reply-To",
    "references": "References",
    "content-id": "Content-Id",
}

_addr_splitter = re.compile(r",(?![^\<]*\>)")  # split commas not inside <...>


def _get_header(headers: Sequence[dict[str, str]], name: str) -> Optional[str]:
    want = HEADER_NORMALIZE.get(name.lower(), name)
    for h in headers or []:
        if h.get("name", "").lower() == want.lower():
            return h.get("value")
    return None


def _parse_addresses(value: Optional[str]) -> list[EmailAddress]:
    if not value:
        return []
    parts = _addr_splitter.split(value)
    out: list[EmailAddress] = []
    for p in parts:
        name, addr = email.utils.parseaddr(p)
        addr = (addr or "").strip()
        if not addr:
            continue
        name = (name or "").strip() or None
        out.append(EmailAddress(email=addr, name=name))
    return out


def _parse_rfc2822_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        log.debug("gmail.normalizer.date_parse_failed", extra={"date": s})
        return None


def _internal_ms_to_dt(ms: Optional[int | str]) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        if isinstance(ms, str):
            ms = int(ms)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def _decode_b64(data_b64: Optional[str]) -> Optional[bytes]:
    if not data_b64:
        return None
    try:
        return urlsafe_b64decode(data_b64.encode("utf-8"))
    except Exception:
        try:
            return urlsafe_b64decode((data_b64 + "===")).decode("utf-8")  # type: ignore[return-value]
        except Exception:
            return None


# -------------------- body/attachment walker --------------------

def _extract_content_id(part: dict[str, Any]) -> Optional[str]:
    for h in (part.get("headers") or []):
        if h.get("name", "").lower() == "content-id":
            return h.get("value")
    return None


def _walk_payload_collect(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str], list[AttachmentMeta]]:
    """
    Collect:
      - first text/plain body
      - first text/html body
      - attachment metadata (no binary download)
    """
    text: Optional[str] = None
    html: Optional[str] = None
    atts: list[AttachmentMeta] = []

    def walk(part: dict[str, Any]) -> None:
        nonlocal text, html, atts
        mime = part.get("mimeType")
        body = part.get("body", {}) or {}
        data_b64 = body.get("data")
        attachment_id = body.get("attachmentId")
        filename = part.get("filename") or None

        # Recurse
        for p in (part.get("parts") or []):
            walk(p)

        # Attachment?
        if attachment_id:
            atts.append(
                AttachmentMeta(
                    id=attachment_id,
                    filename=filename or "",
                    mime_type=mime,
                    size_bytes=body.get("size"),
                    content_id=_extract_content_id(part),
                )
            )
            return

        # Inline body?
        if data_b64:
            raw = _decode_b64(data_b64)
            if raw is None:
                return
            try:
                text_value = raw.decode("utf-8", errors="replace")
            except Exception:
                text_value = None

            if mime == "text/plain" and text is None:
                text = text_value
            elif mime == "text/html" and html is None:
                html = text_value

    if payload:
        walk(payload)

    return text, html, atts


# -------------------- normalizers --------------------

def normalize_message(raw: dict[str, Any]) -> EmailMessage:
    """
    Gmail users.messages.get(..., format="full") → EmailMessage
    """
    payload = raw.get("payload") or {}
    headers = payload.get("headers", []) or []

    subject = _get_header(headers, "Subject") or ""
    from_h = _get_header(headers, "From")
    to_h = _get_header(headers, "To")
    cc_h = _get_header(headers, "Cc")
    bcc_h = _get_header(headers, "Bcc")
    date_h = _get_header(headers, "Date")

    from_list = _parse_addresses(from_h)
    from_addr = from_list[0] if from_list else EmailAddress(email="", name=None)

    to_addrs = _parse_addresses(to_h)
    cc_addrs = _parse_addresses(cc_h)
    bcc_addrs = _parse_addresses(bcc_h)

    date_dt = _parse_rfc2822_date(date_h)
    internal_dt = _internal_ms_to_dt(raw.get("internalDate"))

    # Prefer provider's internal timestamp if available; else fall back to Date header; else now (UTC).
    internal_ts = internal_dt or date_dt or datetime.now(timezone.utc)

    body_text, body_html, attachments = _walk_payload_collect(payload)

    labels = list(raw.get("labelIds", []) or [])
    snippet = raw.get("snippet")

    return EmailMessage(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        subject=subject,
        from_=from_addr,
        to=tuple(to_addrs),
        cc=tuple(cc_addrs),
        bcc=tuple(bcc_addrs),
        snippet=snippet,
        body_text=body_text,
        body_html=body_html,
        internal_ts=internal_ts,
        labels=tuple(labels),
        has_attachments=len(attachments) > 0,
        attachments=tuple(attachments),
    )


def _message_timestamp(m: EmailMessage) -> datetime:
    # Your DTO guarantees a datetime here (we populated it), so just return it
    return m.internal_ts


def normalize_thread(raw_thread: dict[str, Any]) -> EmailThread:
    """
    Gmail users.threads.get(..., format="full") → EmailThread with messages sorted oldest→newest.
    """
    thread_id = raw_thread.get("id", "")
    raw_messages: list[dict[str, Any]] = raw_thread.get("messages", []) or []

    # Normalize all messages
    messages = [normalize_message(m) for m in raw_messages]

    # Sort oldest → newest by internal_ts
    messages.sort(key=_message_timestamp)

    # Subject: use last non-empty subject across the thread; else empty
    subject = ""
    for m in messages:
        if m.subject:
            subject = m.subject

    # Last updated = newest message timestamp
    last_updated = _message_timestamp(messages[-1]) if messages else datetime.now(timezone.utc)

    # Thread labels = union of message labels
    label_union = sorted({lab for m in messages for lab in m.labels})

    return EmailThread(
        id=thread_id,
        subject=subject,
        last_updated=last_updated,
        labels=tuple(label_union),
        messages=tuple(messages),
    )


def summarize_thread(thread: EmailThread) -> EmailThreadSummary:
    """
    Build a lightweight summary from a normalized EmailThread.
    """
    # Participants = unique from from_/to/cc across the thread
    seen: dict[str, EmailAddress] = {}

    def add_many(items: Iterable[EmailAddress]) -> None:
        for a in items:
            if a and a.email and a.email not in seen:
                seen[a.email] = a

    for m in thread.messages:
        if m.from_:
            add_many([m.from_])
        add_many(m.to)
        add_many(m.cc)

    # Unread if any message has UNREAD label
    unread = any("UNREAD" in m.labels for m in thread.messages)

    # Use latest message snippet if available
    snippet = thread.messages[-1].snippet if thread.messages else None

    return EmailThreadSummary(
        id=thread.id,
        subject=thread.subject,
        last_updated=thread.last_updated,
        message_ids=tuple(m.id for m in thread.messages),
        participants=tuple(seen.values()),
        snippet=snippet,
        labels=tuple(thread.labels),
        unread=unread,
    )
