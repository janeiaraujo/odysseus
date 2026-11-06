"""Storage contract, integrity checks and an operation ledger for cost tracking."""
from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import case, func

from src.jarvis import config
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import StorageOp

logger = logging.getLogger(__name__)

# Published at developers.cloudflare.com/r2/pricing (Standard storage), checked 2026-11-06.
R2_PRICES = {"class_a_per_million": 4.50, "class_b_per_million": 0.36, "gb_month": 0.015,
             "free_class_a": 1_000_000, "free_class_b": 10_000_000, "free_gb_month": 10}
MAX_KEY_BYTES = 1024  # developers.cloudflare.com/r2/platform/limits


class StorageError(RuntimeError):
    pass


class IntegrityError(StorageError):
    pass


class QuotaExceeded(StorageError):
    pass


@dataclass
class ObjectInfo:
    key: str
    size: int = 0
    sha256: str = ""
    last_modified: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)


def validate_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\0" in key:
        raise StorageError("chave de objeto invalida")
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise StorageError(f"chave maior que {MAX_KEY_BYTES} bytes")
    return key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def usage_this_month(backend: Optional[str] = None) -> Dict[str, Any]:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as db:
        q = db.query(func.sum(case((StorageOp.op_class == "A", 1), else_=0)), func.sum(case((StorageOp.op_class == "B", 1), else_=0)),
                     func.coalesce(func.sum(StorageOp.bytes), 0), func.sum(case((StorageOp.ok == False, 1), else_=0))).filter(StorageOp.ts >= start)  # noqa: E712
        if backend:
            q = q.filter(StorageOp.backend == backend)
        class_a, class_b, transferred, errors = q.one()
    class_a, class_b = int(class_a or 0), int(class_b or 0)
    billable_a = max(0, class_a - R2_PRICES["free_class_a"])
    billable_b = max(0, class_b - R2_PRICES["free_class_b"])
    return {"class_a": class_a, "class_b": class_b, "bytes": int(transferred or 0), "errors": int(errors or 0),
            "estimated_op_cost_usd": round(billable_a / 1e6 * R2_PRICES["class_a_per_million"] + billable_b / 1e6 * R2_PRICES["class_b_per_million"], 4)}


class StorageBackend(ABC):
    name = "base"
    enforce_quota = False

    def _meter(self, op: str, op_class: str, size: int = 0, ok: bool = True) -> None:
        try:
            with session_scope() as db:
                db.add(StorageOp(backend=self.name, op=op, op_class=op_class, bytes=size, ok=ok))
        except Exception as exc:
            logger.debug("storage meter failed: %s", exc)

    def _check_quota(self, op_class: str) -> None:
        if not self.enforce_quota:
            return
        usage = usage_this_month(self.name)
        limit = config.get(f"storage.max_class_{op_class.lower()}_month", 0)
        if limit and usage[f"class_{op_class.lower()}"] >= int(limit):
            raise QuotaExceeded(f"limite mensal de operacoes classe {op_class} atingido ({limit})")

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, metadata: Optional[Dict[str, str]] = None) -> ObjectInfo: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def head(self, key: str) -> Optional[ObjectInfo]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str = "", limit: int = 100000) -> List[ObjectInfo]: ...

    def put_file(self, key: str, path: Path) -> Dict[str, Any]:
        info = self.put_bytes(key, Path(path).read_bytes())
        return {"key": info.key, "size": info.size, "sha256": info.sha256}

    def get_file(self, key: str, path: Path) -> Dict[str, Any]:
        data = self.get_bytes(key)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
        os.replace(tmp, path)
        return {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "path": str(path)}

    def verify(self, prefix: str = "") -> Dict[str, Any]:
        """Download and re-hash everything that recorded a checksum."""
        checked, bad = 0, []
        for info in self.list(prefix):
            if info.size == 0:
                continue
            try:
                self.get_bytes(info.key)
                checked += 1
            except IntegrityError:
                bad.append(info.key)
        return {"checked": checked, "corrupted": bad}


def apply_retention(backend: StorageBackend, prefix: str, keep_last: int) -> Dict[str, Any]:
    """Keep the newest `keep_last` objects under prefix. keep_last < 1 is refused."""
    if keep_last < 1:
        raise StorageError("a retencao precisa manter pelo menos um objeto")
    objects = sorted(backend.list(prefix), key=lambda o: (o.last_modified, o.key), reverse=True)
    removed = [o.key for o in objects[keep_last:]]
    for key in removed:
        backend.delete(key)
    return {"kept": len(objects) - len(removed), "removed": removed}
