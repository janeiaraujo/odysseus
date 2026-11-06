"""Jarvis long-term memory, independent of any model.

SQLite (data/jarvis/jarvis.db) is the source of truth; FTS5 and ChromaDB are
rebuildable indexes. Untrusted sources (email, web, documents) are stored as
hypotheses and can never overwrite or contradict a user-confirmed fact.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import or_, text as sql_text

from src.jarvis import audit
from src.jarvis.db import get_engine, session_scope, utcnow
from src.jarvis.models import MemoryItem, MemoryLink, MemoryVersion
from src.jarvis.redact import redact_text

logger = logging.getLogger(__name__)

KINDS = ("episodic", "semantic", "procedural", "operational")
UNTRUSTED_SOURCES = {"email", "web", "document", "attachment", "tool"}
DUPLICATE_THRESHOLD = 0.82
CONFLICT_OVERLAP = 0.5
_STOP = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no", "na", "nos", "nas", "um", "uma", "que", "para",
         "por", "pela", "pelo", "com", "meu", "minha", "the", "of", "to", "and", "is", "my", "eu", "se", "ao", "aos", "é"}
_NEGATION = re.compile(r"\b(nao|nunca|jamais|not|never|no)\b")
_SUBJECT = re.compile(r"^\s*(?:o |a )?(?:meu|minha|my)\s+([\w ]{2,40}?)\s+(?:e|eh|sera|is|fica|=)\s+(.+)$")
_VALUE_TOKEN = re.compile(r"\d[\d:/.-]*")

_vector_lock = threading.Lock()
_vector_store: Any = None
_vector_failed = False
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-memory")


def normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in stripped if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:/.-]", " ", stripped)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _tokens(text: str) -> set:
    return {t.strip(".:/-") for t in normalize(text).split() if t.strip(".:/-") and t.strip(".:/-") not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _contradicts(old: str, new: str) -> bool:
    """Conservative: same subject with a different value, opposite polarity, or different numbers."""
    n_old, n_new = normalize(old), normalize(new)
    subj_old, subj_new = _SUBJECT.match(n_old), _SUBJECT.match(n_new)
    if subj_old and subj_new and subj_old.group(1).strip() == subj_new.group(1).strip():
        return subj_old.group(2).strip() != subj_new.group(2).strip()
    overlap = similarity(old, new)
    if overlap < CONFLICT_OVERLAP:
        return False
    if bool(_NEGATION.search(n_old)) != bool(_NEGATION.search(n_new)):
        return True
    return set(_VALUE_TOKEN.findall(n_old)) != set(_VALUE_TOKEN.findall(n_new))


# --- search indexes ------------------------------------------------------------

def _ensure_fts() -> bool:
    try:
        with get_engine().begin() as conn:
            conn.execute(sql_text("CREATE VIRTUAL TABLE IF NOT EXISTS jarvis_memory_fts USING fts5(id UNINDEXED, body, tokenize='unicode61 remove_diacritics 2')"))
        return True
    except Exception as exc:  # non-SQLite backend or FTS5 not compiled in
        logger.info("FTS5 unavailable, using LIKE search: %s", exc)
        return False


def _fts_put(item_id: str, text: str) -> None:
    if not _ensure_fts():
        return
    with get_engine().begin() as conn:
        conn.execute(sql_text("DELETE FROM jarvis_memory_fts WHERE id = :id"), {"id": item_id})
        conn.execute(sql_text("INSERT INTO jarvis_memory_fts (id, body) VALUES (:id, :body)"), {"id": item_id, "body": normalize(text)})


def _fts_del(item_id: str) -> None:
    if _ensure_fts():
        with get_engine().begin() as conn:
            conn.execute(sql_text("DELETE FROM jarvis_memory_fts WHERE id = :id"), {"id": item_id})


def _fts_query(query: str, limit: int) -> List[str]:
    tokens = [t for t in _tokens(query) if re.fullmatch(r"\w+", t)]
    if not tokens or not _ensure_fts():
        return []
    expr = " OR ".join(f'"{t}"' for t in tokens[:16])
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(sql_text("SELECT id FROM jarvis_memory_fts WHERE jarvis_memory_fts MATCH :q ORDER BY bm25(jarvis_memory_fts) LIMIT :n"),
                                {"q": expr, "n": limit}).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.debug("fts query failed: %s", exc)
        return []


def set_vector_store(store: Any) -> None:
    """Install a vector backend (tests, or a replacement for ChromaDB)."""
    global _vector_store, _vector_failed
    with _vector_lock:
        _vector_store, _vector_failed = store, False


def _vectors() -> Any:
    global _vector_store, _vector_failed
    with _vector_lock:
        if _vector_store is None and not _vector_failed:
            try:
                from src.constants import MEMORY_VECTORS_DIR
                from src.memory_vector import MemoryVectorStore

                class _JarvisVectors(MemoryVectorStore):
                    COLLECTION_NAME = "jarvis_memories"

                store = _JarvisVectors(MEMORY_VECTORS_DIR)
                if store.healthy:
                    _vector_store = store
                else:
                    _vector_failed = True
            except Exception as exc:
                logger.warning("Jarvis vector store unavailable, using text search: %s", redact_text(exc))
                _vector_failed = True
        return _vector_store


def _vector_add(item_id: str, text: str) -> None:
    def job() -> None:
        store = _vectors()
        if store:
            try:
                store.remove(item_id)
                store.add(item_id, text)
            except Exception as exc:
                logger.warning("vector index failed for %s: %s", item_id, redact_text(exc))
    _pool.submit(job)


def _vector_remove(item_id: str) -> None:
    def job() -> None:
        store = _vectors()
        if store:
            try:
                store.remove(item_id)
            except Exception as exc:
                logger.warning("vector removal failed for %s: %s", item_id, redact_text(exc))
    _pool.submit(job)


def flush_index(timeout: float = 30) -> None:
    """Wait for queued index work; a no-op job drains the single worker."""
    _pool.submit(lambda: None).result(timeout=timeout)


# --- records -------------------------------------------------------------------

def to_dict(row: MemoryItem) -> Dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "category": row.category, "text": row.text, "source_type": row.source_type,
            "source_ref": row.source_ref, "trust": row.trust, "status": row.status, "confidence": row.confidence,
            "priority": row.priority, "valid_until": row.valid_until.isoformat() + "Z" if row.valid_until else None,
            "version": row.version, "uses": row.uses, "created_at": row.created_at.isoformat() + "Z",
            "updated_at": row.updated_at.isoformat() + "Z"}


def _active(q, owner: Optional[str]):
    q = q.filter(MemoryItem.deleted_at.is_(None), MemoryItem.status != "superseded",
                 or_(MemoryItem.valid_until.is_(None), MemoryItem.valid_until > utcnow()))
    return q.filter(MemoryItem.owner == (owner or "")) if owner is not None else q


def remember(text: str, *, owner: Optional[str] = "", kind: str = "semantic", category: str = "fact", source_type: str = "user",
             source_ref: str = "", status: Optional[str] = None, confidence: Optional[float] = None, priority: int = 0,
             valid_until: Optional[datetime] = None) -> Dict[str, Any]:
    """Store a memory. Returns {"action": created|duplicate|conflict, "item", "conflicts"}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("memória vazia")
    if kind not in KINDS:
        raise ValueError(f"tipo de memória inválido: {kind}")
    owner = owner or ""
    untrusted = source_type in UNTRUSTED_SOURCES
    trust = "untrusted" if untrusted else ("user" if source_type in ("user", "import") else "derived")
    if untrusted:  # A document cannot promote itself to fact, whatever the caller says.
        status, confidence = "hypothesis", min(confidence if confidence is not None else 0.4, 0.4)
    status = status or "fact"
    confidence = 1.0 if confidence is None else max(0.0, min(float(confidence), 1.0))
    digest, conflicts, created = content_hash(text), [], True
    with session_scope() as db:
        existing = _active(db.query(MemoryItem), owner).filter(MemoryItem.norm_hash == digest).first()
        # An import is what the user chose to keep: only exact repeats are merged,
        # and comparing it against everything would flood them with false conflicts.
        judged = source_type != "import"
        if existing is None and judged:
            for row in _active(db.query(MemoryItem), owner).filter(MemoryItem.kind == kind).order_by(MemoryItem.updated_at.desc()).limit(500):
                if similarity(row.text, text) >= DUPLICATE_THRESHOLD and not _contradicts(row.text, text):
                    existing = row
                    break
        if existing is not None:
            if trust == "user" and existing.trust == "untrusted":  # the user just confirmed it
                existing.trust, existing.status, existing.confidence = "user", "fact", max(confidence, existing.confidence)
            else:
                existing.confidence = min(1.0, (existing.confidence or 0) + 0.05)
            existing.uses, existing.updated_at = (existing.uses or 0) + 1, utcnow()
            db.flush()
            return {"action": "duplicate", "item": to_dict(existing), "conflicts": []}
        rows = _active(db.query(MemoryItem), owner).filter(MemoryItem.kind == kind, MemoryItem.status.in_(("fact", "conflicted"))).limit(500).all()
        contradicted = [r for r in rows if _contradicts(r.text, text)] if judged else []
        row = MemoryItem(owner=owner, kind=kind, category=category, text=text, norm_hash=digest, source_type=source_type,
                         source_ref=redact_text(source_ref)[:300], trust=trust, status=status, confidence=confidence,
                         priority=priority, valid_until=valid_until)
        db.add(row)
        db.flush()
        for other in contradicted:
            db.add(MemoryLink(src_id=row.id, dst_id=other.id, relation="conflicts"))
            conflicts.append(to_dict(other))
            if not untrusted:  # Untrusted text never degrades a fact; only flags itself.
                other.status = "conflicted"
        if contradicted and status == "fact":
            row.status = "conflicted"
        db.add(MemoryVersion(memory_id=row.id, version=1, text=text, reason="criacao"))
        db.flush()
        item = to_dict(row)
    _fts_put(item["id"], text)
    _vector_add(item["id"], text)
    if judged:
        audit.record("memory.remember", owner=owner, risk="reversible", target=item["id"], kind=kind, source=source_type,
                     status=item["status"], conflicts=len(conflicts))
    _replicate(item, owner)
    return {"action": "conflict" if conflicts else "created", "item": item, "conflicts": conflicts}


def _replicate(item: Dict[str, Any], owner: str, deleted: bool = False) -> None:
    try:
        from src.jarvis.replication import enqueue_memory
        enqueue_memory(item, owner, deleted=deleted)
    except Exception as exc:
        logger.debug("replication enqueue skipped: %s", redact_text(exc))


def _owned(db, memory_id: str, owner: Optional[str]) -> MemoryItem:
    q = db.query(MemoryItem).filter(MemoryItem.id == memory_id, MemoryItem.deleted_at.is_(None))
    if owner is not None:
        q = q.filter(MemoryItem.owner == (owner or ""))
    row = q.first()
    if row is None:
        raise LookupError("memória não encontrada")
    return row


def get(memory_id: str, owner: Optional[str] = "") -> Dict[str, Any]:
    with session_scope() as db:
        return to_dict(_owned(db, memory_id, owner))


def correct(memory_id: str, new_text: str, *, owner: Optional[str] = "", reason: str = "correção do usuário") -> Dict[str, Any]:
    """User correction: new version, status back to fact, conflict links cleared."""
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("memória vazia")
    with session_scope() as db:
        row = _owned(db, memory_id, owner)
        row.version, row.text, row.norm_hash = (row.version or 1) + 1, new_text, content_hash(new_text)
        row.status, row.trust, row.confidence, row.updated_at = "fact", "user", 1.0, utcnow()
        db.add(MemoryVersion(memory_id=row.id, version=row.version, text=new_text, reason=reason[:200]))
        db.query(MemoryLink).filter(or_(MemoryLink.src_id == row.id, MemoryLink.dst_id == row.id), MemoryLink.relation == "conflicts").delete(synchronize_session=False)
        db.flush()
        item = to_dict(row)
    _fts_put(memory_id, new_text)
    _vector_add(memory_id, new_text)
    audit.record("memory.correct", owner=owner, risk="reversible", target=memory_id, version=item["version"])
    _replicate(item, owner or "")
    return item


def resolve_conflict(keep_id: str, drop_id: str, *, owner: Optional[str] = "") -> Dict[str, Any]:
    with session_scope() as db:
        keep, drop = _owned(db, keep_id, owner), _owned(db, drop_id, owner)
        keep.status, keep.confidence, keep.updated_at = "fact", 1.0, utcnow()
        drop.status, drop.updated_at = "superseded", utcnow()
        db.add(MemoryLink(src_id=keep.id, dst_id=drop.id, relation="corrects"))
        db.query(MemoryLink).filter(MemoryLink.relation == "conflicts", or_(
            (MemoryLink.src_id == keep.id) & (MemoryLink.dst_id == drop.id),
            (MemoryLink.src_id == drop.id) & (MemoryLink.dst_id == keep.id))).delete(synchronize_session=False)
        db.flush()
        item = to_dict(keep)
    _fts_del(drop_id)
    _vector_remove(drop_id)
    audit.record("memory.resolve_conflict", owner=owner, risk="reversible", target=keep_id, dropped=drop_id)
    return item


def conflicts(owner: Optional[str] = "") -> List[Dict[str, Any]]:
    """Pairs waiting for the user to decide which is right."""
    with session_scope() as db:
        links = db.query(MemoryLink).filter(MemoryLink.relation == "conflicts").limit(200).all()
        out = []
        for link in links:
            a, b = db.get(MemoryItem, link.src_id), db.get(MemoryItem, link.dst_id)
            if not a or not b or a.deleted_at or b.deleted_at or a.owner != (owner or "") or b.owner != (owner or ""):
                continue
            if a.status == "superseded" or b.status == "superseded":
                continue
            out.append({"a": to_dict(a), "b": to_dict(b)})
        return out


def versions(memory_id: str, owner: Optional[str] = "") -> List[Dict[str, Any]]:
    with session_scope() as db:
        _owned(db, memory_id, owner)
        rows = db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory_id).order_by(MemoryVersion.version).all()
        return [{"version": r.version, "text": r.text, "reason": r.reason, "changed_at": r.changed_at.isoformat() + "Z"} for r in rows]


def delete(memory_id: str, *, owner: Optional[str] = "", purge: bool = False) -> None:
    """Soft delete by default; purge wipes text and version history (privacy erasure)."""
    with session_scope() as db:
        row = _owned(db, memory_id, owner)
        row_dict = to_dict(row)
        row.deleted_at, row.updated_at = utcnow(), utcnow()
        if purge:
            db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory_id).delete()
            db.query(MemoryLink).filter(or_(MemoryLink.src_id == memory_id, MemoryLink.dst_id == memory_id)).delete(synchronize_session=False)
            db.delete(row)
    _fts_del(memory_id)
    _vector_remove(memory_id)
    audit.record("memory.purge" if purge else "memory.delete", owner=owner, risk="destructive" if purge else "reversible", target=memory_id)
    _replicate(row_dict, owner or "", deleted=True)


def list_items(owner: Optional[str] = "", *, kind: Optional[str] = None, status: Optional[str] = None, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = _active(db.query(MemoryItem), owner)
        if kind:
            q = q.filter(MemoryItem.kind == kind)
        if status:
            q = q.filter(MemoryItem.status == status)
        return [to_dict(r) for r in q.order_by(MemoryItem.updated_at.desc()).offset(offset).limit(min(limit, 500)).all()]


def search(query: str, *, owner: Optional[str] = "", k: int = 8, kinds: Optional[Sequence[str]] = None,
           include_hypotheses: bool = False, touch: bool = False) -> List[Dict[str, Any]]:
    """Hybrid retrieval: reciprocal-rank fusion of FTS5 and vector results."""
    query = (query or "").strip()
    if not query:
        return []
    fetch = max(k * 4, 20)
    scores: Dict[str, float] = {}
    for rank_pos, item_id in enumerate(_fts_query(query, fetch)):
        scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (60 + rank_pos)
    store = _vectors()
    if store:
        try:
            for rank_pos, hit in enumerate(store.search(query, k=fetch)):
                if hit.get("score", 0) >= 0.25:
                    scores[hit["memory_id"]] = scores.get(hit["memory_id"], 0.0) + 1.0 / (60 + rank_pos)
        except Exception as exc:
            logger.warning("vector search failed, using text only: %s", redact_text(exc))
    with session_scope() as db:
        q = _active(db.query(MemoryItem), owner)  # owner filter applied in SQL, after index lookup
        if kinds:
            q = q.filter(MemoryItem.kind.in_(list(kinds)))
        if not include_hypotheses:
            q = q.filter(MemoryItem.status != "hypothesis")
        if scores:
            rows = q.filter(MemoryItem.id.in_(list(scores))).all()
        else:  # FTS unavailable or no hit: LIKE keeps the feature working
            tokens = list(_tokens(query))[:6]
            rows = q.filter(or_(*[MemoryItem.text.ilike(f"%{t}%") for t in tokens])).limit(fetch).all() if tokens else []
        for row in rows:
            scores.setdefault(row.id, similarity(row.text, query) / 60)
        rows.sort(key=lambda r: scores[r.id] * (1 + 0.1 * (r.priority or 0)) * (0.5 + 0.5 * (r.confidence or 0)), reverse=True)
        rows = rows[:k]
        if touch:
            for row in rows:
                row.uses, row.last_used_at = (row.uses or 0) + 1, utcnow()
            db.flush()
        return [{**to_dict(r), "score": round(scores[r.id], 5)} for r in rows]


def core_facts(owner: Optional[str] = "", limit: int = 30) -> List[Dict[str, Any]]:
    """Who the user is: included in every answer, whatever the question. A question like
    "previsão do tempo" shares no words with "moro no Rio", and search alone misses it."""
    with session_scope() as db:
        rows = (_active(db.query(MemoryItem), owner).filter(MemoryItem.category == "identity", MemoryItem.status.in_(("fact", "conflicted")))
                .order_by(MemoryItem.priority.desc(), MemoryItem.created_at).limit(limit).all())
        return [to_dict(r) for r in rows]


def context_for_chat(message: str, owner: Optional[str] = "", limit: int = 6) -> str:
    """Compact block for the prompt. Caller must wrap it as untrusted context."""
    items = core_facts(owner) + search(message, owner=owner, k=limit, touch=True)
    lines, seen = [], set()
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        tag = "em conflito" if item["status"] == "conflicted" else item["kind"]
        lines.append(f"- [{tag}] {item['text']}")
    return "\n".join(lines)


def consolidate(owner: Optional[str] = "") -> Dict[str, int]:
    """Merge near-duplicates into the oldest entry, keeping the history links."""
    merged, removed_ids = 0, []
    with session_scope() as db:
        rows = _active(db.query(MemoryItem), owner).filter(MemoryItem.status == "fact").order_by(MemoryItem.created_at).limit(2000).all()
        kept: List[MemoryItem] = []
        for row in rows:
            match = next((k for k in kept if k.kind == row.kind and similarity(k.text, row.text) >= DUPLICATE_THRESHOLD and not _contradicts(k.text, row.text)), None)
            if match is None:
                kept.append(row)
                continue
            match.uses = (match.uses or 0) + (row.uses or 0) + 1
            match.confidence = max(match.confidence or 0, row.confidence or 0)
            row.status, row.updated_at = "superseded", utcnow()
            db.add(MemoryLink(src_id=match.id, dst_id=row.id, relation="consolidates"))
            removed_ids.append(row.id)
            merged += 1
    for item_id in removed_ids:
        _fts_del(item_id)
        _vector_remove(item_id)
    if merged:
        audit.record("memory.consolidate", owner=owner, risk="reversible", merged=merged)
    return {"merged": merged}


def reindex(owner: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild FTS and vectors from SQLite, the source of truth."""
    with session_scope() as db:
        rows = [(r.id, r.text) for r in _active(db.query(MemoryItem), owner).all()]
    if _ensure_fts() and owner is None:
        with get_engine().begin() as conn:
            conn.execute(sql_text("DELETE FROM jarvis_memory_fts"))
    for item_id, text in rows:
        _fts_put(item_id, text)
        _vector_add(item_id, text)
    return {"indexed": len(rows), "vector_backend": bool(_vectors())}


def stats(owner: Optional[str] = None) -> Dict[str, Any]:
    from sqlalchemy import func
    with session_scope() as db:
        q = _active(db.query(MemoryItem.kind, MemoryItem.status, func.count(MemoryItem.id)), owner)
        rows = q.group_by(MemoryItem.kind, MemoryItem.status).all()
    by_kind: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for kind, status, n in rows:
        by_kind[kind] = by_kind.get(kind, 0) + n
        by_status[status] = by_status.get(status, 0) + n
    store = None if _vector_failed else _vector_store
    return {"total": sum(by_kind.values()), "by_kind": by_kind, "by_status": by_status, "vector_backend": bool(store)}


def import_odysseus(entries: Iterable[Dict[str, Any]], owner: Optional[str] = "") -> Dict[str, int]:
    """One-way, idempotent copy from Odysseus memory.json. The source is never modified."""
    created = duplicates = 0
    for entry in entries or []:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        result = remember(text, owner=owner, category=str(entry.get("category") or "fact"), source_type="import",
                          source_ref=f"odysseus:{entry.get('id', '')}")
        created += result["action"] != "duplicate"
        duplicates += result["action"] == "duplicate"
    return {"created": created, "duplicates": duplicates}


def export_all(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = db.query(MemoryItem).filter(MemoryItem.deleted_at.is_(None))
        if owner is not None:
            q = q.filter(MemoryItem.owner == owner)
        return [{**to_dict(r), "owner": r.owner} for r in q.all()]


def sync_from_odysseus(entries: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Copy memories new in Odysseus's store. Jarvis stays complete if that file is rewritten."""
    entries = list(entries or [])
    with session_scope() as db:
        known = {r[0] for r in db.query(MemoryItem.source_ref).filter(MemoryItem.source_ref.like("odysseus:%")).all()}
    created = 0
    for entry in entries:
        if f"odysseus:{entry.get('id', '')}" in known or not str(entry.get("text") or "").strip():
            continue
        result = remember(str(entry["text"]).strip(), owner=str(entry.get("owner") or ""), category=str(entry.get("category") or "fact"),
                          source_type="import", source_ref=f"odysseus:{entry.get('id', '')}")
        created += result["action"] != "duplicate"
    return {"created": created, "total": len(entries)}


def delete_mirrored(odysseus_id: str, owner: Optional[str] = "") -> bool:
    with session_scope() as db:
        row = db.query(MemoryItem.id).filter(MemoryItem.source_ref == f"odysseus:{odysseus_id}", MemoryItem.owner == (owner or ""), MemoryItem.deleted_at.is_(None)).first()
    if row is None:
        return False
    delete(row[0], owner=owner)
    return True
