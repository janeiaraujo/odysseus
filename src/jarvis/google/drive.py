"""Google Drive: browse anything accessible, but index ONLY folders the user allowed.

Indexed text is untrusted document data. Chunks live in SQLite (source of truth)
with vectors in ChromaDB, and sync uses the Changes API to avoid re-downloading.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

from src.jarvis import audit, config, memory
from src.jarvis.db import session_scope, utcnow
from src.jarvis.google.calendar import GoogleApiError, _cursor, _save_cursor
from src.jarvis.google.oauth import default_account, google_request
from src.jarvis.models import DocChunk, IndexedFile

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/drive/v3"
EXPORTS = {"application/vnd.google-apps.document": "text/plain", "application/vnd.google-apps.spreadsheet": "text/csv", "application/vnd.google-apps.presentation": "text/plain"}
TEXT_MIMES = ("text/", "application/json", "application/csv")
FIELDS = "id,name,mimeType,md5Checksum,modifiedTime,parents,size,trashed"
MAX_BYTES = 20_000_000
CHUNK_CHARS = 1500
_doc_vectors: Any = None


def _check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Drive respondeu {resp.status_code} em {what}")
    return resp.json()


async def list_files(owner: str, *, folder_id: str = "", query: str = "", page_size: int = 50, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    account_id = account_id or default_account(owner, "drive")
    clauses = ["trashed = false"]
    if folder_id:
        clauses.append(f"'{folder_id.replace(chr(39), '')}' in parents")
    if query:
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"(name contains '{safe}' or fullText contains '{safe}')")
    body = _check(await google_request("GET", f"{API}/files", account_id=account_id, owner=owner, params={"q": " and ".join(clauses), "pageSize": min(page_size, 200), "fields": f"files({FIELDS})"}), "listar arquivos")
    return body.get("files", [])


async def read_text(owner: str, file: Dict[str, Any], account_id: Optional[str] = None) -> str:
    """Text of one file: Docs exported, PDFs parsed, text as-is. Oversized files are refused."""
    account_id = account_id or default_account(owner, "drive")
    mime, file_id = file.get("mimeType", ""), file["id"]
    if mime in EXPORTS:
        resp = await google_request("GET", f"{API}/files/{file_id}/export", account_id=account_id, owner=owner, params={"mimeType": EXPORTS[mime]})
    elif mime == "application/pdf" or mime.startswith(TEXT_MIMES):
        if int(file.get("size") or 0) > MAX_BYTES:
            raise GoogleApiError("arquivo grande demais para indexar")
        resp = await google_request("GET", f"{API}/files/{file_id}", account_id=account_id, owner=owner, params={"alt": "media"})
    else:
        return ""
    if resp.status_code >= 400:
        raise GoogleApiError(f"Google Drive respondeu {resp.status_code} ao ler arquivo")
    if mime == "application/pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(resp.content)).pages)
    return resp.text


def allowed_folders(owner: str) -> List[str]:
    return list((config.get("google.drive_allowed_folders", {}) or {}).get(owner or "default", []))


def set_allowed_folders(owner: str, folder_ids: List[str]) -> None:
    current = dict(config.get("google.drive_allowed_folders", {}) or {})
    current[owner or "default"] = [str(f) for f in folder_ids]
    config.update_config({"google": {"drive_allowed_folders": current}})
    audit.record("drive.allowlist", owner=owner, risk="reversible", folders=len(folder_ids))


def set_doc_vectors(store: Any) -> None:
    global _doc_vectors
    _doc_vectors = store


def _vectors() -> Any:
    global _doc_vectors
    if _doc_vectors is None:
        try:
            from src.constants import MEMORY_VECTORS_DIR
            from src.memory_vector import MemoryVectorStore

            class _DocVectors(MemoryVectorStore):
                COLLECTION_NAME = "jarvis_docs"
            store = _DocVectors(MEMORY_VECTORS_DIR)
            _doc_vectors = store if store.healthy else False
        except Exception as exc:
            logger.info("doc vectors unavailable: %s", exc)
            _doc_vectors = False
    return _doc_vectors or None


def _chunks(text: str) -> List[str]:
    text = text.strip()
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS - 150)] if text else []


def _store_file(owner: str, file: Dict[str, Any], text: str) -> int:
    pieces = _chunks(text)
    store = _vectors()
    with session_scope() as db:
        row = db.query(IndexedFile).filter(IndexedFile.owner == owner, IndexedFile.provider == "gdrive", IndexedFile.file_id == file["id"]).first()
        if row is None:
            row = IndexedFile(owner=owner, provider="gdrive", file_id=file["id"])
            db.add(row)
        row.name, row.md5, row.modified_time, row.indexed_at = file.get("name", ""), file.get("md5Checksum", ""), file.get("modifiedTime", ""), utcnow()
        db.flush()
        old = [c.id for c in db.query(DocChunk.id).filter(DocChunk.file_row_id == row.id).all()]
        db.query(DocChunk).filter(DocChunk.file_row_id == row.id).delete()
        for n, piece in enumerate(pieces):
            db.add(DocChunk(id=f"{row.id}:{n}", file_row_id=row.id, owner=owner, n=n, text=piece))
        row_id = row.id
    if store:
        for chunk_id in old:
            store.remove(chunk_id)
        for n, piece in enumerate(pieces):
            store.add(f"{row_id}:{n}", piece)
    return len(pieces)


def _forget_file(owner: str, file_id: str) -> None:
    store = _vectors()
    with session_scope() as db:
        row = db.query(IndexedFile).filter(IndexedFile.owner == owner, IndexedFile.provider == "gdrive", IndexedFile.file_id == file_id).first()
        if row is None:
            return
        ids = [c.id for c in db.query(DocChunk.id).filter(DocChunk.file_row_id == row.id).all()]
        db.query(DocChunk).filter(DocChunk.file_row_id == row.id).delete()
        db.delete(row)
    if store:
        for chunk_id in ids:
            store.remove(chunk_id)


async def index_allowed(owner: str, account_id: Optional[str] = None) -> Dict[str, int]:
    """Initial pass over allowed folders. Unchanged files aren't downloaded again."""
    account_id = account_id or default_account(owner, "drive")
    folders = allowed_folders(owner)
    stats = {"indexed": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    if not folders:
        return stats
    start = _check(await google_request("GET", f"{API}/changes/startPageToken", account_id=account_id, owner=owner), "ler token de mudancas").get("startPageToken", "")
    for folder in folders:
        for file in await list_files(owner, folder_id=folder, page_size=200, account_id=account_id):
            await _index_one(owner, file, account_id, stats)
    _save_cursor(owner, f"drive:{account_id}", start)
    return stats


async def _index_one(owner: str, file: Dict[str, Any], account_id: str, stats: Dict[str, int]) -> None:
    with session_scope() as db:
        row = db.query(IndexedFile).filter(IndexedFile.owner == owner, IndexedFile.provider == "gdrive", IndexedFile.file_id == file["id"]).first()
        if row is not None and row.modified_time == file.get("modifiedTime") and row.indexed_at:
            stats["unchanged"] += 1
            return
    try:
        text = await read_text(owner, file, account_id)
    except Exception as exc:
        logger.info("drive read failed: %s", type(exc).__name__)
        stats["errors"] += 1
        return
    if not text.strip():
        stats["skipped"] += 1
        return
    _store_file(owner, file, text)
    stats["indexed"] += 1


async def sync_changes(owner: str, account_id: Optional[str] = None) -> Dict[str, int]:
    """Incremental: apply changes, ignoring anything outside the allowed folders."""
    account_id = account_id or default_account(owner, "drive")
    resource = f"drive:{account_id}"
    token = _cursor(owner, resource)
    if not token:
        return await index_allowed(owner, account_id)
    allowed = set(allowed_folders(owner))
    stats = {"indexed": 0, "unchanged": 0, "skipped": 0, "errors": 0, "removed": 0}
    while token:
        body = _check(await google_request("GET", f"{API}/changes", account_id=account_id, owner=owner, params={"pageToken": token, "fields": f"newStartPageToken,nextPageToken,changes(fileId,removed,file({FIELDS}))"}), "ler mudancas")
        for change in body.get("changes", []):
            file = change.get("file") or {}
            if change.get("removed") or file.get("trashed") or not (set(file.get("parents", [])) & allowed):
                _forget_file(owner, change.get("fileId", ""))  # also covers moved out of an allowed folder
                stats["removed"] += 1
                continue
            await _index_one(owner, file, account_id, stats)
        if body.get("newStartPageToken"):
            _save_cursor(owner, resource, body["newStartPageToken"])
        token = body.get("nextPageToken", "")
    return stats


def search_documents(query: str, *, owner: str, k: int = 5) -> List[Dict[str, Any]]:
    """Hybrid over indexed chunks, owner-scoped in SQL. Results are untrusted document data."""
    tokens = list(memory._tokens(query))[:6]
    scores: Dict[str, float] = {}
    store = _vectors()
    if store:
        try:
            for hit in store.search(query, k=k * 4):
                scores[hit["memory_id"]] = hit.get("score", 0)
        except Exception as exc:
            logger.info("doc vector search failed: %s", exc)
    with session_scope() as db:
        q = db.query(DocChunk, IndexedFile.name).join(IndexedFile, DocChunk.file_row_id == IndexedFile.id).filter(DocChunk.owner == owner)
        rows = q.filter(DocChunk.id.in_(list(scores))).all() if scores else []
        if tokens:
            from sqlalchemy import or_
            rows += q.filter(or_(*[DocChunk.text.ilike(f"%{t}%") for t in tokens])).limit(k * 4).all()
        out: Dict[str, Dict[str, Any]] = {}
        for chunk, name in rows:
            score = scores.get(chunk.id, 0) + memory.similarity(chunk.text, query)
            if chunk.id not in out or out[chunk.id]["score"] < score:
                out[chunk.id] = {"file": name, "text": chunk.text[:600], "score": round(score, 4)}
    return sorted(out.values(), key=lambda r: r["score"], reverse=True)[:k]


def find_duplicates(owner: str) -> List[Dict[str, Any]]:
    """Same md5 among indexed files; native Google Docs have no checksum and can't match."""
    with session_scope() as db:
        groups: Dict[str, List[str]] = {}
        for row in db.query(IndexedFile).filter(IndexedFile.owner == owner, IndexedFile.md5 != "").all():
            groups.setdefault(row.md5, []).append(row.name)
    return [{"md5": md5, "files": names} for md5, names in groups.items() if len(names) > 1]
