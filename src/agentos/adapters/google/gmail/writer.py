# src/agentos/adapters/google/gmail/writer.py
from __future__ import annotations

import base64
import mimetypes
import os
import random
import time
from dataclasses import dataclass
from email.message import EmailMessage as PyEmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Iterable, Optional, Sequence

from googleapiclient.errors import HttpError

from agentos.config import settings
from agentos.logging import get_logger
from agentos.adapters.google.gmail.client import GmailClient, GmailClientConfig
from agentos.ports.email import (
    EmailAddress,
    EmailPort,
    NewEmailDraft,
    ReplyDraft,
)

log = get_logger(__name__)


# ----------------------------- retry/backoff -----------------------------

def _should_retry_http_error(e: HttpError) -> bool:
    try:
        status = int(getattr(e, "status_code", None) or e.resp.status)  # type: ignore[attr-defined]
    except Exception:
        return False
    return status == 429 or 500 <= status <= 599


def _execute_with_retries(request, *, max_attempts: int = 3, base_delay: float = 0.5, cap_s: float = 8.0):
    attempt = 0
    while True:
        attempt += 1
        try:
            return request.execute()
        except HttpError as e:
            if attempt < max_attempts and _should_retry_http_error(e):
                delay = min(cap_s, base_delay * (2 ** (attempt - 1)))
                delay = delay * (0.5 + random.random())  # jitter in [0.5x, 1.5x]
                log.warning(
                    "gmail.writer.retrying_http_error",
                    extra={"attempt": attempt, "max_attempts": max_attempts, "delay_s": round(delay, 3)},
                )
                time.sleep(delay)
                continue
            log.exception("gmail.writer.request_failed")
            raise


# ----------------------------- helpers -----------------------------

def _addr_list_to_str(addrs: Optional[Sequence[EmailAddress]]) -> str:
    if not addrs:
        return ""
    parts = []
    for a in addrs:
        if a.name:
            parts.append(f"{a.name} <{a.email}>")
        else:
            parts.append(a.email)
    return ", ".join(parts)


def _guess_mime_type(path: Path) -> tuple[str, str]:
    # returns (maintype, subtype)
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        return ("application", "octet-stream")
    main, sub = mime.split("/", 1)
    return (main, sub)


def _attach_files(msg: PyEmailMessage, paths: Optional[Sequence[str]]) -> int:
    count = 0
    if not paths:
        return count
    for p in paths:
        path = Path(p)
        if not path.exists() or not path.is_file():
            log.warning("gmail.writer.attachment_missing", extra={"path": str(path)})
            continue
        maintype, subtype = _guess_mime_type(path)
        with path.open("rb") as f:
            data = f.read()
        # NOTE: Gmail raw RFC-822 size cap ≈ 35MB (after base64). Keep callers mindful.
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        count += 1
    return count


def _encode_message_to_raw(msg: PyEmailMessage) -> str:
    # Gmail expects base64url-encoded raw RFC-822
    bytes_msg = msg.as_bytes()
    b64 = base64.urlsafe_b64encode(bytes_msg).decode("utf-8")
    return b64


def _get_profile_email(service) -> Optional[str]:
    # users.getProfile → {"emailAddress": "...", ...}
    try:
        prof = _execute_with_retries(service.users().getProfile(userId=settings.gmail_user_id))
        return prof.get("emailAddress")
    except Exception:
        return None


def _compute_reply_all_recipients(
    service,
    draft: ReplyDraft,
    my_email: Optional[str],
) -> tuple[list[EmailAddress], list[EmailAddress]]:
    """
    If reply_all=True, fetch the thread (metadata) and include original To/Cc,
    excluding yourself. If reply_all=False, we send only to the original sender
    of the reference message (if available); otherwise leave recipient list empty
    (Gmail UI will usually fill it from the thread when sending).
    """
    # Default empty; Gmail will still attach the draft to the thread
    to_addrs: list[EmailAddress] = []
    cc_addrs: list[EmailAddress] = []

    if not draft.reply_all and not draft.reference_message_id:
        return to_addrs, cc_addrs

    try:
        t = _execute_with_retries(
            service.users().threads().get(
                userId=settings.gmail_user_id,
                id=draft.thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date", "Message-Id"],
            )
        )
    except Exception:
        return to_addrs, cc_addrs

    # Find target message inside thread (by Message-Id) or use last
    messages = t.get("messages", []) or []
    target = None
    if draft.reference_message_id:
        for m in messages:
            headers = {h["name"].lower(): h.get("value") for h in (m.get("payload", {}).get("headers") or [])}
            if headers.get("message-id") == draft.reference_message_id:
                target = m
                break
    if target is None and messages:
        target = messages[-1]

    if not target:
        return to_addrs, cc_addrs

    headers = {h["name"].lower(): h.get("value") for h in (target.get("payload", {}).get("headers") or [])}

    # Parse addresses
    from email.utils import getaddresses
    to_list = [EmailAddress(email=a[1], name=a[0] or None) for a in getaddresses([headers.get("to", "")])]
    cc_list = [EmailAddress(email a[1], name=a[0] or None) for a in getaddresses([headers.get("cc", "")])]  # type: ignore  # noqa: E231

    # Determine main "reply to" target = original sender
    from_list = [EmailAddress(email=a[1], name=a[0] or None) for a in getaddresses([headers.get("from", "")])]
    original_sender = from_list[0] if from_list else None

    def _not_me(a: EmailAddress) -> bool:
        return not my_email or a.email.lower() != my_email.lower()

    if draft.reply_all:
        to_addrs = [a for a in (to_list or []) if _not_me(a)]
        cc_addrs = [a for a in (cc_list or []) if _not_me(a)]
        # Ensure original sender is in To
        if original_sender and _not_me(original_sender) and all(original_sender.email.lower() != a.email.lower() for a in to_addrs):
            to_addrs.insert(0, original_sender)
    else:
        # reply (not reply-all): just the original sender if present and not me
        if original_sender and _not_me(original_sender):
            to_addrs = [original_sender]

    return to_addrs, cc_addrs


def _apply_reply_headers(msg: PyEmailMessage, draft: ReplyDraft) -> None:
    # If a reference Message-Id is provided, set threading headers
    if draft.reference_message_id:
        msg["In-Reply-To"] = draft.reference_message_id
        # Append to References safely (if already present it will be merged by Gmail)
        msg["References"] = draft.reference_message_id


# ----------------------------- writer -----------------------------

@dataclass
class GmailWriter(EmailPort):
    """
    Gmail write-side implementation of the EmailPort's draft/send methods.
    """
    client: GmailClient

    @classmethod
    def from_settings(cls) -> "GmailWriter":
        return cls(client=GmailClient(GmailClientConfig.from_settings_or_env()))

    # --- Writes ---

    def create_draft_new(self, draft: NewEmailDraft) -> str:
        service = self.client.get_service()

        msg = PyEmailMessage()
        to_str = _addr_list_to_str(draft.to)
        cc_str = _addr_list_to_str(draft.cc) if draft.cc else ""
        bcc_str = _addr_list_to_str(draft.bcc) if draft.bcc else ""

        if to_str:
            msg["To"] = to_str
        if cc_str:
            msg["Cc"] = cc_str
        if bcc_str:
            msg["Bcc"] = bcc_str

        msg["Subject"] = draft.subject or ""
        msg["Date"] = formatdate(localtime=False)
        msg["Message-Id"] = make_msgid()

        # Body
        if draft.body_html and draft.body_text:
            # multipart/alternative
            msg.set_content(draft.body_text)
            msg.add_alternative(draft.body_html, subtype="html")
        elif draft.body_html:
            msg.add_alternative(draft.body_html, subtype="html")
        else:
            msg.set_content(draft.body_text or "")

        # Attachments
        _attach_files(msg, draft.attachment_paths)

        raw = _encode_message_to_raw(msg)
        req = service.users().drafts().create(
            userId=settings.gmail_user_id,
            body={"message": {"raw": raw}},
        )
        resp = _execute_with_retries(req)
        draft_id = resp.get("id")
        log.info("gmail.writer.create_draft_new_done", extra={"draft_id": draft_id})
        return draft_id

    def create_draft_reply(self, draft: ReplyDraft) -> str:
        service = self.client.get_service()
        my_email = _get_profile_email(service)

        # Build MIME
        msg = PyEmailMessage()
        msg["Subject"] = ""  # Gmail UI typically prefixes "Re:" automatically; leaving blank is okay
        msg["Date"] = formatdate(localtime=False)
        msg["Message-Id"] = make_msgid()

        # Threading headers
        _apply_reply_headers(msg, draft)

        # Recipients per reply_all policy
        to_addrs, cc_addrs = _compute_reply_all_recipients(service, draft, my_email)
        to_str = _addr_list_to_str(to_addrs)
        cc_str = _addr_list_to_str(cc_addrs)
        if to_str:
            msg["To"] = to_str
        if cc_str:
            msg["Cc"] = cc_str

        # Body
        if draft.body_html and draft.body_text:
            msg.set_content(draft.body_text)
            msg.add_alternative(draft.body_html, subtype="html")
        elif draft.body_html:
            msg.add_alternative(draft.body_html, subtype="html")
        else:
            msg.set_content(draft.body_text or "")

        # Attachments
        _attach_files(msg, draft.attachment_paths)

        raw = _encode_message_to_raw(msg)
        req = service.users().drafts().create(
            userId=settings.gmail_user_id,
            body={
                "message": {
                    "raw": raw,
                    "threadId": draft.thread_id,  # ensure draft attaches to the conversation
                }
            },
        )
        resp = _execute_with_retries(req)
        draft_id = resp.get("id")
        log.info("gmail.writer.create_draft_reply_done", extra={"draft_id": draft_id, "thread_id": draft.thread_id})
        return draft_id

    def send_draft(self, draft_id: str) -> str:
        service = self.client.get_service()
        req = service.users().drafts().send(
            userId=settings.gmail_user_id,
            body={"id": draft_id},
        )
        resp = _execute_with_retries(req)
        msg_id = (resp.get("id") or resp.get("message", {}).get("id")) if isinstance(resp, dict) else None
        log.info("gmail.writer.send_draft_done", extra={"draft_id": draft_id, "message_id": msg_id})
        return msg_id or ""
