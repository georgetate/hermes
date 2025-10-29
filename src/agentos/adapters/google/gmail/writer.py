# src/agentos/adapters/google/gmail/writer.py
from __future__ import annotations

import base64
import mimetypes

import random
import time
from dataclasses import dataclass
from email.message import EmailMessage as PyEmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional, Sequence, Protocol, TypeVar, List, Any, TypedDict, cast


from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest


from agentos.config import settings
from agentos.logging_utils import get_logger
from agentos.adapters.google.gmail.client import GmailClient, GmailClientConfig
from agentos.ports.email import (
    EmailAddress,
    NewEmailDraft,
    ReplyDraft,
)

log = get_logger(__name__)


# ----------------------------- retry/backoff -----------------------------

def _should_retry_http_error(e: HttpError) -> bool:
    try:
        status = int(getattr(e, "status_code", None) or e.resp.status)  
    except Exception:
        return False
    return status == 429 or 500 <= status <= 599


T = TypeVar('T')
def _execute_with_retries(request: HttpRequest, *, max_attempts: int = 3, base_delay: float = 0.5, cap_s: float = 8.0) -> T: # type: ignore
    attempt = 0
    while True:
        attempt += 1
        try:
            result = request.execute()
            return cast(T, result)
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


# --- custom typing surface for JUST what we call on the Gmail discovery client ---
class _ThreadsResource(Protocol):
    def get(
        self,
        *,
        userId: str,
        id: str,
        format: str,
        metadataHeaders: List[str],
    ) -> HttpRequest: ...

class _UsersResource(Protocol):
    def threads(self) -> _ThreadsResource: ...
    def getProfile(self, *, userId: str) -> HttpRequest: ...

class GmailLikeService(Protocol):
    def users(self) -> _UsersResource: ...

# --- TypedDicts for the shapes we actually read from the thread metadata response ---
class _Header(TypedDict, total=False):
    name: str
    value: str

class _Payload(TypedDict, total=False):
    headers: List[_Header]

class _Message(TypedDict, total=False):
    payload: _Payload

class _ThreadResponse(TypedDict, total=False):
    messages: List[_Message]


def _get_profile_email(service: GmailLikeService) -> Optional[str]:
    # users.getProfile → {"emailAddress": "...", ...}
    try:
        prof = _execute_with_retries(service.users().getProfile(userId=settings.gmail_user_id))
        return prof.get("emailAddress")
    except Exception:
        return None


def _compute_reply_all_recipients(
    service: GmailLikeService,
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
        t: _ThreadResponse = _execute_with_retries(
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
    target: Optional[_Message] = None
    if draft.reference_message_id:
        for m in messages:
            headers = {h.get("name", "").lower(): h.get("value") for h in (m.get("payload", {}).get("headers") or [])}
            if headers.get("message-id") == draft.reference_message_id:
                target = m
                break
    if target is None and messages:
        target = messages[-1]

    if not target:
        return to_addrs, cc_addrs

    headers = {h.get("name", "").lower(): h.get("value") for h in (target.get("payload", {}).get("headers") or [])}

    # Parse addresses
    from email.utils import getaddresses
    to_list = [EmailAddress(email=a[1], name=a[0] or None) for a in getaddresses([headers.get("to", "") or ""])]
    cc_list = [EmailAddress(email=a[1], name=a[0] or None) for a in getaddresses([headers.get("cc", "") or ""])]

    # Determine main "reply to" target = original sender
    from_list = [EmailAddress(email=a[1], name=a[0] or None) for a in getaddresses([headers.get("from", "") or ""])]
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


def _normalize_addresses(addrs: Sequence[str | EmailAddress] | None) -> list[EmailAddress]:
    """
    Ensures all items in the list are EmailAddress objects.
    Accepts raw strings or already-built EmailAddress instances.
    """
    if not addrs:
        return []
    normalized: list[EmailAddress] = []
    for addr in addrs:
        if isinstance(addr, EmailAddress):
            normalized.append(addr)
        elif isinstance(addr, str):
            normalized.append(EmailAddress(email=addr))
        else:
            raise TypeError(f"Unsupported address type: {type(addr)}")
    return normalized


# ----------------------------- writer -----------------------------

@dataclass
class GmailWriter():
    """
    Gmail write-side implementation of the EmailPort's draft/send methods.
    """
    client: GmailClient

    @classmethod
    def from_settings(cls) -> "GmailWriter":
        return cls(client=GmailClient(GmailClientConfig.from_settings_or_env()))

    # --- Writes ---

    @classmethod
    def _build_new_draft(
        cls,
        to: list[str],
        subject: str,
        body_text: str = "",
        body_html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> NewEmailDraft:
        return NewEmailDraft(
            to=_normalize_addresses(to),
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=_normalize_addresses(cc),
            bcc=_normalize_addresses(bcc),
            attachment_paths=attachment_paths,
        )
    
    @classmethod
    def _build_reply_draft(
        cls,
        thread_id: str,
        body_text: str = "",
        body_html: str | None = None,
        attachment_paths: list[str] | None = None,
        reply_all: bool = False,
        reference_message_id: str | None = None,
    ) -> ReplyDraft:
        """
        Factory for a reply draft tied to an existing Gmail thread.
        If reply_all is False, reference_message_id must be provided to indicate
        which specific message this reply targets.
        """
        if not reply_all and not reference_message_id:
            raise ValueError(
                "reference_message_id is required when reply_all=False "
                "(must specify which message to reply to)."
            )

        return ReplyDraft(
            thread_id=thread_id,
            body_text=body_text,
            body_html=body_html,
            attachment_paths=attachment_paths or [],
            reply_all=reply_all,
            reference_message_id=reference_message_id,
        )


    def create_new_draft(self, draft: NewEmailDraft) -> str:
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

    def create_reply_draft(self, draft: ReplyDraft, allow_reply_self: bool = False) -> str:
        service = self.client.get_service()
        if not allow_reply_self:
            my_email = _get_profile_email(service)
        else:
            my_email = None
            log.debug("gmail.writer.reply_self_allowed", extra={"thread_id": draft.thread_id})

        
        # Build MIME
        msg = PyEmailMessage()
        msg["Subject"] = ""  # Gmail UI typically prefixes "Re:" automatically; leaving blank is okay
        msg["Date"] = formatdate(localtime=False)
        msg["Message-Id"] = make_msgid()

        # Threading headers
        _apply_reply_headers(msg, draft)

        # Recipients per reply_all policy
        to_addrs, cc_addrs = _compute_reply_all_recipients(service, draft, my_email=my_email)
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
