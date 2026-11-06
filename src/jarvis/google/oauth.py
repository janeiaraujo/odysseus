"""Google OAuth 2.0 / OpenID Connect with PKCE and incremental consent.

Tokens are encrypted at rest and never logged. A rejected refresh token flips the
account to `reauth_required` and raises ReconnectRequired; nothing retries around
Google's decision. Reuses GOOGLE_OAUTH_CLIENT_ID/SECRET, which Odysseus email
already reads.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlencode

import httpx

from src.jarvis import audit
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import OAuthAccount, OAuthState
from src.jarvis.redact import redact_text
from src.secret_storage import decrypt, encrypt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
IDENTITY = ["openid", "email", "profile"]
SCOPE_GROUPS: Dict[str, List[str]] = {
    "login": [],
    "calendar": ["https://www.googleapis.com/auth/calendar"],
    "gmail": ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"],
    "drive": ["https://www.googleapis.com/auth/drive.readonly"],
    "photos": ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"],
}
STATE_TTL = timedelta(minutes=10)
_transport: Any = None


class GoogleNotConfigured(RuntimeError):
    pass


class ReconnectRequired(RuntimeError):
    def __init__(self, account_id: str, reason: str = "reconexao necessaria"):
        super().__init__(reason)
        self.account_id = account_id


class MissingScope(RuntimeError):
    def __init__(self, group: str):
        super().__init__(f"a conta nao autorizou {group}")
        self.group = group


class OAuthStateError(ValueError):
    pass


def set_transport(transport: Any) -> None:
    """Test hook: route all Google traffic through an httpx transport."""
    global _transport
    _transport = transport


def http_client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


def client_credentials() -> tuple:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise GoogleNotConfigured("defina GOOGLE_OAUTH_CLIENT_ID e GOOGLE_OAUTH_CLIENT_SECRET")
    return client_id, secret


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def begin(owner: Optional[str], groups: Iterable[str], redirect_uri: str, *, purpose: str = "connect", account_hint: str = "") -> str:
    """Build the consent URL. The state is single-use, expires, and is bound to the owner."""
    client_id, _ = client_credentials()
    groups = list(groups)
    unknown = set(groups) - set(SCOPE_GROUPS)
    if unknown:
        raise ValueError(f"grupo de permissao desconhecido: {sorted(unknown)}")
    scopes = list(dict.fromkeys(IDENTITY + [s for g in groups for s in SCOPE_GROUPS[g]]))
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with session_scope() as db:
        db.query(OAuthState).filter(OAuthState.expires_at < utcnow()).delete()
        db.add(OAuthState(state_hash=_hash_state(state), owner=owner or "", verifier_enc=encrypt(verifier), scopes=" ".join(scopes),
                          purpose=purpose, expires_at=utcnow() + STATE_TTL))
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes), "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "include_granted_scopes": "true"}
    if purpose == "connect":
        params["prompt"] = "consent"  # Google only returns a refresh token on consent
    if account_hint:
        params["login_hint"] = account_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def _consume_state(state: str, owner: Optional[str], *, purpose: str) -> tuple:
    with session_scope() as db:
        row = db.get(OAuthState, _hash_state(state or ""))
        if row is None or row.expires_at < utcnow():
            if row is not None:
                db.delete(row)
            raise OAuthStateError("estado OAuth invalido ou expirado")
        if row.purpose != purpose:
            raise OAuthStateError("estado OAuth de outro fluxo")
        if purpose == "connect" and row.owner != (owner or ""):
            raise OAuthStateError("estado OAuth pertence a outra sessao")  # CSRF: can't link into someone else's session
        verifier, stored_owner = decrypt(row.verifier_enc), row.owner
        db.delete(row)
    return verifier, stored_owner


async def _exchange(code: str, verifier: str, redirect_uri: str) -> Dict[str, Any]:
    client_id, secret = client_credentials()
    async with http_client() as client:
        resp = await client.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "client_id": client_id, "client_secret": secret,
                                                  "redirect_uri": redirect_uri, "code_verifier": verifier})
    if resp.status_code != 200:
        raise OAuthStateError("o Google recusou o codigo de autorizacao")
    return resp.json()


async def _userinfo(access_token: str) -> Dict[str, Any]:
    async with http_client() as client:
        resp = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    return resp.json()


async def complete_connect(state: str, code: str, redirect_uri: str, owner: Optional[str]) -> Dict[str, Any]:
    """Finish linking a Google account to the logged-in Jarvis user."""
    verifier, _ = _consume_state(state, owner, purpose="connect")
    tokens = await _exchange(code, verifier, redirect_uri)
    info = await _userinfo(tokens["access_token"])
    owner = owner or ""
    with session_scope() as db:
        taken = db.query(OAuthAccount).filter(OAuthAccount.provider == "google", OAuthAccount.subject == info["sub"], OAuthAccount.owner != owner,
                                              OAuthAccount.status != "revoked").first()
        if taken:
            raise OAuthStateError("esta conta Google ja esta vinculada a outro usuario")
        row = db.query(OAuthAccount).filter(OAuthAccount.owner == owner, OAuthAccount.provider == "google", OAuthAccount.subject == info["sub"]).first()
        if row is None:
            row = OAuthAccount(owner=owner, provider="google", subject=info["sub"])
            db.add(row)
        previous = set((row.scopes or "").split())
        row.email, row.scopes = info.get("email", ""), " ".join(sorted(previous | set((tokens.get("scope") or "").split())))
        row.access_enc = encrypt(tokens["access_token"])
        if tokens.get("refresh_token"):  # incremental consent may omit it: keep the old one
            row.refresh_enc = encrypt(tokens["refresh_token"])
        row.expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
        row.status, row.last_error, row.updated_at = "connected", "", utcnow()
        db.flush()
        result = account_dict(row)
    audit.record("google.connect", owner=owner, risk="reversible", target=result["id"], groups=result["groups"])
    return result


async def complete_login(state: str, code: str, redirect_uri: str) -> str:
    """Return the Jarvis username for a Google sign-in. Only for already-linked accounts:
    this never creates a user or replaces an Odysseus account."""
    verifier, _ = _consume_state(state, "", purpose="login")
    tokens = await _exchange(code, verifier, redirect_uri)
    info = await _userinfo(tokens["access_token"])
    if info.get("email_verified") is False:
        raise OAuthStateError("o e-mail desta conta Google nao esta verificado")
    with session_scope() as db:
        row = db.query(OAuthAccount).filter(OAuthAccount.provider == "google", OAuthAccount.subject == info["sub"], OAuthAccount.status != "revoked").first()
        if row is None or not row.owner:
            raise OAuthStateError("nenhuma conta Jarvis vinculada a este Google; entre com senha e vincule nas Integracoes")
        owner = row.owner
    audit.record("google.login", owner=owner, risk="read", actor=owner)
    return owner


def granted_groups(scopes: str) -> List[str]:
    have = set((scopes or "").split())
    return [g for g, needed in SCOPE_GROUPS.items() if needed and set(needed) <= have]


def account_dict(row: OAuthAccount) -> Dict[str, Any]:
    return {"id": row.id, "email": row.email, "status": row.status, "groups": granted_groups(row.scopes), "last_error": row.last_error,
            "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None}


def list_accounts(owner: Optional[str] = "") -> List[Dict[str, Any]]:
    with session_scope() as db:
        return [account_dict(r) for r in db.query(OAuthAccount).filter(OAuthAccount.owner == (owner or ""), OAuthAccount.provider == "google", OAuthAccount.status != "revoked").all()]


def default_account(owner: Optional[str], group: str) -> str:
    """First connected account holding `group`. Raises when none does."""
    accounts = list_accounts(owner)
    for account in accounts:
        if group in account["groups"]:
            if account["status"] != "connected":
                raise ReconnectRequired(account["id"])
            return account["id"]
    raise MissingScope(group)


def _mark_reauth(account_id: str, reason: str) -> None:
    with session_scope() as db:
        row = db.get(OAuthAccount, account_id)
        if row:
            row.status, row.last_error, row.updated_at = "reauth_required", redact_text(reason)[:200], utcnow()
            owner = row.owner
    audit.record("google.reauth_required", owner=owner, risk="read", outcome="error", target=account_id)


async def access_token(account_id: str, owner: Optional[str], *, force_refresh: bool = False) -> str:
    with session_scope() as db:
        row = db.query(OAuthAccount).filter(OAuthAccount.id == account_id, OAuthAccount.owner == (owner or "")).first()
        if row is None:
            raise LookupError("conta Google nao encontrada")
        if row.status != "connected":
            raise ReconnectRequired(account_id)
        fresh = row.expires_at and row.expires_at > utcnow() + timedelta(seconds=60)
        if fresh and not force_refresh:
            return decrypt(row.access_enc)
        refresh = decrypt(row.refresh_enc) if row.refresh_enc else ""
    if not refresh:
        _mark_reauth(account_id, "sem refresh token")
        raise ReconnectRequired(account_id)
    client_id, secret = client_credentials()
    async with http_client() as client:
        resp = await client.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id, "client_secret": secret})
    if resp.status_code in (400, 401):
        error = ""
        try:
            error = resp.json().get("error", "")
        except ValueError:
            pass
        if error in ("invalid_grant", "unauthorized_client", "invalid_client") or resp.status_code == 401:
            _mark_reauth(account_id, error or "token recusado")
            raise ReconnectRequired(account_id)
    resp.raise_for_status()
    body = resp.json()
    with session_scope() as db:
        row = db.get(OAuthAccount, account_id)
        row.access_enc, row.updated_at = encrypt(body["access_token"]), utcnow()
        row.expires_at = utcnow() + timedelta(seconds=int(body.get("expires_in", 3600)))
        if body.get("refresh_token"):
            row.refresh_enc = encrypt(body["refresh_token"])
    return body["access_token"]


async def google_request(method: str, url: str, *, account_id: str, owner: Optional[str], **kwargs: Any) -> httpx.Response:
    """Authenticated call: one forced refresh on 401, then ask the user to reconnect."""
    token = await access_token(account_id, owner)
    headers = kwargs.pop("headers", {})
    async with http_client() as client:
        resp = await client.request(method, url, headers={"Authorization": f"Bearer {token}", **headers}, **kwargs)
        if resp.status_code == 401:
            token = await access_token(account_id, owner, force_refresh=True)
            resp = await client.request(method, url, headers={"Authorization": f"Bearer {token}", **headers}, **kwargs)
            if resp.status_code == 401:
                _mark_reauth(account_id, "401 apos renovar o token")
                raise ReconnectRequired(account_id)
    return resp


async def revoke(account_id: str, owner: Optional[str]) -> Dict[str, Any]:
    """Revoke at Google and always wipe local tokens, even if Google can't be reached."""
    with session_scope() as db:
        row = db.query(OAuthAccount).filter(OAuthAccount.id == account_id, OAuthAccount.owner == (owner or "")).first()
        if row is None:
            raise LookupError("conta Google nao encontrada")
        token = decrypt(row.refresh_enc or row.access_enc) if (row.refresh_enc or row.access_enc) else ""
        row.access_enc, row.refresh_enc, row.status, row.expires_at, row.updated_at = "", "", "revoked", None, utcnow()
    remote_ok = False
    if token:
        try:
            async with http_client() as client:
                resp = await client.post(REVOKE_URL, data={"token": token})
            remote_ok = resp.status_code == 200
        except httpx.HTTPError:
            pass
    audit.record("google.revoke", owner=owner, risk="reversible", target=account_id, remote_ok=remote_ok)
    return {"revoked": True, "remote_ok": remote_ok}
