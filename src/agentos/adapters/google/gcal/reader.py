from __future__ import annotations

import random
import time
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
)

log = get_logger(__name__)


# ----------------------------- retry/backoff -----------------------------

def _should_retry_http_error(e: HttpError) -> bool:
    try:
        status = int(getattr(e, "status_code", None) or e.resp.status)
    except Exception:
        return False
    return status == 429 or 500 <= status <= 599


T = TypeVar("T")


def _execute_with_retries(
    request: HttpRequest,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    cap_s: float = 8.0,
) -> T:  # type: ignore
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
                    extra={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay_s": round(delay, 3),
                    },
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
        raw = json.dumps({"ci": self.cal_index, "pt": self.page_token or ""}).encode(
            "utf-8"
        )
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def decode(s: Optional[str]) -> _Cursor:
        if not s:
            return _Cursor(cal_index=0, page_token=None)
        try:
            data = json.loads(
                base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")
            )
            return _Cursor(
                cal_index=int(data.get("ci", 0)),
                page_token=(data.get("pt") or None),
            )
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


def _post_filter_has_conference(
    items: Iterable[EventSummary], flag: Optional[bool]
) -> List[EventSummary]:
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
    - Aggregates across multiple calendars with a composite cursor (windowed mode).
    - Supports full sync (initial) and incremental sync (delta) via syncToken.
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
                req = service.calendarList().list(
                    pageToken=token,
                    minAccessRole="reader",  # show readable calendars
                    maxResults=250,
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
            log.warning(
                "gcal.reader.list_calendars_failed",
                extra={"error": str(e)},
            )
            raise

    # ---------- events syncing ----------

    def sync_events(
        self,
        *,
        calendar_id: str,
        sync_token: str,
        include_cancelled: bool = True,
        filters: Optional[EventFilter] = None,
    ) -> Page[EventSummary]:
        """Incremental sync using a syncToken (single calendar). Always exhausts pages."""
        try:
            service = self.client.get_service()

            page_size = 100  # fixed page size; we always exhaust all pages
            summaries: List[EventSummary] = []
            next_sync_token: Optional[str] = None

            page_token: Optional[str] = None
            first_page = True
            last_resp: Optional[Dict[str, Any]] = None

            while True:
                params: Dict[str, Any] = {
                    "calendarId": calendar_id,
                    "showDeleted": True,  # required with syncToken
                    "maxResults": page_size,
                }
                if first_page:
                    params["syncToken"] = sync_token
                    first_page = False
                else:
                    params["pageToken"] = page_token

                req = service.events().list(**params)
                resp = _execute_with_retries(req)
                last_resp = resp

                items = resp.get("items", []) or []

                if not include_cancelled:
                    items = [it for it in items if it.get("status") != "cancelled"]

                for raw in items:
                    try:
                        summary = normalize_event_summary(
                            raw, calendar_id=calendar_id, calendar_tz=None
                        )
                        if summary:
                            summaries.append(summary)
                    except Exception as ne:
                        log.debug(
                            "gcal.reader.normalize_event_summary_skip",
                            extra={
                                "error": str(ne),
                                "event_id": raw.get("id"),
                                "calendar_id": calendar_id,
                            },
                        )

                page_token = cast(Optional[str], resp.get("nextPageToken"))
                if not page_token:
                    break

            if last_resp is not None:
                next_sync_token = cast(Optional[str], last_resp.get("nextSyncToken"))

            summaries = _post_filter_has_conference(
                summaries, filters.has_conference_link if filters else None
            )

            return Page(
                items=tuple(summaries),
                next_cursor=None,
                total=None,
                next_sync_token=next_sync_token,
            )
        except Exception as e:
            log.warning(
                "gcal.reader.sync_events_failed",
                extra={
                    "error": str(e),
                    "include_cancelled": include_cancelled,
                    "calendar_id": calendar_id,
                },
            )
            raise


    def full_sync_events(
        self,
        *,
        calendar_id: str,
        include_cancelled: bool = True,
        expand: ExpandMode = 'none',
        filters: Optional[EventFilter] = None,
    ) -> Page[EventSummary]:
        """Initial full sync (no time bounds, no syncToken; single calendar). Always exhausts pages."""
        try:
            service = self.client.get_service()

            single_events = expand == "instances"
            order_by = "startTime" if single_events else None
            q = _build_q(filters)

            page_size = 100
            summaries: List[EventSummary] = []
            next_sync_token: Optional[str] = None

            page_token: Optional[str] = None
            last_resp: Optional[Dict[str, Any]] = None

            while True:
                params: Dict[str, Any] = {
                    "calendarId": calendar_id,
                    "showDeleted": include_cancelled,
                    "maxResults": page_size,
                }
                if page_token:
                    params["pageToken"] = page_token
                if q:
                    params["q"] = q
                if order_by:
                    params["orderBy"] = order_by
                if single_events is not None:
                    params["singleEvents"] = single_events

                req = service.events().list(**params)
                resp = _execute_with_retries(req)
                last_resp = resp

                items = resp.get("items", []) or []

                if not include_cancelled:
                    items = [it for it in items if it.get("status") != "cancelled"]

                for raw in items:
                    try:
                        summary = normalize_event_summary(
                            raw, calendar_id=calendar_id, calendar_tz=None
                        )
                        if summary:
                            summaries.append(summary)
                    except Exception as ne:
                        log.debug(
                            "gcal.reader.normalize_event_summary_skip",
                            extra={
                                "error": str(ne),
                                "event_id": raw.get("id"),
                                "calendar_id": calendar_id,
                            },
                        )

                page_token = cast(Optional[str], resp.get("nextPageToken"))
                if not page_token:
                    break

            if last_resp is not None:
                next_sync_token = cast(Optional[str], last_resp.get("nextSyncToken"))

            summaries = _post_filter_has_conference(
                summaries, filters.has_conference_link if filters else None
            )

            return Page(
                items=tuple(summaries),
                next_cursor=None,
                total=None,
                next_sync_token=next_sync_token,
            )
        except Exception as e:
            log.warning(
                "gcal.reader.full_sync_events_failed",
                extra={
                    "error": str(e),
                    "include_cancelled": include_cancelled,
                    "calendar_id": calendar_id,
                },
            )
            raise
    
    # ---------- events fetching ----------

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_ids: Optional[list[str]] = None,
        include_cancelled: bool = False,
        expand: ExpandMode = 'none',
        filters: Optional[EventFilter] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Page[EventSummary]:
        """Windowed listing across one or more calendars with composite cursor support."""
        try:
            if not calendar_ids:
                # Default to the configured user calendar (usually "primary")
                calendar_ids = [settings.gcal_calendar_id]

            service = self.client.get_service()

            # Decode composite cursor
            c = _Cursor.decode(cursor)
            cal_index = c.cal_index
            page_token = c.page_token

            single_events = expand == "instances"
            order_by = "startTime" if single_events else None
            q = _build_q(filters)

            page_size = min(limit, 100)
            summaries: List[EventSummary] = []
            next_cursor: Optional[str] = None

            while cal_index < len(calendar_ids) and len(summaries) < limit:
                cal_id = calendar_ids[cal_index]

                params: Dict[str, Any] = {
                    "calendarId": cal_id,
                    "singleEvents": single_events,
                    "timeMin": start.astimezone(timezone.utc).isoformat(),
                    "timeMax": end.astimezone(timezone.utc).isoformat(),
                    "showDeleted": include_cancelled,
                    "maxResults": page_size,
                }
                if page_token:
                    params["pageToken"] = page_token
                if q:
                    params["q"] = q
                if order_by:
                    params["orderBy"] = order_by

                req = service.events().list(**params)
                resp = _execute_with_retries(req)

                items = resp.get("items", []) or []

                if not include_cancelled:
                    items = [it for it in items if it.get("status") != "cancelled"]

                for raw in items:
                    try:
                        summary = normalize_event_summary(
                            raw, calendar_id=cal_id, calendar_tz=None
                        )
                        if summary:
                            summaries.append(summary)
                            if len(summaries) >= limit:
                                break
                    except Exception as ne:
                        log.debug(
                            "gcal.reader.normalize_event_summary_skip",
                            extra={
                                "error": str(ne),
                                "event_id": raw.get("id"),
                                "calendar_id": cal_id,
                            },
                        )

                page_token = cast(Optional[str], resp.get("nextPageToken"))

                if len(summaries) >= limit:
                    # Build next_cursor at current calendar + remaining page token
                    if page_token:
                        next_cursor = _Cursor(
                            cal_index=cal_index, page_token=page_token
                        ).encode()
                    elif (cal_index + 1) < len(calendar_ids):
                        next_cursor = _Cursor(
                            cal_index=cal_index + 1, page_token=None
                        ).encode()
                    else:
                        next_cursor = None
                    break

                if page_token:
                    # More pages in the same calendar; continue loop
                    continue

                # Move to next calendar
                cal_index += 1
                page_token = None

            summaries = _post_filter_has_conference(
                summaries, filters.has_conference_link if filters else None
            )

            # If we exhausted the current calendar and moved on, derive the final next_cursor
            if next_cursor is None and cal_index < len(calendar_ids):
                next_cursor = _Cursor(
                    cal_index=cal_index, page_token=page_token
                ).encode()
            elif next_cursor is None and cal_index >= len(calendar_ids):
                next_cursor = None

            return Page(
                items=tuple(summaries),
                next_cursor=next_cursor,
                total=None,
                next_sync_token=None,
            )
        except Exception as e:
            log.warning(
                "gcal.reader.sync_events_failed",
                extra={
                    "error": str(e),
                    "include_cancelled": include_cancelled,
                    "limit": limit,
                    "calendars": calendar_ids,
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
        start: datetime,
        end: datetime,
        *,
        calendar_ids: Optional[list[str]] = None,
        include_cancelled: bool = False,
    ) -> Sequence[Event]:
        """
        Convenience wrapper that returns fully realized Event objects for all concrete
        occurrences overlapping the window. Uses list_events(expand='instances') then
        fetches each item with get_event().
        """
        try:
            cursor: Optional[str] = None
            out: List[Event] = []
            while True:
                page = self.list_events(
                    start=start,
                    end=end,
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
                            extra={
                                "error": str(ge),
                                "event_id": s.id,
                                "calendar_id": s.calendar_id,
                            },
                        )
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
            return tuple(out)
        except Exception as e:
            log.warning(
                "gcal.reader.find_between_failed",
                extra={"error": str(e)},
            )
            raise
