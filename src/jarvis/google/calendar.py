"""Google Calendar: two-way integration.

Synced events land in Odysseus's own calendar tables with origin="google", so the
existing UI, agent tools and Jarvis agenda all see them. Odysseus's CalDAV pruning
only touches origin="caldav", so these rows are safe from it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from src.jarvis import audit, policy
from src.jarvis.db import session_scope, utcnow
from src.jarvis.google.oauth import default_account, google_request
from src.jarvis.models import SyncState

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/calendar/v3"
INITIAL_WINDOW = timedelta(days=30)


class GoogleApiError(RuntimeError):
    pass


def _check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Calendar respondeu {resp.status_code} em {what}")
    return resp.json() if resp.content else {}


async def list_calendars(owner: str, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    account_id = account_id or default_account(owner, "calendar")
    body = _check(await google_request("GET", f"{API}/users/me/calendarList", account_id=account_id, owner=owner), "listar calendarios")
    return [{"id": c["id"], "name": c.get("summary", ""), "primary": bool(c.get("primary")), "access": c.get("accessRole", "")} for c in body.get("items", [])]


async def list_events(owner: str, *, start: datetime, end: datetime, query: str = "", calendar_id: str = "primary", account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    account_id = account_id or default_account(owner, "calendar")
    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
    if query:
        params["q"] = query
    body = _check(await google_request("GET", f"{API}/calendars/{quote(calendar_id, safe='')}/events", account_id=account_id, owner=owner, params=params), "listar eventos")
    return [_event_view(e) for e in body.get("items", [])]


def _event_view(event: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": event.get("id", ""), "title": event.get("summary", "(sem titulo)"), "start": event.get("start", {}), "end": event.get("end", {}),
            "location": event.get("location", ""), "recurring": bool(event.get("recurringEventId") or event.get("recurrence")),
            "attendees": [a.get("email", "") for a in event.get("attendees", [])], "status": event.get("status", ""), "reminders": event.get("reminders", {})}


def _risk(attendees: Any) -> str:
    """Anything that notifies other people is external; a private change is reversible."""
    return policy.EXTERNAL if attendees else policy.REVERSIBLE


async def _do_create(owner: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    account_id = payload.get("account_id") or default_account(owner, "calendar")
    send = "all" if payload["event"].get("attendees") else "none"
    resp = await google_request("POST", f"{API}/calendars/{quote(payload.get('calendar_id', 'primary'), safe='')}/events", account_id=account_id, owner=owner,
                                params={"sendUpdates": send}, json=payload["event"])
    return _event_view(_check(resp, "criar evento"))


async def _do_update(owner: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    account_id = payload.get("account_id") or default_account(owner, "calendar")
    send = "all" if payload.get("affects_others") else "none"
    resp = await google_request("PATCH", f"{API}/calendars/{quote(payload.get('calendar_id', 'primary'), safe='')}/events/{quote(payload['event_id'], safe='')}",
                                account_id=account_id, owner=owner, params={"sendUpdates": send}, json=payload["changes"])
    return _event_view(_check(resp, "alterar evento"))


async def _do_delete(owner: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    account_id = payload.get("account_id") or default_account(owner, "calendar")
    send = "all" if payload.get("affects_others") else "none"
    resp = await google_request("DELETE", f"{API}/calendars/{quote(payload.get('calendar_id', 'primary'), safe='')}/events/{quote(payload['event_id'], safe='')}",
                                account_id=account_id, owner=owner, params={"sendUpdates": send})
    if resp.status_code not in (200, 204, 404, 410):
        raise GoogleApiError(f"Google Calendar respondeu {resp.status_code} ao cancelar evento")
    return {"deleted": payload["event_id"]}


policy.register_executor("calendar.create", _do_create)
policy.register_executor("calendar.update", _do_update)
policy.register_executor("calendar.delete", _do_delete)


async def create_event(owner: str, *, title: str, start: str, end: str, tz: str = "America/Sao_Paulo", attendees: Optional[List[str]] = None, location: str = "",
                       description: str = "", reminder_minutes: Optional[int] = None, recurrence: Optional[List[str]] = None, calendar_id: str = "primary", account_id: Optional[str] = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {"summary": title, "start": {"dateTime": start, "timeZone": tz}, "end": {"dateTime": end, "timeZone": tz}}
    if location:
        event["location"] = location
    if description:
        event["description"] = description
    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]
    if recurrence:
        event["recurrence"] = recurrence
    if reminder_minutes is not None:
        event["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(reminder_minutes)}]}
    payload = {"event": event, "calendar_id": calendar_id, "account_id": account_id}
    return await policy.guarded("calendar.create", _risk(attendees), owner, f"Criar evento '{title}' em {start}", payload, lambda: _do_create(owner, payload))


async def _fetch_attendees(owner: str, event_id: str, calendar_id: str, account_id: Optional[str]) -> List[Dict[str, Any]]:
    account = account_id or default_account(owner, "calendar")
    return _check(await google_request("GET", f"{API}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}", account_id=account, owner=owner), "ler evento").get("attendees", [])


async def update_event(owner: str, event_id: str, changes: Dict[str, Any], *, calendar_id: str = "primary", account_id: Optional[str] = None) -> Dict[str, Any]:
    """Attendees are read from Google, not trusted from the caller, to classify the risk."""
    attendees = await _fetch_attendees(owner, event_id, calendar_id, account_id)
    payload = {"event_id": event_id, "changes": changes, "calendar_id": calendar_id, "account_id": account_id, "affects_others": bool(attendees)}
    return await policy.guarded("calendar.update", _risk(attendees), owner, f"Alterar evento {event_id}", payload, lambda: _do_update(owner, payload))


async def delete_event(owner: str, event_id: str, *, calendar_id: str = "primary", account_id: Optional[str] = None) -> Dict[str, Any]:
    attendees = await _fetch_attendees(owner, event_id, calendar_id, account_id)
    payload = {"event_id": event_id, "calendar_id": calendar_id, "account_id": account_id, "affects_others": bool(attendees)}
    return await policy.guarded("calendar.delete", _risk(attendees), owner, f"Cancelar evento {event_id}", payload, lambda: _do_delete(owner, payload))


# --- incremental sync into Odysseus tables ---------------------------------------

def _parse_when(when: Dict[str, Any]) -> tuple:
    if when.get("date"):
        return datetime.fromisoformat(when["date"]), True
    parsed = datetime.fromisoformat(when["dateTime"].replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None), False


def _apply_local(owner: str, account_id: str, calendar_name: str, events: List[Dict[str, Any]]) -> Dict[str, int]:
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    created = updated = removed = 0
    cal_id = f"gcal:{account_id}:primary"
    db = SessionLocal()
    try:
        calendar = db.get(CalendarCal, cal_id)
        if calendar is None:
            db.add(CalendarCal(id=cal_id, owner=owner or None, name=calendar_name, source="google", account_id=account_id))
            db.flush()
        for event in events:
            uid = f"gcal:{account_id}:{event['id']}"
            row = db.get(CalendarEvent, uid)
            if event.get("status") == "cancelled":
                if row is not None:
                    db.delete(row)
                    removed += 1
                continue
            if "start" not in event:
                continue
            start, all_day = _parse_when(event["start"])
            end, _ = _parse_when(event.get("end") or event["start"])
            if row is None:
                row = CalendarEvent(uid=uid, calendar_id=cal_id)
                db.add(row)
                created += 1
            else:
                updated += 1
            row.summary, row.description, row.location = event.get("summary", "(sem titulo)"), event.get("description", ""), event.get("location", "")
            row.dtstart, row.dtend, row.all_day, row.is_utc, row.status, row.origin = start, end, all_day, not all_day, "confirmed", "google"
        db.commit()
    finally:
        db.close()
    return {"created": created, "updated": updated, "removed": removed}


def _cursor(owner: str, resource: str) -> str:
    with session_scope() as db:
        row = db.query(SyncState).filter(SyncState.owner == owner, SyncState.provider == "google", SyncState.resource == resource).first()
        return row.cursor if row else ""


def _save_cursor(owner: str, resource: str, cursor: str, error: str = "") -> None:
    with session_scope() as db:
        row = db.query(SyncState).filter(SyncState.owner == owner, SyncState.provider == "google", SyncState.resource == resource).first()
        if row is None:
            row = SyncState(owner=owner, provider="google", resource=resource)
            db.add(row)
        if cursor:
            row.cursor = cursor
        row.last_error, row.updated_at = error[:200], utcnow()
        if not error:
            row.last_ok_at = utcnow()


async def sync(owner: str, account_id: Optional[str] = None) -> Dict[str, int]:
    """Incremental via syncToken. A 410 from Google means start over, as documented."""
    account_id = account_id or default_account(owner, "calendar")
    resource = f"calendar:{account_id}"
    token = _cursor(owner, resource)
    totals = {"created": 0, "updated": 0, "removed": 0}
    page, new_token = "", ""
    while True:
        params: Dict[str, Any] = {"singleEvents": "true", "maxResults": 250}
        if token:
            params["syncToken"] = token
        else:
            params["timeMin"] = (datetime.now(timezone.utc) - INITIAL_WINDOW).isoformat()
        if page:
            params["pageToken"] = page
        resp = await google_request("GET", f"{API}/calendars/primary/events", account_id=account_id, owner=owner, params=params)
        if resp.status_code == 410 and token:
            with session_scope() as db:
                db.query(SyncState).filter(SyncState.owner == owner, SyncState.provider == "google", SyncState.resource == resource).update({"cursor": ""})
            return await sync(owner, account_id)
        body = _check(resp, "sincronizar calendario")
        for key, value in _apply_local(owner, account_id, "Google Calendar", body.get("items", [])).items():
            totals[key] += value
        page = body.get("nextPageToken", "")
        new_token = body.get("nextSyncToken", new_token)
        if not page:
            break
    _save_cursor(owner, resource, new_token)
    audit.record("calendar.sync", owner=owner, **totals)
    return totals
