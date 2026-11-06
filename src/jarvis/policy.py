"""Approval policy for every Jarvis action.

Risk classes come from the action definition, never from model output. The
floors below cannot be relaxed by configuration: destructive, financial and
critical physical actions always wait for a human.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from src.jarvis import audit, config
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import Approval
from src.jarvis.redact import redact_text
from src.secret_storage import decrypt, encrypt

READ, REVERSIBLE, EXTERNAL, DESTRUCTIVE, FINANCIAL, PHYSICAL_CRITICAL = (
    "read", "reversible", "external", "destructive", "financial", "physical_critical",
)
AUTO, CONFIRM, DENY = "auto", "confirm", "deny"

DEFAULT_MODES = {READ: AUTO, REVERSIBLE: AUTO, EXTERNAL: CONFIRM, DESTRUCTIVE: CONFIRM,
                 FINANCIAL: CONFIRM, PHYSICAL_CRITICAL: CONFIRM}
_ALWAYS_CONFIRM = {DESTRUCTIVE, FINANCIAL, PHYSICAL_CRITICAL}
APPROVAL_TTL = timedelta(days=7)

Executor = Callable[[str, Dict[str, Any]], Union[Any, Awaitable[Any]]]
_executors: Dict[str, Executor] = {}


class ApprovalRequired(Exception):
    def __init__(self, approval_id: str):
        super().__init__(f"approval required: {approval_id}")
        self.approval_id = approval_id


class ActionDenied(Exception):
    pass


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str


def register_executor(action: str, fn: Executor) -> None:
    _executors[action] = fn


def evaluate(action: str, risk: str, owner: Optional[str] = "") -> Decision:
    if risk not in DEFAULT_MODES:
        return Decision(CONFIRM, f"risco desconhecido: {risk}")
    autonomy = config.get("autonomy", {}) or {}
    user_rules = (autonomy.get("users") or {}).get(owner or "", {}) or {}
    global_rules = autonomy.get("global") or {}
    prefix = action.split(".", 1)[0]
    for scope, rules in (("usuario", user_rules), ("global", global_rules)):
        for key in (action, f"{prefix}.*", risk):
            if key in rules:
                mode = rules[key]
                if mode == DENY:
                    return Decision(DENY, f"regra {scope}:{key}")
                if mode == AUTO and risk in _ALWAYS_CONFIRM:
                    return Decision(CONFIRM, f"{risk} sempre exige aprovacao")
                if mode in (AUTO, CONFIRM):
                    return Decision(mode, f"regra {scope}:{key}")
    return Decision(DEFAULT_MODES[risk], f"padrao para {risk}")


def request_approval(action: str, risk: str, owner: Optional[str], summary: str, payload: Dict[str, Any]) -> str:
    with session_scope() as db:
        row = Approval(owner=owner or "", action=action, risk=risk, summary=redact_text(summary)[:2000],
                       payload_enc=encrypt(json.dumps(payload, ensure_ascii=False, default=str)),
                       expires_at=utcnow() + APPROVAL_TTL)
        db.add(row)
        db.flush()
        approval_id = row.id
    audit.record("approval.requested", owner=owner, target=action, risk=risk, approval_id=approval_id)
    return approval_id


async def guarded(action: str, risk: str, owner: Optional[str], summary: str, payload: Dict[str, Any], fn: Callable[[], Any]) -> Any:
    """Run fn() if policy allows; otherwise queue an approval and raise."""
    decision = evaluate(action, risk, owner)
    if decision.mode == DENY:
        audit.record(action, owner=owner, risk=risk, outcome="denied", reason=decision.reason)
        raise ActionDenied(decision.reason)
    if decision.mode == CONFIRM:
        raise ApprovalRequired(request_approval(action, risk, owner, summary, payload))
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        audit.record(action, owner=owner, risk=risk, outcome="error", error=str(exc)[:300])
        raise
    audit.record(action, owner=owner, risk=risk, reason=decision.reason)
    return result


def _as_dict(row: Approval) -> Dict[str, Any]:
    return {"id": row.id, "action": row.action, "risk": row.risk, "summary": row.summary, "status": row.status,
            "result": row.result, "created_at": row.created_at.isoformat() + "Z",
            "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None}


def list_approvals(owner: Optional[str] = None, status: Optional[str] = "pending", limit: int = 100) -> List[Dict[str, Any]]:
    now = utcnow()
    with session_scope() as db:
        q = db.query(Approval)
        if owner is not None:
            q = q.filter(Approval.owner == owner)
        for row in q.filter(Approval.status == "pending", Approval.expires_at < now).all():
            row.status = "expired"
        db.flush()
        if status:
            q = q.filter(Approval.status == status)
        return [_as_dict(r) for r in q.order_by(Approval.created_at.desc()).limit(limit).all()]


async def decide(approval_id: str, approve: bool, *, owner: Optional[str], decided_by: str = "") -> Dict[str, Any]:
    """Atomically claim a pending approval, so a double click can't run it twice."""
    with session_scope() as db:
        q = db.query(Approval).filter(Approval.id == approval_id, Approval.status == "pending", Approval.expires_at >= utcnow())
        if owner is not None:
            q = q.filter(Approval.owner == owner)
        claimed = q.update({"status": "approved" if approve else "rejected", "decided_at": utcnow(), "decided_by": decided_by},
                           synchronize_session=False)
        if not claimed:
            raise LookupError("aprovacao inexistente, expirada ou ja decidida")
        row = db.get(Approval, approval_id)
        action, risk, row_owner, payload_enc = row.action, row.risk, row.owner, row.payload_enc
    audit.record("approval.approved" if approve else "approval.rejected", owner=row_owner, actor=decided_by or "user",
                 target=action, risk=risk, approval_id=approval_id)
    status, result_text = ("approved" if approve else "rejected"), ""
    if approve and action in _executors:
        try:
            result = _executors[action](row_owner, json.loads(decrypt(payload_enc)))
            if inspect.isawaitable(result):
                result = await result
            status, result_text = "executed", redact_text(json.dumps(result, ensure_ascii=False, default=str))[:2000]
        except Exception as exc:
            status, result_text = "failed", redact_text(exc)[:500]
        audit.record(action, owner=row_owner, risk=risk, outcome="ok" if status == "executed" else "error",
                     approval_id=approval_id, error=result_text if status == "failed" else "")
        with session_scope() as db:
            row = db.get(Approval, approval_id)
            row.status, row.result = status, result_text
    return {"id": approval_id, "status": status, "result": result_text}
