"""EXPERIMENTAL: store small records in the NAME of zero-byte objects.

Each record is sealed with AES-GCM and base64url-encoded into the key, so names
reveal nothing and the object body is empty. The local SQLite index serves every
lookup; the bucket is only the durable copy, and the index can be rebuilt from it.

Known costs, measured by `compare_costs` rather than assumed:
- Every record is one PUT (class A), even with an empty body. Batching records
  into one regular object is cheaper to write once there's more than a few.
- Cloudflare's pricing page doesn't state how zero-byte objects or their keys are
  billed as storage, so storage is shown as unknown.
- Keys cap at 1024 bytes, about 700 bytes of plaintext after sealing and encoding.
- R2 has no semantic search; ChromaDB stays responsible for that.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import ObjectIndex
from src.jarvis.storage.base import MAX_KEY_BYTES, R2_PRICES, StorageBackend, StorageError

PREFIX = "jarvis/zk"


class ValueTooLarge(StorageError):
    pass


def _derive(master: bytes, namespace: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"jarvis-zk:{namespace}".encode()).derive(master)


class ZeroKeyStore:
    def __init__(self, backend: StorageBackend, namespace: str, *, master_key: bytes, owner: str = ""):
        if not namespace.replace("-", "").replace("_", "").isalnum():
            raise StorageError("namespace invalido")
        self.backend, self.namespace, self.owner = backend, namespace, owner
        self._key = _derive(master_key, namespace)

    def _prefix(self) -> str:
        return f"{PREFIX}/{self.namespace}/"

    def _seal(self, value: Dict[str, Any]) -> str:
        plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        # Nonce derived from the content: writing the same record twice is the same key (idempotent).
        nonce = hmac.new(self._key, plaintext, hashlib.sha256).digest()[:12]
        token = base64.urlsafe_b64encode(nonce + AESGCM(self._key).encrypt(nonce, plaintext, self.namespace.encode())).decode().rstrip("=")
        key = self._prefix() + token
        if len(key.encode()) > MAX_KEY_BYTES:
            raise ValueTooLarge(f"valor nao cabe em uma chave de {MAX_KEY_BYTES} bytes; use um objeto normal")
        return key

    def _open(self, key: str) -> Dict[str, Any]:
        token = key.rsplit("/", 1)[-1]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        return json.loads(AESGCM(self._key).decrypt(raw[:12], raw[12:], self.namespace.encode()))

    def put(self, value: Dict[str, Any]) -> str:
        key = self._seal(value)
        with session_scope() as db:
            row = db.get(ObjectIndex, key)
            if row is not None and row.deleted_at is None:
                return key  # already stored: no extra class A operation
        self.backend.put_bytes(key, b"")
        with session_scope() as db:
            db.merge(ObjectIndex(key=key, owner=self.owner, backend=self.backend.name, namespace=self.namespace, size=0, deleted_at=None))
        return key

    def values(self) -> List[Dict[str, Any]]:
        """Reads the local index only: zero storage operations."""
        with session_scope() as db:
            keys = [r.key for r in db.query(ObjectIndex.key).filter(ObjectIndex.namespace == self.namespace, ObjectIndex.owner == self.owner, ObjectIndex.deleted_at.is_(None)).all()]
        return [self._open(k) for k in keys]

    def delete(self, value: Dict[str, Any]) -> None:
        key = self._seal(value)
        self.backend.delete(key)
        with session_scope() as db:
            row = db.get(ObjectIndex, key)
            if row is not None:
                row.deleted_at = utcnow()

    def rebuild_index(self) -> Dict[str, int]:
        """Recreate the index from the bucket, e.g. on a new machine. Foreign keys are skipped."""
        rebuilt = unreadable = 0
        for info in self.backend.list(self._prefix()):
            try:
                self._open(info.key)
            except Exception:
                unreadable += 1
                continue
            with session_scope() as db:
                db.merge(ObjectIndex(key=info.key, owner=self.owner, backend=self.backend.name, namespace=self.namespace, size=info.size, deleted_at=None))
            rebuilt += 1
        return {"rebuilt": rebuilt, "unreadable": unreadable}

    def validate(self) -> Dict[str, Any]:
        """Compare index and bucket."""
        with session_scope() as db:
            indexed = {r.key for r in db.query(ObjectIndex.key).filter(ObjectIndex.namespace == self.namespace, ObjectIndex.owner == self.owner, ObjectIndex.deleted_at.is_(None)).all()}
        remote = {o.key for o in self.backend.list(self._prefix())}
        return {"ok": indexed == remote, "only_in_index": len(indexed - remote), "only_in_bucket": len(remote - indexed), "total": len(remote)}


def benchmark(backend: StorageBackend, *, master_key: bytes, records: int = 50) -> Dict[str, Any]:
    """Time N zero-byte writes against one aggregated object on the given backend."""
    store = ZeroKeyStore(backend, f"bench{int(time.time())}", master_key=master_key)
    rows = [{"i": i, "t": "preferencia de exemplo"} for i in range(records)]
    start = time.perf_counter()
    for row in rows:
        store.put(row)
    zero_seconds = time.perf_counter() - start
    body = "\n".join(json.dumps(r) for r in rows).encode()
    key = f"jarvis/bench/{store.namespace}.jsonl"
    start = time.perf_counter()
    backend.put_bytes(key, body)
    batch_seconds = time.perf_counter() - start
    for row in rows:
        store.delete(row)
    backend.delete(key)
    return {"records": records, "zero_byte_seconds": round(zero_seconds, 4), "batched_object_seconds": round(batch_seconds, 4),
            "zero_byte_class_a": records, "batched_class_a": 1, "backend": backend.name}


def compare_costs(records_per_month: int, avg_record_bytes: int = 200, batch_size: int = 1000) -> Dict[str, Any]:
    """Class A write cost of each strategy beyond the free tier, using published prices."""
    def cost(ops: int) -> float:
        return round(max(0, ops - R2_PRICES["free_class_a"]) / 1e6 * R2_PRICES["class_a_per_million"], 4)
    batches = max(1, -(-records_per_month // max(batch_size, 1)))
    return {
        "records_per_month": records_per_month,
        "zero_byte": {"class_a": records_per_month, "write_cost_usd": cost(records_per_month), "storage_cost": "nao documentado pela Cloudflare"},
        "batched": {"class_a": batches, "write_cost_usd": cost(batches),
                    "storage_gb": round(records_per_month * avg_record_bytes / 1e9, 4)},
        "recommendation": "batched" if records_per_month > batch_size else "qualquer um",
    }
