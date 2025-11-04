from __future__ import annotations

import time
from datetime import datetime
from typing import Optional, Sequence, TypeVar, cast
import random

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from zoneinfo import ZoneInfo

from agentos.adapters.google.gcal.client import GCalClient
from agentos.adapters.google.gcal.normalizer import build_event_insert_body
from agentos.logging_utils import get_logger
from agentos.ports.calendar import (
    Attendee,
    Reminder,
    Recurrence,
    NewEvent,
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


# ----------------------------- Writer -----------------------------

class GCalWriter:
    """
    Google Calendar write adapter.
    Mirrors the GmailWriter pattern:
      - _build_new_event() → constructs DTO
      - create_new_event() → inserts into Google Calendar
      - send_event() → retries + returns event id
    """

    def __init__(self, client: Optional[GCalClient] = None) -> None:
        self.client = client or GCalClient.from_settings()

    # --- Builders ---

    @classmethod
    def _build_new_event(
        cls,
        *,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool = False,
        timezone: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        attendees: Optional[Sequence[Attendee]] = None,
        reminders: Optional[Sequence[Reminder]] = None,
        has_conference_link: Optional[bool] = None,
        recurrence: Optional[Recurrence] = None,
    ) -> NewEvent:
        """
        Factory for constructing a NewEvent DTO.
        Normalizes tz awareness and populates optional fields safely.
        """
        try:
            if not start.tzinfo:
                start = start.replace(tzinfo=ZoneInfo(timezone) if timezone else ZoneInfo("UTC"))
            if not end.tzinfo:
                end = end.replace(tzinfo=ZoneInfo(timezone) if timezone else ZoneInfo("UTC"))

            event = NewEvent(
                title=title,
                start=start,
                end=end,
                all_day=all_day,
                timezone=timezone,
                location=location,
                description=description,
                attendees=attendees,
                reminders=reminders,
                has_conference_link=has_conference_link,
                recurrence=recurrence,
            )
            return event
        except Exception as e:
            log.warning("gcal.writer.build_new_event_failed", extra={"error": str(e)})
            raise

    # --- Create ---

    def create_new_event(self, calendar_id: str, event: NewEvent, send_updates: bool = True) -> str:
        """
        Upload a new event to Google Calendar.
        Returns the provisional event id (master id if recurring).
        """
        try:
            service = self.client.get_service()
            body = build_event_insert_body(event)
            has_conf = "conferenceData" in body

            req = service.events().insert(
                calendarId=calendar_id,
                body=body,
                conferenceDataVersion=1 if has_conf else 0,
                sendUpdates="all" if send_updates else "none",
            )
            resp = _execute_with_retries(req)
            event_id = resp.get("id")
            log.info("gcal.writer.create_new_event_done", extra={"calendar_id": calendar_id, "event_id": event_id})
            if not event_id:
                raise RuntimeError("events.insert returned no id")
            return event_id
        except Exception as e:
            log.warning(
                "gcal.writer.create_new_event_failed",
                extra={"error": str(e), "calendar_id": calendar_id, "title": getattr(event, "title", None)},
            )
            raise
