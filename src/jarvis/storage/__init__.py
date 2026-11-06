"""Interchangeable object storage: local filesystem or Cloudflare R2."""
from __future__ import annotations

from typing import Optional

from src.jarvis import config
from src.jarvis.storage.base import IntegrityError, ObjectInfo, QuotaExceeded, StorageBackend, StorageError, apply_retention, usage_this_month


def get_backend(name: Optional[str] = None) -> StorageBackend:
    name = name or config.get("storage.backend", "local")
    if name == "r2":
        from src.jarvis.storage.r2 import R2Backend
        return R2Backend.from_environment()
    if name == "local":
        from src.jarvis.storage.local import LocalBackend
        return LocalBackend()
    raise StorageError(f"backend de armazenamento desconhecido: {name}")


__all__ = ["IntegrityError", "ObjectInfo", "QuotaExceeded", "StorageBackend", "StorageError", "apply_retention", "get_backend", "usage_this_month"]
