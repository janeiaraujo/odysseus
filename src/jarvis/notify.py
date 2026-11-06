"""Notify the user through every configured channel. Always stored in-app first."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from src.jarvis import config
from src.jarvis.db import session_scope
from src.jarvis.models import Notification
from src.jarvis.redact import redact_text

logger = logging.getLogger(__name__)


async def notify(owner: Optional[str], title: str, body: str = "", *, transport: Any = None) -> Dict[str, bool]:
    channels = {"inapp": False, "app_toast": False, "ntfy": False}
    with session_scope() as db:
        db.add(Notification(owner=owner or "", title=title[:200], body=body[:4000]))
    channels["inapp"] = True
    try:
        from src.event_bus import get_task_scheduler
        scheduler = get_task_scheduler()
        if scheduler:
            scheduler.add_notification(title[:200], "success", owner=owner or None, body=body)
            channels["app_toast"] = True
    except Exception as exc:
        logger.debug("toast unavailable: %s", exc)
    try:
        from src.integrations import load_integrations
        from src.service_health import _ntfy_integration
        integration = _ntfy_integration(load_integrations())
        if integration and integration.get("base_url"):
            topic = config.get("notifications.ntfy_topic", "jarvis")
            async with httpx.AsyncClient(timeout=10, transport=transport) as client:
                resp = await client.post(f"{integration['base_url'].rstrip('/')}/{topic}", content=f"{title}\n{body}"[:4000].encode("utf-8"))
            channels["ntfy"] = resp.status_code < 400
    except Exception as exc:
        logger.info("ntfy delivery failed: %s", redact_text(exc))
    return channels


def list_notifications(owner: Optional[str], *, unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = db.query(Notification).filter(Notification.owner == (owner or ""))
        if unread_only:
            q = q.filter(Notification.read == False)  # noqa: E712
        return [{"id": n.id, "title": n.title, "body": n.body, "read": n.read, "created_at": n.created_at.isoformat() + "Z"} for n in q.order_by(Notification.created_at.desc()).limit(limit).all()]


def mark_read(owner: Optional[str]) -> int:
    with session_scope() as db:
        return db.query(Notification).filter(Notification.owner == (owner or ""), Notification.read == False).update({"read": True})  # noqa: E712
