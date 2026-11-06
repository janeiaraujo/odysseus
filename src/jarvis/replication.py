"""Optional Firestore replica through a persistent outbox.

SQLite remains the source of truth: a Firebase outage never affects Jarvis, and
writes are retried. Memory text is encrypted before leaving by default, since a
replica holds the same personal data under a different provider's policy.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from src.jarvis import config
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import ReplicationOutbox
from src.jarvis.redact import redact_text
from src.secret_storage import encrypt

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def _enabled(collection: str) -> bool:
    fb = config.get("firebase", {}) or {}
    return bool(fb.get("enabled")) and collection in (fb.get("collections") or [])


def owner_key(owner: Optional[str]) -> str:
    """Firestore paths never contain the username."""
    return hashlib.sha256((owner or "default").encode()).hexdigest()[:24]


def _enqueue(path: str, op: str, payload: Dict[str, Any]) -> None:
    with session_scope() as db:
        db.add(ReplicationOutbox(doc_path=path, op=op, payload=json.dumps(payload, ensure_ascii=False, default=str)))


def enqueue_memory(item: Dict[str, Any], owner: Optional[str], deleted: bool = False) -> None:
    if not _enabled("memory"):
        return
    path = f"users/{owner_key(owner)}/memory/{item['id']}"
    if deleted:
        _enqueue(path, "delete", {})
        return
    doc = {k: item.get(k) for k in ("kind", "category", "status", "version", "confidence", "updated_at")}
    if config.get("firebase.encrypt_payloads", True):
        doc["text_enc"] = encrypt(item["text"])
    else:
        doc["text"] = item["text"]
    _enqueue(path, "upsert", doc)


def enqueue_device(device: Dict[str, Any], owner: Optional[str]) -> None:
    if _enabled("devices"):
        _enqueue(f"users/{owner_key(owner)}/devices/{device['id']}", "upsert",
                 {k: device.get(k) for k in ("name", "domain", "area", "enabled", "critical")})


async def flush(client: Any = None, limit: int = 100) -> Dict[str, int]:
    """Send queued writes, oldest first. A failed document is retried with backoff."""
    pending = {"sent": 0, "failed": 0}
    now = utcnow()
    with session_scope() as db:
        rows = db.query(ReplicationOutbox).filter(ReplicationOutbox.done_at.is_(None), ReplicationOutbox.attempts < MAX_ATTEMPTS).order_by(ReplicationOutbox.created_at).limit(limit).all()
        work = [(r.id, r.doc_path, r.op, r.payload, r.attempts, r.created_at) for r in rows]
    if not work:
        return pending
    if client is None:
        from src.jarvis.firebase import FirestoreClient
        client = FirestoreClient.from_config()
    for row_id, path, op, payload, attempts, created in work:
        if attempts and now < created + timedelta(seconds=30 * 2 ** attempts):
            continue
        try:
            if op == "delete":
                await client.delete(path)
            else:
                await client.upsert(path, json.loads(payload))
        except Exception as exc:
            pending["failed"] += 1
            with session_scope() as db:
                row = db.get(ReplicationOutbox, row_id)
                row.attempts, row.last_error = row.attempts + 1, redact_text(exc)[:300]
            continue
        pending["sent"] += 1
        with session_scope() as db:
            db.get(ReplicationOutbox, row_id).done_at = utcnow()
    return pending


def status() -> Dict[str, Any]:
    with session_scope() as db:
        q = db.query(ReplicationOutbox).filter(ReplicationOutbox.done_at.is_(None))
        return {"enabled": bool(config.get("firebase.enabled", False)), "pending": q.count(),
                "stuck": q.filter(ReplicationOutbox.attempts >= MAX_ATTEMPTS).count()}
