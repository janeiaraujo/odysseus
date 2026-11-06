"""Persistent outgoing mail queue that cannot send twice.

The hard case is a crash or timeout after Gmail accepted the message but before we
recorded it. So a row is never resent blindly: it carries a deterministic
Message-ID, and an interrupted send becomes `uncertain`. Reconciliation then asks
Gmail whether that Message-ID exists, and only a confirmed absence may requeue it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from src.jarvis import audit, policy
from src.jarvis.db import session_scope, utcnow
from src.jarvis.google.gmail import API, build_mime
from src.jarvis.google.oauth import MissingScope, ReconnectRequired, default_account, google_request
from src.jarvis.models import OutboundEmail
from src.jarvis.redact import redact_text
from src.secret_storage import decrypt, encrypt

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 5
_ADDRESS = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+$")


def _message_id(row_id: str) -> str:
    return f"<jarvis-{row_id}@jarvis.local>"


def _view(row: OutboundEmail) -> Dict[str, Any]:
    payload = json.loads(decrypt(row.payload_enc))
    return {"id": row.id, "status": row.status, "to": payload["to"], "subject": payload["subject"], "send_at": row.send_at.isoformat() + "Z", "timezone": row.timezone,
            "attempts": row.attempts, "error": row.error, "sent_at": row.sent_at.isoformat() + "Z" if row.sent_at else None, "approval_id": row.approval_id}


def schedule(owner: str, *, to: List[str], subject: str, body: str, send_at: Optional[datetime] = None, tz: str = "America/Sao_Paulo",
             account_id: str = "", idempotency_key: str = "", cc: Optional[List[str]] = None) -> Dict[str, Any]:
    """Queue a message. The same idempotency key returns the existing row."""
    bad = [a for a in (to or []) + (cc or []) if not _ADDRESS.match(a)]
    if not to or bad:
        raise ValueError("destinatario invalido")
    zone = ZoneInfo(tz)  # raises on an unknown zone
    moment = send_at or datetime.now(timezone.utc)
    moment = (moment if moment.tzinfo else moment.replace(tzinfo=zone)).astimezone(timezone.utc).replace(tzinfo=None)
    key = idempotency_key or hashlib.sha256(json.dumps([owner, to, cc, subject, body, moment.isoformat()], default=str).encode()).hexdigest()
    payload = {"to": to, "cc": cc or [], "subject": subject, "body": body}
    decision = policy.evaluate("gmail.send", policy.EXTERNAL, owner)
    if decision.mode == policy.DENY:
        raise policy.ActionDenied(decision.reason)
    with session_scope() as db:
        existing = db.query(OutboundEmail).filter(OutboundEmail.idempotency_key == key, OutboundEmail.owner == owner).first()
        if existing:
            return _view(existing)
        row = OutboundEmail(owner=owner, account_id=account_id, idempotency_key=key, payload_enc=encrypt(json.dumps(payload, ensure_ascii=False)), send_at=moment, timezone=tz,
                            status="awaiting_approval" if decision.mode == policy.CONFIRM else "queued")
        db.add(row)
        db.flush()
        row_id = row.id
    if decision.mode == policy.CONFIRM:
        approval = policy.request_approval("gmail.send", policy.EXTERNAL, owner, f"Enviar e-mail '{subject}' para {len(to)} destinatario(s)", {"outbound_id": row_id})
        with session_scope() as db:
            db.get(OutboundEmail, row_id).approval_id = approval
    audit.record("gmail.schedule", owner=owner, risk="external", target=row_id, status=decision.mode)
    return get(owner, row_id)


def get(owner: str, row_id: str) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.query(OutboundEmail).filter(OutboundEmail.id == row_id, OutboundEmail.owner == owner).first()
        if row is None:
            raise LookupError("e-mail nao encontrado")
        return _view(row)


def list_queue(owner: str, limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as db:
        return [_view(r) for r in db.query(OutboundEmail).filter(OutboundEmail.owner == owner).order_by(OutboundEmail.send_at.desc()).limit(limit).all()]


def _approved(owner: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with session_scope() as db:
        updated = db.query(OutboundEmail).filter(OutboundEmail.id == payload.get("outbound_id"), OutboundEmail.owner == owner, OutboundEmail.status == "awaiting_approval").update({"status": "queued"})
    if not updated:
        raise LookupError("e-mail nao esta aguardando aprovacao")
    return {"queued": payload["outbound_id"]}


policy.register_executor("gmail.send", _approved)


def cancel(owner: str, row_id: str) -> bool:
    with session_scope() as db:
        updated = db.query(OutboundEmail).filter(OutboundEmail.id == row_id, OutboundEmail.owner == owner, OutboundEmail.status.in_(("queued", "awaiting_approval"))).update({"status": "cancelled"}, synchronize_session=False)
    if updated:
        audit.record("gmail.cancel", owner=owner, risk="reversible", target=row_id)
    return bool(updated)


def _claim_due(limit: int) -> List[str]:
    """Atomic queued -> sending: two workers can never claim the same row."""
    now, claimed = utcnow(), []
    with session_scope() as db:
        ids = [r.id for r in db.query(OutboundEmail.id).filter(OutboundEmail.status == "queued", OutboundEmail.send_at <= now).order_by(OutboundEmail.send_at).limit(limit).all()]
        for row_id in ids:
            if db.query(OutboundEmail).filter(OutboundEmail.id == row_id, OutboundEmail.status == "queued").update(
                    {"status": "sending", "lease_until": now + LEASE, "attempts": OutboundEmail.attempts + 1}, synchronize_session=False):
                claimed.append(row_id)
    return claimed


async def _send_gmail(row: Dict[str, Any]) -> str:
    account = row["account_id"] or default_account(row["owner"], "gmail")
    raw = build_mime(row["to"], row["subject"], row["body"], cc=row["cc"], message_id=_message_id(row["id"]))
    resp = await google_request("POST", f"{API}/messages/send", account_id=account, owner=row["owner"], json={"raw": raw})
    if resp.status_code >= 500:
        raise httpx.ReadError(f"gmail {resp.status_code}")  # the server may have accepted it
    if resp.status_code >= 400:
        raise DefiniteFailure(f"gmail {resp.status_code}")
    return resp.json().get("id", "")


async def _exists_in_gmail(row: Dict[str, Any]) -> bool:
    account = row["account_id"] or default_account(row["owner"], "gmail")
    resp = await google_request("GET", f"{API}/messages", account_id=account, owner=row["owner"], params={"q": f"rfc822msgid:{_message_id(row['id'])}", "maxResults": 1})
    resp.raise_for_status()
    return bool(resp.json().get("messages"))


class DefiniteFailure(RuntimeError):
    """The provider rejected it, so it certainly wasn't sent."""


async def tick(send: Callable[[Dict[str, Any]], Awaitable[str]] = _send_gmail, exists: Callable[[Dict[str, Any]], Awaitable[bool]] = _exists_in_gmail, limit: int = 20) -> Dict[str, int]:
    """One worker pass: expired leases become uncertain, uncertain rows reconcile, due rows send."""
    stats = {"sent": 0, "failed": 0, "uncertain": 0, "reconciled_sent": 0, "requeued": 0}
    now = utcnow()
    with session_scope() as db:
        db.query(OutboundEmail).filter(OutboundEmail.status == "sending", OutboundEmail.lease_until < now).update({"status": "uncertain", "error": "envio interrompido"}, synchronize_session=False)
        uncertain = [(r.id, r.owner, r.account_id, r.attempts) for r in db.query(OutboundEmail).filter(OutboundEmail.status == "uncertain").limit(limit).all()]
    for row_id, owner, account_id, attempts in uncertain:
        try:
            found = await exists({"id": row_id, "owner": owner, "account_id": account_id})
        except Exception as exc:  # still unknown: do nothing and ask again next pass
            logger.info("reconcile pending for %s: %s", row_id, redact_text(exc))
            continue
        with session_scope() as db:
            row = db.get(OutboundEmail, row_id)
            if found:
                row.status, row.sent_at, row.error = "sent", utcnow(), ""
                stats["reconciled_sent"] += 1
            elif attempts >= MAX_ATTEMPTS:
                row.status, row.error = "failed", "tentativas esgotadas"
                stats["failed"] += 1
            else:
                row.status, row.error = "queued", ""
                stats["requeued"] += 1
    for row_id in _claim_due(limit):
        with session_scope() as db:
            row = db.get(OutboundEmail, row_id)
            work = {"id": row.id, "owner": row.owner, "account_id": row.account_id, **json.loads(decrypt(row.payload_enc))}
        try:
            provider_id = await send(work)
        except (DefiniteFailure, ReconnectRequired, MissingScope, LookupError) as exc:
            with session_scope() as db:
                row = db.get(OutboundEmail, row_id)
                row.status, row.error = "failed", redact_text(exc)[:300]
            stats["failed"] += 1
            audit.record("gmail.send", owner=work["owner"], risk="external", outcome="error", target=row_id, error=str(exc)[:200])
            continue
        except Exception as exc:  # timeout or network: it may have been delivered
            with session_scope() as db:
                row = db.get(OutboundEmail, row_id)
                row.status, row.error = "uncertain", redact_text(exc)[:300]
            stats["uncertain"] += 1
            continue
        with session_scope() as db:
            row = db.get(OutboundEmail, row_id)
            row.status, row.provider_message_id, row.sent_at, row.error = "sent", provider_id, utcnow(), ""
        stats["sent"] += 1
        audit.record("gmail.send", owner=work["owner"], risk="external", target=row_id, provider_id=provider_id)
    return stats
