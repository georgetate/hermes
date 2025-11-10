from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence, Tuple, List
from zoneinfo import ZoneInfo

from agentos.logging_utils import get_logger
from agentos.ports.calendar import (
    CalendarRef,
    EventSummary,
    Event,
    Attendee,
    Reminder,
    Recurrence,
    TimeRange,
    NewEvent,
)

log = get_logger(__name__)


# -------------------- helpers: RFC3339 / ISO parsing --------------------

def _parse_rfc3339(s: Optional[str]) -> Optional[datetime]:
    """
    Parse RFC3339/ISO strings returned by Google (e.g., "2025-10-30T09:00:00-06:00" or "...Z").
    Returns timezone-aware datetimes.
    """
    if not s:
        return None
    try:
        # Handle trailing Z for UTC for Python versions where fromisoformat doesn't accept Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # fromisoformat returns aware if offset present; ensure aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        log.warning("gcal.normalizer.rfc3339_parse_failed", extra={"value": s})
        return None


def _dt_all_day(date_str: str, tz_name: Optional[str]) -> TimeRange:
    """
    Create [start=00:00, end=next-day 00:00) in the provided tz for an all-day event date string "YYYY-MM-DD".
    """
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:
        log.debug("gcal.normalizer.tz_load_failed", extra={"tz": tz_name})
        tz = timezone.utc
    try:
        y, m, d = map(int, date_str.split("-"))
        start = datetime(y, m, d, 0, 0, tzinfo=tz)
        end = start + timedelta(days=1)
        return TimeRange(start=start, end=end)
    except Exception:
        log.warning("gcal.normalizer.allday_parse_failed", extra={"date": date_str, "tz": tz_name})
        # Fallback to "today" in UTC if badly malformed (rare)
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return TimeRange(start=today, end=today + timedelta(days=1))


def _resolve_event_times(
    raw: dict[str, Any],
    *,
    calendar_tz: Optional[str],
) -> Tuple[datetime, datetime, bool, Optional[str]]:
    """
    Convert Google event start/end objects to aware datetimes.
    Returns (start, end, all_day, canonical_tz).
    - canonical_tz is taken as the start tz if present, else end tz, else calendar tz.
    - If start/end use different tz's, they are still represented correctly; canonical_tz chooses start tz.
    """
    start_obj = raw.get("start") or {}
    end_obj = raw.get("end") or {}

    start_tz = start_obj.get("timeZone") or calendar_tz
    end_tz = end_obj.get("timeZone") or calendar_tz

    # Prefer start tz as the event's defining timezone
    canonical_tz = start_tz or end_tz or None

    # Case 1: dateTime (timed event)
    if "dateTime" in start_obj or "dateTime" in end_obj:
        # Google always provides both for timed events, but be defensive.
        s_iso = start_obj.get("dateTime")
        e_iso = end_obj.get("dateTime")

        # Patch missing timezone by appending canonical tz if needed
        s_dt = _parse_rfc3339(s_iso)
        e_dt = _parse_rfc3339(e_iso)

        # If one side is missing tz but a tz name exists, localize
        if s_dt is not None and s_dt.tzinfo is None and start_tz:
            try:
                s_dt = s_dt.replace(tzinfo=ZoneInfo(start_tz))
            except Exception:
                s_dt = s_dt.replace(tzinfo=timezone.utc)
        if e_dt is not None and e_dt.tzinfo is None and end_tz:
            try:
                e_dt = e_dt.replace(tzinfo=ZoneInfo(end_tz))
            except Exception:
                e_dt = e_dt.replace(tzinfo=timezone.utc)

        # If still None due to malformed inputs, fallback log + now
        if s_dt is None or e_dt is None:
            log.warning("gcal.normalizer.datetime_missing", extra={"event_id": raw.get("id")})
            now = datetime.now(timezone.utc)
            s_dt = s_dt or now
            e_dt = e_dt or (s_dt + timedelta(hours=1))

        return (s_dt, e_dt, False, canonical_tz)

    # Case 2: date (all-day)
    if "date" in start_obj and "date" in end_obj:
        all_day_range = _dt_all_day(start_obj["date"], start_tz)
        s, e = all_day_range.start, all_day_range.end
        # Google end.date for all-day is usually the next calendar day; our spec
        # requires exclusive next-day end at 00:00 in event tz already, so we ignore raw end.date.
        return (s, e, True, canonical_tz)

    # Fallback: unknown structure — try best effort
    log.warning("gcal.normalizer.unknown_time_format", extra={"event_id": raw.get("id")})
    now = datetime.now(timezone.utc)
    return (now, now + timedelta(hours=1), False, canonical_tz)


def _has_conference(raw: dict[str, Any]) -> bool:
    return bool(raw.get("hangoutLink") or raw.get("conferenceData"))


def _parse_updated(raw: dict[str, Any]) -> Optional[datetime]:
    return _parse_rfc3339(raw.get("updated"))


# -------------------- helpers: recurrence parsing/building --------------------

def _parse_until(s: str) -> Optional[datetime]:
    """
    Parse common UNTIL encodings:
      - YYYYMMDDTHHMMSSZ   (UTC)
      - YYYYMMDD           (date only; treat as 00:00Z)
    Returns aware datetimes.
    """
    try:
        if s.endswith("Z"):
            # UTC timestamp like 20250101T120000Z
            base = s[:-1]
            if "T" in base:
                dt = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            else:
                dt = datetime.strptime(base, "%Y%m%d").replace(tzinfo=timezone.utc)
            return dt
        # No trailing Z: try full ymdThms with offset is uncommon in RRULE; fallback to date only.
        if "T" in s:
            # Best-effort: assume naive UTC
            try:
                return datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            except Exception:
                pass
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
    except Exception:
        log.warning("gcal.normalizer.until_parse_failed", extra={"until": s})
        return None


def normalize_recurrence(rrule_list: Optional[Sequence[str]]) -> Optional[Recurrence]:
    """
    Google returns recurrence as a list of strings, typically ["RRULE:..."].
    We parse a single RRULE line into our Recurrence DTO.
    Supported keys: FREQ, INTERVAL, BYDAY, BYMONTHDAY, COUNT, UNTIL, TZID
    """
    if not rrule_list:
        return None

    # Find first rule line
    rule_line = None
    for r in rrule_list:
        if r and r.upper().startswith("RRULE:"):
            rule_line = r[6:]  # strip "RRULE:"
            break
    if not rule_line:
        return None

    # Split into parts like ["FREQ=WEEKLY", "INTERVAL=2", ...]
    parts = [p for p in rule_line.split(";") if p]
    kv: dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.upper().strip()] = v.strip()

    freq = kv.get("FREQ")
    if not freq:
        log.warning("gcal.normalizer.rrule_missing_freq", extra={"rrule": rule_line})
        return None

    interval = 1
    try:
        if "INTERVAL" in kv:
            interval = int(kv["INTERVAL"])
    except Exception:
        log.warning("gcal.normalizer.rrule_interval_parse_failed", extra={"interval": kv.get("INTERVAL")})

    byweekday: Optional[List[str]] = None
    if "BYDAY" in kv and kv["BYDAY"]:
        byweekday = [d.strip().upper() for d in kv["BYDAY"].split(",") if d.strip()]

    bymonthday: Optional[List[int]] = None
    if "BYMONTHDAY" in kv and kv["BYMONTHDAY"]:
        vals = []
        for item in kv["BYMONTHDAY"].split(","):
            try:
                vals.append(int(item.strip()))
            except Exception:
                log.debug("gcal.normalizer.bymonthday_bad_value", extra={"value": item})
        bymonthday = vals or None

    count: Optional[int] = None
    if "COUNT" in kv:
        try:
            count = int(kv["COUNT"])
        except Exception:
            log.debug("gcal.normalizer.count_bad_value", extra={"value": kv.get("COUNT")})

    until: Optional[datetime] = None
    if "UNTIL" in kv:
        until = _parse_until(kv["UNTIL"])

    tzid: Optional[str] = kv.get("TZID")

    try:
        return Recurrence(
            freq=freq,  # type: ignore[arg-type]
            interval=interval,
            byweekday=tuple(byweekday) if byweekday else None,
            bymonthday=tuple(bymonthday) if bymonthday else None,
            count=count,
            until=until,
            tzid=tzid,
        )
    except Exception as e:
        log.warning("gcal.normalizer.rrule_construct_failed", extra={"error": str(e), "rrule": rule_line})
        return None


def build_rrule(rec: Recurrence) -> str:
    """
    Build an RFC 5545-style RRULE string from our Recurrence DTO.
    Only includes fields that are set.
    """
    parts: List[str] = [f"FREQ={rec.freq}"]
    if rec.interval and rec.interval != 1:
        parts.append(f"INTERVAL={rec.interval}")
    if rec.byweekday:
        parts.append("BYDAY=" + ",".join(rec.byweekday))
    if rec.bymonthday:
        parts.append("BYMONTHDAY=" + ",".join(str(x) for x in rec.bymonthday))
    if rec.count is not None:
        parts.append(f"COUNT={rec.count}")
    if rec.until is not None:
        # Encode UNTIL in UTC as YYYYMMDDTHHMMSSZ
        u = rec.until.astimezone(timezone.utc)
        parts.append("UNTIL=" + u.strftime("%Y%m%dT%H%M%SZ"))
    if rec.tzid:
        parts.append(f"TZID={rec.tzid}")
    return "RRULE:" + ";".join(parts)


# -------------------- inbound: Google → Port DTOs --------------------

def normalize_calendar_ref(raw: dict[str, Any]) -> Optional[CalendarRef]:
    """
    calendarList.list item → CalendarRef
    """
    try:
        return CalendarRef(
            id=raw.get("id", ""),
            name=raw.get("summary", "") or "",
            timezone=raw.get("timeZone"),
            is_primary=bool(raw.get("primary", False)),
        )
    except Exception as e:
        log.warning("gcal.normalizer.calendar_ref_failed", extra={"error": str(e), "id": raw.get("id")})
        return None


def _normalize_attendees(raw_list: Optional[Sequence[dict[str, Any]]]) -> Tuple[Attendee, ...]:
    out: List[Attendee] = []
    for a in (raw_list or []):
        try:
            out.append(
                Attendee(
                    name=a.get("displayName"),
                    email=a.get("email", ""),
                    optional=bool(a.get("optional", False)),
                    response_status=a.get("responseStatus"),
                )
            )
        except Exception as e:
            log.debug("gcal.normalizer.attendee_skip", extra={"error": str(e), "attendee": a})
    return tuple(out)


def _normalize_reminders(rem: Optional[dict[str, Any]]) -> Tuple[Reminder, ...]:
    """
    Google encodes reminders as:
      {"useDefault": true}  OR
      {"useDefault": false, "overrides":[{"method":"popup","minutes":10}, ...]}
    We convert overrides to our Reminder list; if useDefault=True, return empty tuple and let caller infer defaults.
    """
    if not rem:
        return tuple()
    if rem.get("useDefault", False):
        return tuple()
    overrides = rem.get("overrides") or []
    out: List[Reminder] = []
    for r in overrides:
        try:
            out.append(Reminder(minutes_before_start=int(r.get("minutes", 0)), method=r.get("method")))
        except Exception as e:
            log.debug("gcal.normalizer.reminder_skip", extra={"error": str(e), "reminder": r})
    return tuple(out)


def normalize_event_summary(
    raw: dict[str, Any],
    *,
    calendar_id: str,
    calendar_tz: Optional[str] = None,
) -> Optional[EventSummary]:
    """
    events.list item → EventSummary
    Works for both singleEvents=true (instances) and false (masters).
    """
    try:
        start, end, all_day, canonical_tz = _resolve_event_times(raw, calendar_tz=calendar_tz)
        recurrence = normalize_recurrence(raw.get("recurrence"))
        is_instance = bool(raw.get("recurringEventId"))
        series_id = raw.get("recurringEventId") if is_instance else (raw.get("id") if recurrence else None)

        return EventSummary(
            id=raw.get("id", ""),
            calendar_id=calendar_id,
            title=raw.get("summary", "") or "",
            start=start,
            end=end,
            all_day=all_day,
            last_updated=_parse_updated(raw),
            is_recurring_series=bool(recurrence and not is_instance),
            series_id=series_id,
            recurrence=recurrence if (recurrence and not is_instance) else None,
            has_conference_link=_has_conference(raw),
            status=raw.get("status"),
        )
    except Exception as e:
        log.warning("gcal.normalizer.event_summary_failed", extra={"error": str(e), "event_id": raw.get("id")})
        return None


def normalize_event_full(
    raw: dict[str, Any],
    *,
    calendar_id: str,
    calendar_tz: Optional[str] = None,
) -> Optional[Event]:
    """
    events.get → Event
    """
    try:
        start, end, all_day, canonical_tz = _resolve_event_times(raw, calendar_tz=calendar_tz)

        attendees = _normalize_attendees(raw.get("attendees"))
        reminders = _normalize_reminders(raw.get("reminders"))

        recurrence = normalize_recurrence(raw.get("recurrence"))
        series_id = raw.get("recurringEventId")

        return Event(
            id=raw.get("id", ""),
            calendar_id=calendar_id,
            title=raw.get("summary", "") or "",
            start=start,
            end=end,
            all_day=all_day,
            timezone=canonical_tz,
            location=raw.get("location"),
            description=raw.get("description"),
            attendees=attendees,
            reminders=reminders,
            last_updated=_parse_updated(raw),
            has_conference_link=_has_conference(raw),
            recurrence=recurrence,
            series_id=series_id,
            status=raw.get("status"),
        )
    except Exception as e:
        log.warning("gcal.normalizer.event_full_failed", extra={"error": str(e), "event_id": raw.get("id")})
        return None


# -------------------- outbound: Port DTOs → Google --------------------

def _build_attendees(attendees: Optional[Sequence[Attendee]]) -> Optional[List[dict[str, Any]]]:
    if not attendees:
        return None
    out: List[dict[str, Any]] = []
    for a in attendees:
        try:
            if not a.email:
                continue
            item: dict[str, Any] = {"email": a.email}
            if a.name:
                item["displayName"] = a.name
            if a.optional:
                item["optional"] = True
            if a.response_status:
                item["responseStatus"] = a.response_status
            out.append(item)
        except Exception as e:
            log.debug("gcal.normalizer.build_attendee_skip", extra={"error": str(e), "attendee": repr(a)})
    return out or None


def _build_reminders(reminders: Optional[Sequence[Reminder]]) -> Optional[dict[str, Any]]:
    if reminders is None:
        return None  # omit, use calendar defaults
    if len(reminders) == 0:
        return {"useDefault": True}
    overs: List[dict[str, Any]] = []
    for r in reminders:
        try:
            overs.append({"method": r.method or "popup", "minutes": int(r.minutes_before_start)})
        except Exception as e:
            log.debug("gcal.normalizer.build_reminder_skip", extra={"error": str(e), "reminder": repr(r)})
    return {"useDefault": False, "overrides": overs}


def _build_start_end(
    *,
    start: datetime,
    end: datetime,
    all_day: bool,
    tz_name: Optional[str],
) -> Tuple[dict[str, Any], dict[str, Any]]:
    """
    Build Google start/end dicts.
    - For all_day=True, use {"date": "YYYY-MM-DD"}; Google interprets end.date as exclusive next day.
    - For timed, use {"dateTime": "...", "timeZone": "..."} preserving tz.
    """
    if all_day:
        s_date = start.astimezone(ZoneInfo(tz_name)) if tz_name else start
        e_date = end.astimezone(ZoneInfo(tz_name)) if tz_name else end
        return (
            {"date": s_date.date().isoformat()},
            {"date": e_date.date().isoformat()},
        )
    else:
        def _fmt(dt: datetime) -> str:
            # Ensure RFC3339 with offset; isoformat() includes offset for aware datetimes
            # Replace +00:00 with Z for compactness (optional)
            s = dt.isoformat()
            return "Z".join(s.rsplit("+00:00", 1)) if s.endswith("+00:00") else s

        return (
            {"dateTime": _fmt(start), **({"timeZone": tz_name} if tz_name else {})},
            {"dateTime": _fmt(end), **({"timeZone": tz_name} if tz_name else {})},
        )


def build_event_insert_body(event: NewEvent) -> dict[str, Any]:
    """
    Convert NewEvent DTO → Google events.insert body.
    Only fields present on NewEvent are populated.
    """
    try:
        start_dict, end_dict = _build_start_end(
            start=event.start,
            end=event.end,
            all_day=bool(event.all_day),
            tz_name=event.timezone,
        )

        body: dict[str, Any] = {
            "summary": event.title,
            "start": start_dict,
            "end": end_dict,
        }

        if event.location:
            body["location"] = event.location
        if event.description:
            body["description"] = event.description
        if event.attendees:
            atts = _build_attendees(event.attendees)
            if atts:
                body["attendees"] = atts
        if event.reminders is not None:
            rem = _build_reminders(event.reminders)
            if rem is not None:
                body["reminders"] = rem
        if event.has_conference_link:
            # Minimal signal; real conferenceData creation requires create/update with conferenceData + requestId
            body["conferenceData"] = {"createRequest": {"requestId": "agentos-autogen"}}
        if event.recurrence:
            body["recurrence"] = [build_rrule(event.recurrence)]

        return body
    except Exception as e:
        log.warning("gcal.normalizer.build_event_failed", extra={"error": str(e), "title": getattr(event, 'title', None)})
        # Provide a minimal body so the writer can decide how to proceed
        return {
            "summary": getattr(event, "title", "") or "",
            "start": {"dateTime": (event.start or datetime.now(timezone.utc)).isoformat()},
            "end": {"dateTime": (event.end or (datetime.now(timezone.utc) + timedelta(hours=1))).isoformat()},
        }
