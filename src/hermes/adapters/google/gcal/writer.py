"""Google Calendar write adapter implementation.

This module builds provider request payloads and performs write operations
against the Google Calendar API (create and delete flows).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional, Sequence, TypeVar, cast
import random

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from zoneinfo import ZoneInfo

from hermes.adapters.google.gcal.client import GCalClient
from hermes.adapters.google.gcal.normalizer import build_event_insert_body
from hermes.logging_utils import get_logger
from hermes.ports.calendar import (
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
    Mirrors the Gmail writer flow:
      - _build_new_event() → constructs data transfer object (DTO)
      - create_new_event() → inserts into Google Calendar and returns event id
      - delete_event() → deletes an event by its id
      - delete_all_after() → removes a recurring event and its future instances
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
            if not (start and end):
                raise ValueError("Must provide either start and end datetimes.")
        
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

    # --- Delete ---

    def delete_event(self, calendar_id: str, event_id: str, send_updates: bool = True) -> None:
        """
        Delete an event from Google Calendar.
        If the event is part of a recurring series, deleting the master removes all instances.
        
        For recurring events, it's equivalent to Google Calendar's 'All events' option (for the master) and 'This event' (for any other).
        """
        try:
            service = self.client.get_service()
            req = service.events().delete(
                calendarId=calendar_id,
                eventId=event_id,
                sendUpdates="all" if send_updates else "none",
            )
            _execute_with_retries(req)
            log.info(
                "gcal.writer.delete_event_done",
                extra={"calendar_id": calendar_id, "event_id": event_id},
            )
        except Exception as e:
            log.warning(
                "gcal.writer.delete_event_failed",
                extra={"error": str(e), "calendar_id": calendar_id, "event_id": event_id},
            )
            raise

    def delete_all_after(
        self,
        calendar_id: str,
        master_event_id: str,
        cutoff_start: datetime,
        *,
        send_updates: bool = True,
    ) -> None:
        """
        Truncates a recurring event so that no occurrences exist after and including `cutoff_start`.
        Equivalent to Google Calendar's 'Delete this and following events' option.
        """
        try:
            service = self.client.get_service()

            # --- 1. Fetch the master event to inspect its recurrence ---
            get_req = service.events().get(calendarId=calendar_id, eventId=master_event_id)
            master = _execute_with_retries(get_req)
            rrules = master.get("recurrence", [])
            if not rrules:
                raise ValueError("Event is not recurring; use delete_event() instead.")

            # --- 2. Build new RRULE(s) with an UNTIL boundary before cutoff_start ---
            until_str = cutoff_start.strftime("%Y%m%dT%H%M%SZ")
            new_rrules = []
            for rule in rrules:
                if rule.startswith("RRULE:"):
                    # remove any existing UNTIL or COUNT limits
                    parts = [p for p in rule.split(";") if not (p.startswith("UNTIL=") or p.startswith("COUNT="))]
                    new_rrules.append(";".join(parts + [f"UNTIL={until_str}"]))
                else:
                    new_rrules.append(rule)

            # --- 3. Patch the master event to update its recurrence rule ---
            patch_body = {"recurrence": new_rrules}
            patch_req = service.events().patch(
                calendarId=calendar_id,
                eventId=master_event_id,
                body=patch_body,
                sendUpdates="all" if send_updates else "none",
            )
            _execute_with_retries(patch_req)

            # --- 4. Delete the clicked instance itself ---
            # Instance IDs use pattern: {master_id}_{start_in_UTC}
            instance_suffix = cutoff_start.strftime("%Y%m%dT%H%M%SZ")
            instance_id = f"{master_event_id}_{instance_suffix}"

            del_req = service.events().delete(
                calendarId=calendar_id,
                eventId=instance_id,
                sendUpdates="all" if send_updates else "none",
            )
            _execute_with_retries(del_req)

            log.info(
                "gcal.writer.delete_this_and_following_done",
                extra={
                    "calendar_id": calendar_id,
                    "master_event_id": master_event_id,
                    "cutoff": cutoff_start.isoformat(),
                    "until": until_str,
                },
            )

        except Exception as e:
            log.warning(
                "gcal.writer.delete_this_and_following_failed",
                extra={
                    "error": str(e),
                    "calendar_id": calendar_id,
                    "master_event_id": master_event_id,
                    "cutoff": cutoff_start.isoformat(),
                },
            )
            raise
