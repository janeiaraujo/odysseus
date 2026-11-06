import asyncio
import json
import sqlite3

import pytest

from src.jarvis import audit, config, policy
from src.jarvis.db import session_scope
from src.jarvis.models import Approval, AuditEvent
from src.jarvis.redact import redact, redact_text


def test_jarvis_data_stays_out_of_the_odysseus_database(jarvis_env):
    audit.record("x.y", owner="ana")
    con = sqlite3.connect(jarvis_env / "jarvis.db")
    assert con.execute("select count(*) from jarvis_audit").fetchone()[0] == 1


def test_config_merges_defaults_and_persists():
    config.update_config({"router": {"profile": "local"}})
    assert config.get("router.profile") == "local"
    assert config.get("router.allow_paid_fallback") is False


@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnopqrstuvwx", "AIzaSyA1234567890abcdefghijklmn", "ya29.a0AfB_byC123456789",
    "Bearer abcdef123456789", "eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl", "password=hunter2secret",
])
def test_redact_text_masks_known_secret_shapes(secret):
    assert secret not in redact_text(f"erro ao chamar: {secret} fim")


def test_redact_masks_sensitive_keys_and_emails():
    out = redact({"api_key": "plain", "nested": {"refresh_token": "x", "note": "fale com ana@example.com"}, "n": 3})
    assert out == {"api_key": "***", "nested": {"refresh_token": "***", "note": "fale com <email>"}, "n": 3}


def test_audit_never_stores_secret_values():
    audit.record("gmail.send", owner="ana", token="ya29.secretvalue1234", note="key sk-abcdefghijklmnopqrstuv")
    with session_scope() as db:
        detail = db.query(AuditEvent).one().detail
    assert "secretvalue" not in detail and "sk-abc" not in detail


def test_default_modes_by_risk():
    assert policy.evaluate("calendar.read", policy.READ).mode == policy.AUTO
    assert policy.evaluate("notes.create", policy.REVERSIBLE).mode == policy.AUTO
    assert policy.evaluate("gmail.send", policy.EXTERNAL).mode == policy.CONFIRM
    assert policy.evaluate("x.y", "made_up").mode == policy.CONFIRM


def test_user_rule_can_relax_external_but_never_destructive():
    config.update_config({"autonomy": {"users": {"ana": {"gmail.send": "auto", "drive.delete": "auto", "destructive": "auto"}}}})
    assert policy.evaluate("gmail.send", policy.EXTERNAL, "ana").mode == policy.AUTO
    assert policy.evaluate("gmail.send", policy.EXTERNAL, "bob").mode == policy.CONFIRM
    assert policy.evaluate("drive.delete", policy.DESTRUCTIVE, "ana").mode == policy.CONFIRM
    assert policy.evaluate("door.open", policy.PHYSICAL_CRITICAL, "ana").mode == policy.CONFIRM


def test_deny_rule_blocks_action():
    config.update_config({"autonomy": {"global": {"browser.*": "deny"}}})
    with pytest.raises(policy.ActionDenied):
        asyncio.run(policy.guarded("browser.open", policy.READ, "", "s", {}, lambda: 1))


def test_guarded_runs_allowed_action_and_audits_it():
    assert asyncio.run(policy.guarded("calendar.read", policy.READ, "ana", "s", {}, lambda: 42)) == 42
    assert audit.recent("ana")[0]["action"] == "calendar.read"


def test_confirm_flow_executes_exactly_once():
    calls = []
    policy.register_executor("test.send", lambda owner, payload: calls.append((owner, payload)) or {"sent": True})
    ran = []
    with pytest.raises(policy.ApprovalRequired) as exc:
        asyncio.run(policy.guarded("test.send", policy.EXTERNAL, "ana", "enviar", {"to": "x"}, lambda: ran.append(1)))
    assert ran == [] and calls == []
    approval_id = exc.value.approval_id
    assert policy.list_approvals("ana")[0]["id"] == approval_id
    result = asyncio.run(policy.decide(approval_id, True, owner="ana"))
    assert result["status"] == "executed" and calls == [("ana", {"to": "x"})]
    with pytest.raises(LookupError):
        asyncio.run(policy.decide(approval_id, True, owner="ana"))
    assert len(calls) == 1


def test_owner_cannot_decide_another_owners_approval():
    approval_id = policy.request_approval("a.b", policy.EXTERNAL, "ana", "s", {})
    with pytest.raises(LookupError):
        asyncio.run(policy.decide(approval_id, True, owner="bob"))
    assert policy.list_approvals("bob") == []


def test_approval_payload_is_encrypted_at_rest():
    policy.request_approval("gmail.send", policy.EXTERNAL, "ana", "s", {"body": "segredo pessoal"})
    with session_scope() as db:
        assert "segredo" not in db.query(Approval).one().payload_enc


def test_expired_approval_cannot_be_decided():
    from datetime import timedelta
    from src.jarvis.db import utcnow
    approval_id = policy.request_approval("a.b", policy.EXTERNAL, "ana", "s", {})
    with session_scope() as db:
        db.get(Approval, approval_id).expires_at = utcnow() - timedelta(minutes=1)
    with pytest.raises(LookupError):
        asyncio.run(policy.decide(approval_id, True, owner="ana"))
    assert policy.list_approvals("ana") == []
