# agent_hello.py
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.header import decode_header

from google.oauth2.credentials import Credentials



# -------- Utilities --------

def safe_call(fn, *args, **kwargs):
    """Wrap Google API calls: return None on HttpError with a short message."""
    try:
        return fn(*args, **kwargs)
    except HttpError as e:
        print(f"[API error] {getattr(e, 'status_code', '')} {e}")
        return None


def decode_mime_header_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


# -------- Gmail: list + fetch + normalize --------

def gmail_list_unread_ids_exact(
    service, need: int = 5, label_ids: Tuple[str, ...] = ("INBOX", "UNREAD")
) -> List[str]:
    ids: List[str] = []
    token: Optional[str] = None

    while len(ids) < need:
        resp = safe_call(
            service.users().messages().list(
                userId="me",
                labelIds=list(label_ids),
                maxResults=min(need - len(ids), 100),
                pageToken=token,
            ).execute
        )
        if not resp:
            break
        new_msgs = [m["id"] for m in resp.get("messages", [])]
        ids.extend(new_msgs)
        token = resp.get("nextPageToken")

        # Defensive breaks: no more token OR this page had no messages
        if not token or not new_msgs:
            break

    return ids[:need]


def gmail_batch_get(service, ids: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for mid in ids:
        msg = safe_call(service.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=[
            "From", "Subject", "Date"
        ]).execute)
        if msg:
            out.append(msg)
    return out


def parse_from(header_value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Very simple 'From:' parser: 'Name <email>' or just 'email'.
    If you want bulletproof parsing, use email.utils.parseaddr.
    """
    if not header_value:
        return None, None
    from email.utils import parseaddr
    name, email_ = parseaddr(header_value)
    name = decode_mime_header_value(name) if name else None
    return (name or None), (email_ or None)


def header_lookup(payload_headers: List[Dict[str, str]], name: str) -> Optional[str]:
    for h in payload_headers:
        if h.get("name") == name:
            return h.get("value")
    return None


def gmail_normalize(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return items with the contract: id, from_name, from_email, subject, date_utc_iso, date_local_iso, snippet, labels"""
    items: List[Dict[str, Any]] = []
    for m in msgs:
        headers = m.get("payload", {}).get("headers", [])
        subj_raw = header_lookup(headers, "Subject")
        date_raw = header_lookup(headers, "Date")
        from_raw = header_lookup(headers, "From")

        subject = decode_mime_header_value(subj_raw) or "(no subject)"
        from_name, from_email = parse_from(from_raw)

        # Convert RFC2822 Date → ISO strings (best effort)
        date_utc_iso, date_local_iso = None, None
        if date_raw:
            from email.utils import parsedate_to_datetime
            try:
                dt = parsedate_to_datetime(date_raw)  # timezone-aware if header had tz

                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                date_utc_iso = dt.astimezone(timezone.utc).isoformat()
                date_local_iso = dt.astimezone().isoformat() # system local timezone

            except Exception:
                pass

        items.append({
            "id": m.get("id"),
            "from_name": from_name,
            "from_email": from_email,
            "subject": subject,
            "date_utc_iso": date_utc_iso,
            "date_local_iso": date_local_iso,
            "snippet": m.get("snippet"),
            "labels": m.get("labelIds", []),
        })
    return items


def gmail_normalize_unread(service, max_results: int = 5) -> List[Dict[str, Any]]:
    ids = gmail_list_unread_ids_exact(service, need=max_results)
    if not ids:
        return []
    raw = gmail_batch_get(service, ids)
    return gmail_normalize(raw)


# -------- Calendar: window helpers + normalize --------

def cal_list_window(
    service, start: datetime, end: datetime, calendar_id: str = "primary", n: int = 50
) -> List[Dict[str, Any]]:
    resp = safe_call(
        service.events().list(
            calendarId=calendar_id,
            timeMin=start.astimezone(timezone.utc).isoformat(),
            timeMax=end.astimezone(timezone.utc).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=n,
        ).execute
    )
    if not resp:
        return []
    return resp.get("items", [])


def calendar_normalize(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for e in items:
        start = e.get("start", {}) or {}
        end = e.get("end", {}) or {}

        # All-day events use 'date' (YYYY-MM-DD), timed events use 'dateTime'
        if "date" in start:
            start_iso = start["date"]  # YYYY-MM-DD
            end_iso = end.get("date", start_iso)
            is_all_day = True
        else:
            start_iso = start.get("dateTime")
            end_iso = end.get("dateTime", start_iso)
            is_all_day = False

        out.append({
            "summary": e.get("summary") or "(no title)",
            "location": e.get("location"),
            "start_iso": start_iso,
            "end_iso": end_iso,
            "is_all_day": is_all_day,
        })
    return out


def calendar_normalize_upcoming(service, n: int = 5) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=30)   # tweak as desired
    raw = cal_list_window(service, start=now, end=horizon, n=n)
    norm = calendar_normalize(raw)
    return norm[:n]


# -------- Pretty printers --------

def print_gmail(items: List[Dict[str, Any]]):
    print(f"--- Unread ({len(items)}) ---")
    for it in items:
        ts = it.get("date_utc_iso") or it.get("date_local_iso") or ""
        who = f"{(it.get('from_name') or '').strip()} <{(it.get('from_email') or '').strip()}>".strip()
        subj = it.get("subject") or "(no subject)"
        print(f"[{ts}] {subj} — {who}")


def print_calendar(items: List[Dict[str, Any]]):
    print(f"\n--- Upcoming ({len(items)}) ---")
    for e in items:
        when = e["start_iso"]
        title = e.get("summary") or "(no title)"
        loc = f" ({e['location']})" if e.get("location") else ""
        print(f"{when}  {title}{loc}")


# -------- Main --------

def build_services(credentials) -> Tuple[Any, Any]:
    """
    Assumes you already have OAuth creds flow elsewhere and are now building services.
    Replace with your credential loader as needed.
    """
    # Example:
    # from google.oauth2.credentials import Credentials
    # creds = Credentials.from_authorized_user_file(".credentials/token.json", SCOPES)
    gmail = build("gmail", "v1", credentials=credentials)       # build(serviceName, version, credentials=creds)
    cal = build("calendar", "v3", credentials=credentials)
    return gmail, cal


def main():
    creds = Credentials.from_authorized_user_file('/home/george/trillion-agentos/.credentials/token.json') # use same scope as token to avoid mismatch

    parser = argparse.ArgumentParser(description="Mini agent: print unread emails and upcoming events.")
    parser.add_argument("--emails", type=int, default=5, help="How many unread emails to show")
    parser.add_argument("--events", type=int, default=5, help="How many upcoming events to show")
    parser.add_argument("--json", action="store_true", help="Output in JSON format instead of pretty print")
    args = parser.parse_args()

    gmail, cal = build_services(creds)

    gmail_items = gmail_normalize_unread(gmail, max_results=args.emails)
    cal_items = calendar_normalize_upcoming(cal, n=args.events)

    if args.json:
        payload = {
            "emails": gmail_items,
            "events": cal_items,
        }
        print(json.dumps(payload, indent=2))
    else:
        print_gmail(gmail_items)
        print_calendar(cal_items)


if __name__ == "__main__":
    main()
