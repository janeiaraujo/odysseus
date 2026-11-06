"""HTTP API for J.A.R.V.I.S. Every route needs an authenticated user; every query is owner-scoped.

Like email and calendar in Odysseus's threat model, anything that touches external
accounts, devices or the system is admin-only.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from core.middleware import require_admin
from routes.auth_routes import SESSION_COOKIE
from src.auth_helpers import effective_user, require_user
from src.jarvis import agenda, audit, config, home, intents, learning, mailqueue, memory, missions, notify, observability, persona, policy, replication, router as model_router, voice
from src.jarvis import browser as jbrowser
from src.jarvis.firebase import FirebaseNotConfigured
from src.jarvis.google import calendar as gcal, drive, gmail, oauth, photos
from src.jarvis.google.calendar import GoogleApiError
from src.jarvis.redact import redact_text
from src.jarvis.storage import QuotaExceeded, StorageError, get_backend, usage_this_month
from src.jarvis.storage import zerokey
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)
PATCHABLE_CONFIG = {"enabled", "timezone", "intents", "persona", "router", "budget", "pricing", "autonomy", "learning", "memory", "voice", "firebase", "storage", "notifications", "browser"}
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class _JarvisRoute(APIRoute):
    """Translate domain exceptions to HTTP, without exposing internals."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except policy.ApprovalRequired as exc:
                return JSONResponse(status_code=202, content={"pending_approval": exc.approval_id})
            except policy.ActionDenied as exc:
                raise HTTPException(403, f"negado pela política: {exc}") from None
            except oauth.ReconnectRequired as exc:
                return JSONResponse(status_code=409, content={"reconnect_required": True, "account_id": exc.account_id, "detail": "reconecte a conta Google"})
            except oauth.MissingScope as exc:
                return JSONResponse(status_code=409, content={"missing_scope": exc.group, "detail": f"autorize {exc.group} nas Integracoes"})
            except (oauth.GoogleNotConfigured, home.HomeNotConfigured, FirebaseNotConfigured, voice.VoiceUnavailable, jbrowser.BrowserUnavailable) as exc:
                raise HTTPException(503, redact_text(exc)) from None
            except (GoogleApiError, StorageError, QuotaExceeded) as exc:
                raise HTTPException(502, redact_text(exc)) from None
            except jbrowser.HumanRequired as exc:
                return JSONResponse(status_code=409, content={"human_required": True, "detail": str(exc)})
            except (oauth.OAuthStateError, jbrowser.BlockedUrl, home.UnsafeAction, ValueError) as exc:
                raise HTTPException(400, redact_text(exc)) from None
            except LookupError as exc:
                raise HTTPException(404, redact_text(exc)) from None
        return handler


def _owner(request: Request) -> str:
    require_user(request)
    return effective_user(request) or ""


def _admin(request: Request) -> str:
    require_admin(request)
    return _owner(request)


def _redirect_uri(request: Request, path: str) -> str:
    base = os.environ.get("JARVIS_PUBLIC_URL") or str(request.base_url)
    return base.rstrip("/") + path


class RoutingBody(BaseModel):
    auto: bool


class CommandBody(BaseModel):
    text: str = Field(max_length=4000)


class RememberBody(BaseModel):
    text: str = Field(max_length=4000)
    kind: str = "semantic"
    category: str = "fact"
    priority: int = 0
    valid_until: Optional[datetime] = None


class CorrectBody(BaseModel):
    text: str = Field(max_length=4000)


class ResolveBody(BaseModel):
    keep_id: str
    drop_id: str


class DecideBody(BaseModel):
    approve: bool
    edited_text: Optional[str] = None


class SelectBody(BaseModel):
    profile: Optional[str] = None
    requires: List[str] = []


class MissionBody(BaseModel):
    title: str = Field(max_length=200)
    goal: str = ""
    steps: List[Dict[str, Any]]
    cron: Optional[str] = None
    timezone: str = "America/Sao_Paulo"


class PlanBody(BaseModel):
    goal: str = Field(max_length=2000)
    cron: Optional[str] = None


class ConnectBody(BaseModel):
    groups: List[str]


class EventBody(BaseModel):
    title: str
    start: str
    end: str
    timezone: str = "America/Sao_Paulo"
    attendees: List[str] = []
    location: str = ""
    description: str = ""
    reminder_minutes: Optional[int] = None
    recurrence: List[str] = []


class EmailBody(BaseModel):
    to: List[str]
    cc: List[str] = []
    subject: str = Field(max_length=500)
    body: str = Field(max_length=200000)
    send_at: Optional[datetime] = None
    timezone: str = "America/Sao_Paulo"
    idempotency_key: str = ""
    account_id: str = ""


class FoldersBody(BaseModel):
    folder_ids: List[str]


class DeviceBody(BaseModel):
    name: str
    entity_id: str
    domain: Optional[str] = None
    area: str = ""
    aliases: List[str] = []
    critical: bool = False


class HomeBody(BaseModel):
    url: str = ""
    token: Optional[str] = None


class ControlBody(BaseModel):
    device_id: str
    op: str
    value: Any = None


class UrlsBody(BaseModel):
    urls: List[str]


class AnalyzeBody(BaseModel):
    session_id: str
    item_id: str
    remember: bool = False


class BackupBody(BaseModel):
    include_secrets: bool = True


def _scrub_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    if isinstance(out.get("home"), dict):
        out["home"] = {k: v for k, v in out["home"].items() if k != "token_enc"}
    return out


async def _describe_image(owner: str, image: bytes, mime: str) -> str:
    from src.jarvis.llm import complete
    return await complete(owner, photos.vision_request(image, mime), requires=["vision"], purpose="vision")


def setup_jarvis_routes(memory_manager: Any = None) -> APIRouter:
    r = APIRouter(prefix="/api/jarvis", tags=["jarvis"], route_class=_JarvisRoute)
    login_limiter = RateLimiter(max_requests=15, window_seconds=60)

    # --- identity, dashboard, commands ---
    @r.get("/status")
    async def status(request: Request):
        owner = _owner(request)
        cfg = config.load_config()
        data: Dict[str, Any] = {"identity": {"name": cfg["persona"]["name"], "meaning": "Just A Rather Very Intelligent System", "language": cfg["persona"]["language"], "enabled": cfg["enabled"]},
                                "now": agenda.now_local().isoformat(), "memory": memory.stats(owner), "approvals_pending": len(policy.list_approvals(owner)),
                                "learning_pending": len(learning.list_candidates(owner)), "notifications_unread": len(notify.list_notifications(owner, unread_only=True)),
                                "voice": voice.status(), "google_configured": oauth.is_configured(), "home": home.status()}
        return data

    @r.post("/command")
    async def command(body: CommandBody, request: Request):
        """Deterministic commands only; `handled: false` means the chat should take over."""
        answer = await intents.handle(body.text, _owner(request))
        return {"handled": bool(answer), **(answer or {})}

    @r.get("/config")
    async def get_config(request: Request):
        _admin(request)
        return _scrub_config(config.load_config())

    @r.patch("/config")
    async def patch_config(patch: Dict[str, Any], request: Request):
        owner = _admin(request)
        unknown = set(patch) - PATCHABLE_CONFIG
        if unknown:
            raise HTTPException(400, f"configuração não editável: {sorted(unknown)}")
        mode = (patch.get("router") or {}).get("paid_fallback_mode")
        if mode is not None and mode not in ("never", "configured", "always"):
            raise HTTPException(400, "paid_fallback_mode inválido")
        updated = config.update_config(patch)
        audit.record("config.update", owner=owner, risk="reversible", sections=sorted(patch))
        return _scrub_config(updated)

    @r.get("/audit")
    async def get_audit(request: Request, limit: int = 100, prefix: str = ""):
        return {"events": audit.recent(_owner(request), limit=limit, action_prefix=prefix)}

    @r.get("/notifications")
    async def get_notifications(request: Request, unread_only: bool = False):
        return {"notifications": notify.list_notifications(_owner(request), unread_only=unread_only)}

    @r.post("/notifications/read")
    async def read_notifications(request: Request):
        return {"updated": notify.mark_read(_owner(request))}

    # --- models ---
    @r.get("/models")
    async def models(request: Request, profile: Optional[str] = None):
        owner = _owner(request)
        ranked = model_router.rank(owner=owner, profile=profile, include_unavailable=True)
        return {"profile": profile or config.get("router.profile"), "profiles": {k: v["label"] for k, v in model_router.PROFILES.items()},
                "paid_fallback_mode": config.get("router.paid_fallback_mode"), "models": [m.to_dict() for m in ranked[:200]],
                "catalog_total": len(model_router.catalog(owner))}

    @r.post("/models/select")
    async def select_model(body: SelectBody, request: Request):
        choice = model_router.select(owner=_owner(request), profile=body.profile, requires=body.requires)
        if choice is None:
            raise HTTPException(404, "nenhum modelo disponivel dentro das regras de custo")
        return choice.to_dict()

    @r.get("/routing")
    async def routing_get(request: Request):
        return {"auto": model_router.auto_chat_enabled(_owner(request))}

    @r.put("/routing")
    async def routing_set(body: RoutingBody, request: Request):
        owner = _owner(request)
        model_router.set_auto_chat(owner, body.auto)
        audit.record("router.auto_chat", owner=owner, risk="reversible", enabled=body.auto)
        return {"auto": body.auto}

    @r.get("/usage")
    async def usage(request: Request, days: int = 30):
        return model_router.usage_summary(_owner(request), days=days)

    @r.post("/pricing/refresh")
    async def refresh_pricing(request: Request):
        _admin(request)
        return {"updated": await model_router.refresh_openrouter_pricing()}

    # --- memory ---
    @r.get("/memory")
    async def memory_list(request: Request, kind: Optional[str] = None, status: Optional[str] = None, q: str = "", include_hypotheses: bool = False, limit: int = 100, offset: int = 0):
        owner = _owner(request)
        if q:
            return {"items": memory.search(q, owner=owner, k=min(limit, 50), include_hypotheses=include_hypotheses), "stats": memory.stats(owner)}
        return {"items": memory.list_items(owner, kind=kind, status=status, limit=limit, offset=offset), "stats": memory.stats(owner)}

    @r.post("/memory")
    async def memory_add(body: RememberBody, request: Request):
        return memory.remember(body.text, owner=_owner(request), kind=body.kind, category=body.category, priority=body.priority, valid_until=body.valid_until, source_type="user", source_ref="painel")

    @r.patch("/memory/{memory_id}")
    async def memory_correct(memory_id: str, body: CorrectBody, request: Request):
        return memory.correct(memory_id, body.text, owner=_owner(request))

    @r.delete("/memory/{memory_id}")
    async def memory_delete(memory_id: str, request: Request, purge: bool = False):
        memory.delete(memory_id, owner=_owner(request), purge=purge)
        return {"deleted": True, "purged": purge}

    @r.get("/memory/{memory_id}/versions")
    async def memory_versions(memory_id: str, request: Request):
        return {"versions": memory.versions(memory_id, owner=_owner(request))}

    @r.get("/memory/conflicts")
    async def memory_conflicts(request: Request):
        return {"conflicts": memory.conflicts(_owner(request))}

    @r.post("/memory/resolve")
    async def memory_resolve(body: ResolveBody, request: Request):
        return memory.resolve_conflict(body.keep_id, body.drop_id, owner=_owner(request))

    @r.post("/memory/consolidate")
    async def memory_consolidate(request: Request):
        return memory.consolidate(_owner(request))

    @r.post("/memory/reindex")
    async def memory_reindex(request: Request):
        _admin(request)
        return memory.reindex()

    @r.post("/memory/import-odysseus")
    async def memory_import(request: Request):
        """Copies Odysseus memories into Jarvis. The source file isn't changed."""
        owner = _owner(request)
        if memory_manager is None:
            raise HTTPException(503, "memória do Odysseus indisponível")
        return memory.import_odysseus(memory_manager.load(owner=owner or None), owner)

    @r.get("/learning/candidates")
    async def candidates(request: Request):
        return {"candidates": learning.list_candidates(_owner(request))}

    @r.post("/learning/candidates/{candidate_id}")
    async def decide_candidate(candidate_id: str, body: DecideBody, request: Request):
        return learning.decide_candidate(candidate_id, body.approve, owner=_owner(request), edited_text=body.edited_text)

    @r.post("/learning/dataset")
    async def export_dataset(request: Request):
        return learning.export_dataset(_admin(request))

    # --- approvals ---
    @r.get("/approvals")
    async def approvals(request: Request, status: Optional[str] = "pending"):
        return {"approvals": policy.list_approvals(_owner(request), status=status or None)}

    @r.post("/approvals/{approval_id}")
    async def decide(approval_id: str, body: DecideBody, request: Request):
        owner = _owner(request)
        return await policy.decide(approval_id, body.approve, owner=owner, decided_by=owner or "user")

    # --- missions ---
    @r.get("/missions")
    async def missions_list(request: Request):
        return {"missions": missions.list_missions(_admin(request))}

    @r.post("/routines")
    async def routines_enable(request: Request):
        return missions.enable_recommended(_admin(request))

    @r.get("/missions/catalog")
    async def missions_catalog(request: Request):
        _admin(request)
        return {"handlers": missions.handler_catalog()}

    @r.post("/missions")
    async def missions_create(body: MissionBody, request: Request):
        return missions.create(_admin(request), body.title, body.steps, goal=body.goal, cron=body.cron, tz=body.timezone)

    @r.post("/missions/plan")
    async def missions_plan(body: PlanBody, request: Request):
        owner = _admin(request)
        from src.jarvis.llm import complete

        async def llm(messages):
            return await complete(owner, messages, purpose="planner")
        return await missions.plan_goal(owner, body.goal, llm, cron=body.cron)

    @r.get("/missions/{mission_id}")
    async def missions_get(mission_id: str, request: Request):
        return missions.get(_admin(request), mission_id)

    @r.delete("/missions/{mission_id}")
    async def missions_cancel(mission_id: str, request: Request):
        missions.cancel(_admin(request), mission_id)
        return {"cancelled": True}

    # --- Google: accounts ---
    @r.get("/google/accounts")
    async def google_accounts(request: Request):
        owner = _admin(request)
        return {"configured": oauth.is_configured(), "accounts": oauth.list_accounts(owner), "groups": [g for g in oauth.SCOPE_GROUPS if g != "login"]}

    @r.post("/google/connect")
    async def google_connect(body: ConnectBody, request: Request):
        """Incremental consent: only the groups asked for are requested."""
        url = oauth.begin(_admin(request), body.groups, _redirect_uri(request, "/api/jarvis/google/callback"))
        return {"url": url}

    @r.get("/google/callback")
    async def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        owner = _admin(request)
        if error or not code:
            return RedirectResponse("/?jarvis=google_denied", status_code=302)
        await oauth.complete_connect(state, code, _redirect_uri(request, "/api/jarvis/google/callback"), owner)
        missions.enable_recommended(owner)
        return RedirectResponse("/?jarvis=google_connected", status_code=302)

    @r.delete("/google/accounts/{account_id}")
    async def google_revoke(account_id: str, request: Request):
        return await oauth.revoke(account_id, _admin(request))

    @r.get("/google/login-available")
    async def google_login_available():
        return {"available": oauth.is_configured()}

    @r.get("/google/login")
    async def google_login(request: Request):
        """Public by design (exempt in AuthMiddleware). Only linked accounts can get in."""
        if not login_limiter.check(request.client.host if request.client else "?"):
            raise HTTPException(429, "muitas tentativas")
        return RedirectResponse(oauth.begin("", [], _redirect_uri(request, "/api/jarvis/google/login/callback"), purpose="login"), status_code=302)

    @r.get("/google/login/callback")
    async def google_login_callback(request: Request, state: str = "", code: str = ""):
        if not login_limiter.check(request.client.host if request.client else "?"):
            raise HTTPException(429, "muitas tentativas")
        try:
            username = await oauth.complete_login(state, code, _redirect_uri(request, "/api/jarvis/google/login/callback"))
        except oauth.OAuthStateError as exc:
            logger.info("google login refused: %s", redact_text(exc))
            return RedirectResponse("/login?error=google", status_code=302)
        auth = request.app.state.auth_manager
        if auth.totp_enabled(username):  # must not become a way around the second factor
            audit.record("google.login", owner=username, outcome="denied", reason="2fa ativo")
            return RedirectResponse("/login?error=google_2fa", status_code=302)
        token = auth.create_session_trusted(username)
        if not token:
            return RedirectResponse("/login?error=google", status_code=302)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(key=SESSION_COOKIE, value=token, httponly=True, samesite="lax", secure=os.getenv("SECURE_COOKIES", "false").lower() == "true", path="/")
        return response

    # --- Google Calendar ---
    @r.get("/google/calendars")
    async def calendars(request: Request):
        return {"calendars": await gcal.list_calendars(_admin(request))}

    @r.get("/google/events")
    async def events(request: Request, start: datetime, end: datetime, q: str = "", calendar_id: str = "primary"):
        return {"events": await gcal.list_events(_admin(request), start=start, end=end, query=q, calendar_id=calendar_id)}

    @r.post("/google/events")
    async def create_event(body: EventBody, request: Request):
        return await gcal.create_event(_admin(request), title=body.title, start=body.start, end=body.end, tz=body.timezone, attendees=body.attendees, location=body.location,
                                       description=body.description, reminder_minutes=body.reminder_minutes, recurrence=body.recurrence)

    @r.patch("/google/events/{event_id}")
    async def update_event(event_id: str, changes: Dict[str, Any], request: Request):
        return await gcal.update_event(_admin(request), event_id, changes)

    @r.delete("/google/events/{event_id}")
    async def delete_event(event_id: str, request: Request):
        return await gcal.delete_event(_admin(request), event_id)

    @r.post("/google/calendar/sync")
    async def calendar_sync(request: Request):
        return await gcal.sync(_admin(request))

    # --- Gmail ---
    @r.get("/gmail/search")
    async def gmail_search(request: Request, sender: str = "", subject: str = "", after: str = "", before: str = "", text: str = "", limit: int = 20):
        query = gmail.build_query(sender=sender, subject=subject, after=after, before=before, text=text)
        return {"messages": await gmail.search(_admin(request), query, max_results=limit)}

    @r.get("/gmail/messages/{message_id}")
    async def gmail_message(message_id: str, request: Request):
        owner = _admin(request)
        message = await gmail.get_message(owner, message_id)
        return {**message, "analysis": gmail.classify(message)}

    @r.post("/gmail/triage")
    async def gmail_triage(request: Request, apply_labels: bool = False):
        return await gmail.triage(_admin(request), apply_labels=apply_labels)

    @r.get("/gmail/messages/{message_id}/attachments/{attachment_id}")
    async def gmail_attachment(message_id: str, attachment_id: str, request: Request):
        return await gmail.attachment_text(_admin(request), message_id, attachment_id)

    @r.post("/gmail/messages/{message_id}/summary")
    async def gmail_summary(message_id: str, request: Request):
        owner = _admin(request)
        from src.jarvis.llm import complete
        message = await gmail.get_message(owner, message_id)
        return {"summary": await complete(owner, gmail.summarize_prompt(message), purpose="email")}

    @r.get("/gmail/insights")
    async def gmail_insights(request: Request, important_only: bool = False):
        return {"insights": gmail.insights(_admin(request), important_only=important_only)}

    @r.post("/gmail/drafts")
    async def gmail_draft(body: EmailBody, request: Request):
        return await gmail.create_draft(_admin(request), to=body.to, subject=body.subject, body=body.body)

    @r.post("/gmail/outbox")
    async def gmail_schedule(body: EmailBody, request: Request):
        """Queues the message; by default it waits for approval before it can be sent."""
        return mailqueue.schedule(_admin(request), to=body.to, cc=body.cc, subject=body.subject, body=body.body, send_at=body.send_at, tz=body.timezone,
                                  idempotency_key=body.idempotency_key, account_id=body.account_id)

    @r.get("/gmail/outbox")
    async def gmail_outbox(request: Request):
        return {"queue": mailqueue.list_queue(_admin(request))}

    @r.delete("/gmail/outbox/{row_id}")
    async def gmail_cancel(row_id: str, request: Request):
        return {"cancelled": mailqueue.cancel(_admin(request), row_id)}

    # --- Drive ---
    @r.get("/drive/files")
    async def drive_files(request: Request, folder_id: str = "", q: str = ""):
        return {"files": await drive.list_files(_admin(request), folder_id=folder_id, query=q)}

    @r.get("/drive/allowed")
    async def drive_allowed(request: Request):
        return {"folder_ids": drive.allowed_folders(_admin(request))}

    @r.put("/drive/allowed")
    async def drive_set_allowed(body: FoldersBody, request: Request):
        drive.set_allowed_folders(_admin(request), body.folder_ids)
        return {"folder_ids": body.folder_ids}

    @r.post("/drive/sync")
    async def drive_sync(request: Request):
        return await drive.sync_changes(_admin(request))

    @r.get("/drive/search")
    async def drive_search(request: Request, q: str):
        return {"results": drive.search_documents(q, owner=_admin(request))}

    @r.get("/drive/duplicates")
    async def drive_duplicates(request: Request):
        return {"duplicates": drive.find_duplicates(_admin(request))}

    # --- Photos ---
    @r.post("/photos/sessions")
    async def photos_session(request: Request):
        return await photos.create_session(_admin(request))

    @r.get("/photos/sessions/{session_id}")
    async def photos_status(session_id: str, request: Request):
        owner = _admin(request)
        state = await photos.session_status(owner, session_id)
        items = await photos.list_items(owner, session_id) if state["ready"] else []
        return {**state, "items": [{k: v for k, v in i.items() if k != "base_url"} for i in items]}

    @r.get("/photos/sessions/{session_id}/items/{item_id}/image")
    async def photos_image(session_id: str, item_id: str, request: Request, size: int = 512):
        image = await photos.fetch_image(_admin(request), session_id, item_id, size=size)
        return Response(content=image["content"], media_type=image["mime"], headers={"Cache-Control": "no-store"})

    @r.post("/photos/analyze")
    async def photos_analyze(body: AnalyzeBody, request: Request):
        """The item is looked up in Google's own list, so a caller can't supply a URL."""
        owner = _admin(request)
        item = next((i for i in await photos.list_items(owner, body.session_id) if i["id"] == body.item_id), None)
        if item is None:
            raise LookupError("foto não encontrada nesta sessão")

        async def describe(image: bytes, mime: str) -> str:
            return await _describe_image(owner, image, mime)
        return await photos.analyze_item(owner, item, describe, remember=body.remember)

    @r.delete("/photos/sessions/{session_id}")
    async def photos_delete(session_id: str, request: Request):
        await photos.delete_session(_admin(request), session_id)
        return {"deleted": True}

    # --- Home ---
    @r.get("/home")
    async def home_state(request: Request):
        return {**home.status(), "devices": home.list_devices(_admin(request))}

    @r.put("/home/config")
    async def home_config(body: HomeBody, request: Request):
        owner = _admin(request)
        home.configure(body.url, body.token)
        audit.record("home.configure", owner=owner, risk="reversible")
        return home.status()

    @r.post("/home/discover")
    async def home_discover(request: Request):
        return await home.discover(_admin(request))

    @r.post("/home/devices")
    async def home_add(body: DeviceBody, request: Request):
        owner = _admin(request)
        device = home.upsert_device(owner, name=body.name, entity_id=body.entity_id, domain=body.domain, area=body.area, aliases=body.aliases, critical=body.critical)
        replication.enqueue_device(device, owner)
        return device

    @r.delete("/home/devices/{device_id}")
    async def home_delete(device_id: str, request: Request):
        if not home.delete_device(_admin(request), device_id):
            raise LookupError("dispositivo não encontrado")
        return {"deleted": True}

    @r.get("/home/devices/{device_id}/state")
    async def home_state_read(device_id: str, request: Request):
        device = next((d for d in home.list_devices(_admin(request)) if d["id"] == device_id), None)
        if device is None:
            raise LookupError("dispositivo não encontrado")
        return await home.read_device(device)

    @r.get("/home/devices/{device_id}/snapshot")
    async def home_snapshot(device_id: str, request: Request):
        owner = _admin(request)
        device = next((d for d in home.list_devices(owner) if d["id"] == device_id), None)
        if device is None:
            raise LookupError("dispositivo não encontrado")
        return Response(content=await home.camera_snapshot(device, owner=owner), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.post("/home/control")
    async def home_control(body: ControlBody, request: Request):
        owner = _admin(request)
        device = next((d for d in home.list_devices(owner) if d["id"] == body.device_id), None)
        if device is None:
            raise LookupError("dispositivo não encontrado")
        return await home.execute(device, body.op, body.value, owner=owner)

    # --- Voice ---
    @r.get("/voice/status")
    async def voice_status(request: Request):
        _owner(request)
        return voice.status()

    @r.post("/voice/turn")
    async def voice_turn(request: Request, audio: Optional[UploadFile] = File(None), text: str = Form(""), wake_word: bool = Form(False), speak: bool = Form(True)):
        owner = _owner(request)
        data = None
        if audio is not None:
            data = await audio.read(MAX_AUDIO_BYTES + 1)
            if len(data) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "audio grande demais")
        if not data and not text.strip():
            raise HTTPException(400, "envie audio ou texto")
        return await voice.turn(owner, audio=data, text=text or None, require_wake_word=wake_word, speak=speak)

    @r.get("/voice/history")
    async def voice_history(request: Request):
        return {"turns": voice.history(_owner(request))}

    @r.delete("/voice/history")
    async def voice_clear(request: Request):
        return {"deleted": voice.clear_history(_owner(request))}

    # --- Browser ---
    @r.post("/browser/report")
    async def browser_report(body: UrlsBody, request: Request):
        return await jbrowser.report(_admin(request), body.urls)

    @r.delete("/browser/session")
    async def browser_forget(request: Request):
        return {"forgotten": jbrowser.forget_session(_admin(request))}

    # --- Storage, backup, replication, monitoring (system-wide: admin) ---
    @r.post("/credentials/{kind}")
    async def credentials_upload(kind: str, request: Request, file: UploadFile = File(...)):
        """Install a key downloaded from Google. Kept with mode 600, never echoed back."""
        import json as _json

        from src.constants import JARVIS_CREDENTIALS_DIR
        owner = _admin(request)
        if kind not in ("google", "firebase"):
            raise HTTPException(404, "tipo de credencial desconhecido")
        raw = await file.read(64 * 1024 + 1)
        try:
            data = _json.loads(raw)
        except ValueError:
            raise HTTPException(400, "o arquivo não é um JSON válido") from None
        if len(raw) > 64 * 1024 or not isinstance(data, dict):
            raise HTTPException(400, "arquivo inesperado")
        if kind == "firebase" and not (data.get("type") == "service_account" and data.get("private_key") and data.get("client_email")):
            raise HTTPException(400, "este arquivo não é uma chave de conta de serviço")
        if kind == "google" and not (data.get("web") or {}).get("client_secret"):
            raise HTTPException(400, "este arquivo não é um cliente OAuth do tipo Aplicativo da Web")
        os.makedirs(JARVIS_CREDENTIALS_DIR, mode=0o700, exist_ok=True)
        target = Path(JARVIS_CREDENTIALS_DIR) / ("firebase-sa.json" if kind == "firebase" else "google-oauth-client.json")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        audit.record("credentials.install", owner=owner, risk="reversible", target=kind)
        if kind == "google":
            return {"installed": True, "configured": oauth.is_configured()}
        config.update_config({"firebase": {"enabled": True, "project_id": data.get("project_id", ""), "credentials_file": str(target)}})
        from src.jarvis.firebase import check_connection
        try:
            return {"installed": True, "check": await check_connection()}
        except Exception as exc:
            return {"installed": True, "check": {"ok": False, "error": redact_text(exc)[:200]}}

    @r.post("/firebase/check")
    async def firebase_check(request: Request):
        _admin(request)
        from src.jarvis.firebase import check_connection
        return await check_connection()

    @r.get("/storage")
    async def storage_status(request: Request):
        _admin(request)
        return {"backend": config.get("storage.backend"), "usage": usage_this_month(), "r2_configured": bool(os.environ.get("JARVIS_R2_BUCKET")), "replication": replication.status()}

    @r.get("/storage/zero-byte/costs")
    async def zero_costs(request: Request, records_per_month: int = 100000):
        _admin(request)
        return zerokey.compare_costs(records_per_month)

    @r.post("/storage/zero-byte/benchmark")
    async def zero_benchmark(request: Request, records: int = 20):
        _admin(request)
        from src.secret_storage import _load_or_create_key
        return zerokey.benchmark(get_backend(), master_key=_load_or_create_key(), records=min(records, 200))

    @r.post("/replication/flush")
    async def replication_flush(request: Request):
        _admin(request)
        return await replication.flush()

    @r.get("/backups")
    async def backups_list(request: Request):
        _admin(request)
        from src.jarvis import backup as _backup
        return {"backups": observability.backups(), "key_configured": bool(_backup.configured_key())}

    @r.post("/backups")
    async def backups_create(body: BackupBody, request: Request):
        """Runs in a worker thread: encrypting must not block the event loop."""
        import asyncio
        owner = _admin(request)
        from src.jarvis import backup
        if not backup.configured_key():
            raise HTTPException(503, "defina JARVIS_BACKUP_KEY_FILE para criar backups pela interface")
        try:
            result = await asyncio.to_thread(backup.run_configured, body.include_secrets)
        except backup.BackupError as exc:
            raise HTTPException(500, redact_text(exc)) from None
        audit.record("backup.create", owner=owner, risk="reversible", files=result["files"], chroma=result["chroma_included"])
        return result

    @r.get("/monitor")
    async def monitor(request: Request):
        return observability.snapshot(_admin(request))

    return r
