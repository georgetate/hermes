from __future__ import annotations

import random
import time
import base64
import json
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, TypeVar, cast

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from agentos.adapters.google.gcal.client import GCalClient
from agentos.adapters.google.gcal.normalizer import (
    normalize_calendar_ref,
    normalize_event_full,
    normalize_event_summary,
)
from agentos.config import settings
from agentos.logging_utils import get_logger
from agentos.ports.calendar import (
    CalendarRef,
    Event,
    EventFilter,
    EventSummary,
    ExpandMode,
    Page,
    TimeRange,
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
                    "gcal.writer.retrying_http_error",
                    extra={"attempt": attempt, "max_attempts": max_attempts, "delay_s": round(delay, 3)},
                )
                time.sleep(delay)
                continue
            log.exception("gcal.writer.request_failed")
            raise


# ----------------------------- cursor codec -----------------------------

@dataclass(frozen=True)
class _Cursor:
    """Composite cursor to paginate across multiple calendars."""
    cal_index: int
    page_token: Optional[str]

    def encode(self) -> str:
        raw = json.dumps({"ci": self.cal_index, "pt": self.page_token or ""}).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def decode(s: Optional[str]) -> _Cursor:
        if not s:
            return _Cursor(cal_index=0, page_token=None)
        try:
            data = json.loads(base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8"))
            return _Cursor(cal_index=int(data.get("ci", 0)), page_token=(data.get("pt") or None))
        except Exception:
            # Corrupt/foreign cursor → start over
            return _Cursor(cal_index=0, page_token=None)


# ----------------------------- filters → q -----------------------------

def _build_q(filters: Optional[EventFilter]) -> Optional[str]:
    """
    Google Calendar only exposes a single free-text 'q' parameter (no structured
    operators like 'attendee:' are guaranteed). We best-effort concatenate
    available terms and let the API perform full-text match.
    """
    if not filters:
        return None

    terms: List[str] = []
    if filters.title_contains:
        terms.append(filters.title_contains)
    if filters.attendee_contains:
        terms.append(filters.attendee_contains)
    if filters.free_text:
        terms.append(filters.free_text)

    q = " ".join(t for t in terms if t)
    return q or None


def _post_filter_has_conference(items: Iterable[EventSummary], flag: Optional[bool]) -> List[EventSummary]:
    if flag is None:
        return list(items)
    if flag is True:
        return [e for e in items if e.has_conference_link]
    # flag is False → either False or None is acceptable as "no"
    return [e for e in items if not e.has_conference_link]


# ----------------------------- Reader -----------------------------

class GCalReader:
    """
    Google Calendar read adapter.
    - Provides list_calendars(), list_events(), get_event(), find_between()
    - Aggregates across multiple calendars with a composite cursor.
    """

    def __init__(self, client: Optional[GCalClient] = None) -> None:
        self.client = client or GCalClient.from_settings()

    # ---------- Discovery ----------

    def list_calendars(self) -> Sequence[CalendarRef]:
        try:
            service = self.client.get_service()
            # calendarList.list is paginated; most users have few calendars, but paginate defensively
            out: List[CalendarRef] = []
            token: Optional[str] = None
            while True:
                req = (
                    service.calendarList()
                    .list(
                        pageToken=token,
                        minAccessRole="reader",  # show readable calendars
                        maxResults=250,
                    )
                )
                resp = _execute_with_retries(req)
                for raw in resp.get("items", []) or []:
                    ref = normalize_calendar_ref(raw)
                    if ref:
                        out.append(ref)
                token = cast(Optional[str], resp.get("nextPageToken"))
                if not token:
                    break
            return tuple(out)
        except Exception as e:
            log.warning("gcal.reader.list_calendars_failed", extra={"error": str(e)})
            raise

    # ---------- Reads (summaries) ----------

    def list_events(
        self,
        window: TimeRange,
        *,
        calendar_ids: Optional[Sequence[str]] = None,
        include_cancelled: bool = False,
        expand: ExpandMode = "none",
        filters: Optional[EventFilter] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Page[EventSummary]:
        """
        Return EventSummary rows across one or many calendars with stable pagination.
        """
        try:
            service = self.client.get_service()

            # Determine calendars to search
            if calendar_ids is None or len(calendar_ids) == 0:
                # Default to the configured user calendar (usually "primary")
                calendar_ids = (settings.gcal_user_id,)  # type: ignore[attr-defined]
            calendars = list(calendar_ids)

            # Decode composite cursor
            c = _Cursor.decode(cursor)
            cal_index = c.cal_index
            page_token = c.page_token

            single_events = expand == "instances"
            order_by = "startTime" if single_events else None

            q = _build_q(filters)

            # Google page size cap is 250; we cap at 100 to keep latency predictable (mirrors Gmail choice)
            page_size = min(limit, 100)
            collected: List[EventSummary] = []
            next_cursor: Optional[str] = None

            while cal_index < len(calendars) and len(collected) < limit:
                cal_id = calendars[cal_index]

                params: Dict[str, Any] = {
                    "calendarId": cal_id,
                    "singleEvents": single_events,
                    "timeMin": window.start.astimezone(timezone.utc).isoformat(),
                    "timeMax": window.end.astimezone(timezone.utc).isoformat(),
                    "showDeleted": include_cancelled,  # Google uses 'cancelled' status; this exposes them
                    "maxResults": page_size,
                }
                if page_token:
                    params["pageToken"] = page_token
                if q:
                    params["q"] = q
                if order_by:
                    params["orderBy"] = order_by

                # For 'singleEvents=False', Google returns series masters; for True, concrete instances.
                req = service.events().list(**params)
                resp = _execute_with_retries(req)

                items = resp.get("items", []) or []
                # Defensive: if include_cancelled=False, filter out status=cancelled that can sneak in
                if not include_cancelled:
                    items = [it for it in items if it.get("status") != "cancelled"]

                for raw in items:
                    try:
                        summary = normalize_event_summary(raw, calendar_id=cal_id, calendar_tz=None)
                        if summary:
                            collected.append(summary)
                            if len(collected) >= limit:
                                break
                    except Exception as ne:
                        log.debug(
                            "gcal.reader.normalize_event_summary_skip",
                            extra={"error": str(ne), "event_id": raw.get("id"), "calendar_id": cal_id},
                        )

                page_token = cast(Optional[str], resp.get("nextPageToken"))
                if len(collected) >= limit:
                    # Build next_cursor at current calendar + remaining page token
                    next_cursor = _Cursor(cal_index=cal_index, page_token=page_token).encode() if page_token else _Cursor(cal_index=cal_index + 1, page_token=None).encode() if (cal_index + 1) < len(calendars) else None
                    break

                if page_token:
                    # More pages in the same calendar; continue loop
                    continue

                # Move to next calendar
                cal_index += 1
                page_token = None

            # Post-filter on conference flag if requested
            collected = _post_filter_has_conference(collected, filters.has_conference_link if filters else None)

            # If we exhausted the current calendar and moved on, derive the final next_cursor
            if next_cursor is None and cal_index < len(calendars):
                next_cursor = _Cursor(cal_index=cal_index, page_token=page_token).encode()
            elif next_cursor is None and cal_index >= len(calendars):
                next_cursor = None

            return Page(items=tuple(collected), next_cursor=next_cursor, total=None)

        except Exception as e:
            log.warning(
                "gcal.reader.list_events_failed",
                extra={
                    "error": str(e),
                    "expand": expand,
                    "include_cancelled": include_cancelled,
                    "limit": limit,
                },
            )
            raise

    # ---------- Read (single) ----------

    def get_event(self, event_id: str, calendar_id: str) -> Event:
        try:
            service = self.client.get_service()
            req = service.events().get(calendarId=calendar_id, eventId=event_id)
            resp = _execute_with_retries(req)
            evt = normalize_event_full(resp, calendar_id=calendar_id, calendar_tz=None)
            if not evt:
                raise RuntimeError("Failed to normalize event")
            return evt
        except Exception as e:
            log.warning(
                "gcal.reader.get_event_failed",
                extra={"error": str(e), "event_id": event_id, "calendar_id": calendar_id},
            )
            raise

    # ---------- Convenience: expand window to full Events ----------

    def find_between(
        self,
        window: TimeRange,
        *,
        calendar_ids: Optional[Sequence[str]] = None,
        include_cancelled: bool = False,
    ) -> Sequence[Event]:
        """
        Convenience wrapper that returns fully realized Event objects for all concrete
        occurrences overlapping the window. Uses list_events(expand='instances') then
        fetches each item with get_event().
        """
        try:
            # Collect all occurrences (no explicit limit here; the port does not define one)
            cursor: Optional[str] = None
            out: List[Event] = []
            while True:
                page = self.list_events(
                    window,
                    calendar_ids=calendar_ids,
                    include_cancelled=include_cancelled,
                    expand="instances",
                    filters=None,
                    limit=200,
                    cursor=cursor,
                )
                for s in page.items:
                    try:
                        out.append(self.get_event(s.id, s.calendar_id))
                    except Exception as ge:
                        log.debug(
                            "gcal.reader.find_between_get_event_skip",
                            extra={"error": str(ge), "event_id": s.id, "calendar_id": s.calendar_id},
                        )
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
            return tuple(out)
        except Exception as e:
            log.warning("gcal.reader.find_between_failed", extra={"error": str(e)})
            raise
