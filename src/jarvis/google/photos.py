"""Google Photos through the Picker API: the user chooses, Jarvis sees only that.

Since March 2025 the Library API no longer gives apps access to a whole library,
so full-library search isn't offered. Picked bytes stay in memory for analysis and
aren't cached; base URLs expire anyway. Visual descriptions are kept only as
untrusted hypotheses the user can review or delete.
"""
from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.jarvis import audit, memory
from src.jarvis.google.calendar import GoogleApiError
from src.jarvis.google.oauth import default_account, google_request

API = "https://photospicker.googleapis.com/v1"


def _check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Photos respondeu {resp.status_code} em {what}")
    return resp.json() if resp.content else {}


async def create_session(owner: str, account_id: Optional[str] = None) -> Dict[str, Any]:
    account_id = account_id or default_account(owner, "photos")
    body = _check(await google_request("POST", f"{API}/sessions", account_id=account_id, owner=owner, json={}), "criar sessão")
    audit.record("photos.session", owner=owner)
    return {"session_id": body.get("id", ""), "picker_uri": body.get("pickerUri", ""), "ready": bool(body.get("mediaItemsSet")), "poll": body.get("pollingConfig", {})}


async def session_status(owner: str, session_id: str, account_id: Optional[str] = None) -> Dict[str, Any]:
    account_id = account_id or default_account(owner, "photos")
    body = _check(await google_request("GET", f"{API}/sessions/{session_id}", account_id=account_id, owner=owner), "ler sessão")
    return {"session_id": session_id, "ready": bool(body.get("mediaItemsSet")), "poll": body.get("pollingConfig", {})}


async def list_items(owner: str, session_id: str, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    account_id = account_id or default_account(owner, "photos")
    items: List[Dict[str, Any]] = []
    page = ""
    while True:
        params: Dict[str, Any] = {"sessionId": session_id, "pageSize": 50}
        if page:
            params["pageToken"] = page
        body = _check(await google_request("GET", f"{API}/mediaItems", account_id=account_id, owner=owner, params=params), "listar itens")
        for item in body.get("mediaItems", []):
            media = item.get("mediaFile") or {}
            items.append({"id": item.get("id", ""), "type": item.get("type", ""), "mime": media.get("mimeType", ""), "filename": media.get("filename", ""),
                          "base_url": media.get("baseUrl", ""), "created": item.get("createTime", "")})
        page = body.get("nextPageToken", "")
        if not page:
            return items


async def fetch_image(owner: str, session_id: str, item_id: str, *, size: int = 512, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Bytes of one picked photo. The URL comes from Google's list for this session, never from a caller."""
    account_id = account_id or default_account(owner, "photos")
    item = next((i for i in await list_items(owner, session_id, account_id) if i["id"] == item_id), None)
    if item is None:
        raise LookupError("foto não encontrada nesta sessão")
    if not item.get("mime", "").startswith("image/"):
        raise ValueError("apenas fotos têm miniatura")
    size = max(64, min(int(size), 2048))
    resp = await google_request("GET", f"{item['base_url']}=w{size}-h{size}", account_id=account_id, owner=owner)
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Photos respondeu {resp.status_code} ao baixar a foto")
    return {"content": resp.content, "mime": item["mime"]}


async def delete_session(owner: str, session_id: str, account_id: Optional[str] = None) -> None:
    account_id = account_id or default_account(owner, "photos")
    await google_request("DELETE", f"{API}/sessions/{session_id}", account_id=account_id, owner=owner)


async def analyze_item(owner: str, item: Dict[str, Any], describe: Callable[[bytes, str], Awaitable[str]], *, remember: bool = False, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Fetch one photo (baseUrl needs the same token) and describe it with a vision model."""
    account_id = account_id or default_account(owner, "photos")
    if item.get("type") not in ("PHOTO", "TYPE_UNSPECIFIED") and not item.get("mime", "").startswith("image/"):
        raise ValueError("apenas fotos podem ser analisadas")
    resp = await google_request("GET", f"{item['base_url']}=w1024-h1024", account_id=account_id, owner=owner)
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Photos respondeu {resp.status_code} ao baixar a foto")
    description = await describe(resp.content, item.get("mime") or "image/jpeg")
    if remember:
        memory.remember(f"Foto {item.get('filename', '')}: {description}", owner=owner, kind="episodic", category="foto", source_type="document", source_ref=f"gphotos:{item.get('id', '')}")
    return {"id": item.get("id", ""), "description": description}


def vision_request(image: bytes, mime: str, prompt: str = "Descreva a imagem em português, listando os objetos visiveis.") -> List[Dict[str, Any]]:
    data = base64.b64encode(image).decode()
    return [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}]
