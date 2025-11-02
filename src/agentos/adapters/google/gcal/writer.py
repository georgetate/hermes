from __future__ import annotations

from typing import Optional

from agentos.adapters.google.gcal.client import GCalClient
from agentos.adapters.google.gcal.normalizer import build_event_insert_body
from agentos.logging_utils import get_logger
from agentos.ports.calendar import NewEvent

log = get_logger(__name__)


class GCalWriter:
    """
    Google Calendar write adapter.
    - Currently supports create_event(); future: update/delete, add/remove attendees, etc.
    """

    def __init__(self, client: Optional[GCalClient] = None) -> None:
        self.client = client or GCalClient.from_settings()

    def create_event(self, calendar_id: str, event: NewEvent) -> str:
        """
        Create a one-off or recurring event.
        Returns the created event id (series master id if recurring).
        """
        try:
            svc = self.client.get_service()
            body = build_event_insert_body(event)

            # If conferenceData present, must set conferenceDataVersion=1
            has_conf = "conferenceData" in body
            req = svc.events().insert(
                calendarId=calendar_id,
                body=body,
                conferenceDataVersion=1 if has_conf else 0,
                sendUpdates="none",  # caller/service can choose to expose this later
            )
            resp = req.execute()
            event_id = resp.get("id")
            if not event_id:
                raise RuntimeError("events.insert returned no id")
            return event_id
        except Exception as e:
            log.warning(
                "gcal.writer.create_event_failed",
                extra={"error": str(e), "calendar_id": calendar_id, "title": getattr(event, "title", None)},
            )
            raise
