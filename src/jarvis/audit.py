"""Append-only action log. Details are redacted before they are stored."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.jarvis.db import session_scope
from src.jarvis.models import AuditEvent
from src.jarvis.redact import redact, redact_text

logger = logging.getLogger("jarvis.audit")


def record(action: str, *, owner: Optional[str] = "", target: str = "", risk: str = "read",
           outcome: str = "ok", actor: str = "jarvis", **detail: Any) -> None:
    """Never raises: a failing audit write must not break the audited action."""
    try:
        with session_scope() as db:
            db.add(AuditEvent(owner=owner or "", action=action, target=redact_text(target), risk=risk,
                              outcome=outcome, actor=actor,
                              detail=json.dumps(redact(detail), ensure_ascii=False, default=str)))
    except Exception as exc:
        logger.warning("audit write failed for %s: %s", action, redact_text(exc))


def recent(owner: Optional[str] = None, limit: int = 100, action_prefix: str = "") -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = db.query(AuditEvent)
        if owner is not None:
            q = q.filter(AuditEvent.owner == owner)
        if action_prefix:
            q = q.filter(AuditEvent.action.like(f"{action_prefix}%"))
        rows = q.order_by(AuditEvent.ts.desc()).limit(max(1, min(limit, 500))).all()
        return [{"id": r.id, "ts": r.ts.isoformat() + "Z", "actor": r.actor, "action": r.action,
                 "target": r.target, "risk": r.risk, "outcome": r.outcome,
                 "detail": json.loads(r.detail or "{}")} for r in rows]
