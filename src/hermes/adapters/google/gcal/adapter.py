"""Composed Google Calendar adapter that satisfies the full CalendarPort."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from hermes.adapters.google.gcal.client import GCalClient
from hermes.adapters.google.gcal.reader import GCalReader
from hermes.adapters.google.gcal.writer import GCalWriter
from hermes.ports.calendar import (
    Attendee,
    CalendarPort,
    CalendarRef,
    Event,
    EventFilter,
    EventSummary,
    ExpandMode,
    NewEvent,
    Page,
    Recurrence,
    Reminder,
)


@dataclass
class GCalAdapter(CalendarPort):
    """Full Google Calendar adapter composed from read and write helpers."""

    reader: GCalReader
    writer: GCalWriter

    def __init__(
        self,
        *,
        client: Optional[GCalClient] = None,
        reader: Optional[GCalReader] = None,
        writer: Optional[GCalWriter] = None,
    ) -> None:
        shared_client = client or GCalClient.from_settings()
        self.reader = reader or GCalReader(client=shared_client)
        self.writer = writer or GCalWriter(client=shared_client)

    def list_calendars(self) -> Sequence[CalendarRef]:
        return self.reader.list_calendars()

    def sync_events(
        self,
        *,
        calendar_id: str,
        sync_token: str,
        include_cancelled: bool,
        filters: EventFilter | None,
    ) -> Page[EventSummary]:
        return self.reader.sync_events(
            calendar_id=calendar_id,
            sync_token=sync_token,
            include_cancelled=include_cancelled,
            filters=filters,
        )

    def full_sync_events(
        self,
        *,
        calendar_id: str,
        include_cancelled: bool,
        expand: ExpandMode = "none",
        filters: EventFilter | None = None,
    ) -> Page[EventSummary]:
        return self.reader.full_sync_events(
            calendar_id=calendar_id,
            include_cancelled=include_cancelled,
            expand=expand,
            filters=filters,
        )

    def list_events(
        self,
        start: datetime,
        end: datetime,
        *,
        calendar_ids: Sequence[str] | None = None,
        include_cancelled: bool = False,
        expand: ExpandMode = "none",
        filters: EventFilter | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[EventSummary]:
        return self.reader.list_events(
            start=start,
            end=end,
            calendar_ids=list(calendar_ids) if calendar_ids else None,
            include_cancelled=include_cancelled,
            expand=expand,
            filters=filters,
            limit=limit,
            cursor=cursor,
        )

    def get_event(self, event_id: str, calendar_id: str) -> Event:
        return self.reader.get_event(event_id, calendar_id)

    def find_between(
        self,
        start: datetime,
        end: datetime,
        *,
        calendar_ids: Sequence[str] | None = None,
        include_cancelled: bool = False,
    ) -> Sequence[Event]:
        return self.reader.find_between(
            start=start,
            end=end,
            calendar_ids=list(calendar_ids) if calendar_ids else None,
            include_cancelled=include_cancelled,
        )

    def _build_new_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool = False,
        timezone: str | None = None,
        location: str | None = None,
        description: str | None = None,
        attendees: Sequence[Attendee] | None = None,
        reminders: Sequence[Reminder] | None = None,
        has_conference_link: bool | None = None,
        recurrence: Recurrence | None = None,
    ) -> NewEvent:
        return self.writer._build_new_event(
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

    def create_event(self, calendar_id: str, event: NewEvent) -> str:
        return self.writer.create_new_event(calendar_id, event)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.writer.delete_event(calendar_id, event_id)

    def delete_all_after(
        self,
        calendar_id: str,
        master_event_id: str,
        cutoff_start: datetime,
        *,
        send_updates: bool = True,
    ) -> None:
        self.writer.delete_all_after(
            calendar_id,
            master_event_id,
            cutoff_start,
            send_updates=send_updates,
        )
