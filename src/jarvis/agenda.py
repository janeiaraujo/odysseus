"""Calendar queries answered with code. No LLM, no tokens.

Reads Odysseus's calendar tables, including recurring-series expansion, so
everything synced there (CalDAV, Google) is covered from one place.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.jarvis import config


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.get("timezone", "America/Sao_Paulo"))
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(local_tz())


def _parse(value: str) -> Tuple[datetime, bool]:
    """Parse an API datetime; naive values are legacy local time."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=local_tz()), True
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=local_tz())).astimezone(local_tz()), False


def normalize_events(raw: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for ev in raw:
        start, all_day = _parse(ev["dtstart"])
        end, _ = _parse(ev.get("dtend") or ev["dtstart"])
        if end < start:
            end = start
        out.append({"uid": ev.get("uid", ""), "title": ev.get("summary") or ev.get("title") or "(sem título)",
                    "location": ev.get("location") or "", "start": start, "end": end, "all_day": all_day})
    out.sort(key=lambda e: e["start"])
    return out


def events_between(owner: Optional[str], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    from sqlalchemy import and_, or_

    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _expand_rrule

    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal).filter(CalendarEvent.status != "cancelled", or_(
            and_(or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)), CalendarEvent.dtstart < end_utc, CalendarEvent.dtend > start_utc),
            and_(CalendarEvent.rrule.isnot(None), CalendarEvent.rrule != "", CalendarEvent.dtstart < end_utc)))
        if owner:
            q = q.filter(CalendarCal.owner == owner)
        expanded: List[Dict[str, Any]] = []
        for event in q.all():
            expanded.extend(_expand_rrule(event, start_utc, end_utc))
    finally:
        db.close()
    return [e for e in normalize_events(expanded) if e["end"] > start and e["start"] < end]


def find_conflicts(events: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Overlapping timed events. All-day events never block a time slot."""
    timed = sorted((e for e in events if not e["all_day"]), key=lambda e: e["start"])
    conflicts = []
    for i, first in enumerate(timed):
        for second in timed[i + 1:]:
            if second["start"] >= first["end"]:
                break
            conflicts.append((first, second))
    return conflicts


def free_slots(events: List[Dict[str, Any]], start: datetime, end: datetime, minutes: int,
               work_start: int = 8, work_end: int = 18, include_weekends: bool = False, limit: int = 5) -> List[Tuple[datetime, datetime]]:
    """Free windows of at least `minutes` inside working hours."""
    need, slots = timedelta(minutes=minutes), []
    busy = sorted(((e["start"], e["end"]) for e in events if not e["all_day"]), key=lambda b: b[0])
    day = start.date()
    while day <= end.date() and len(slots) < limit:
        if include_weekends or day.weekday() < 5:
            cursor = max(start, datetime.combine(day, time(work_start), tzinfo=start.tzinfo))
            stop = min(end, datetime.combine(day, time(work_end), tzinfo=start.tzinfo))
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= stop:
                    continue
                if busy_start - cursor >= need:
                    slots.append((cursor, busy_start))
                cursor = max(cursor, busy_end)
            if stop - cursor >= need:
                slots.append((cursor, stop))
        day += timedelta(days=1)
    return slots[:limit]


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def format_events(events: List[Dict[str, Any]], *, with_day: bool = False) -> str:
    lines = []
    for ev in events:
        when = "dia inteiro" if ev["all_day"] else f"{_hhmm(ev['start'])}-{_hhmm(ev['end'])}"
        if with_day:
            when = f"{ev['start'].strftime('%d/%m')} {when}"
        where = f" ({ev['location']})" if ev["location"] else ""
        lines.append(f"- {when}: {ev['title']}{where}")
    return "\n".join(lines)
