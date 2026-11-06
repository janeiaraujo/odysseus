"""Filesystem backend: the default, and the fallback when no cloud is configured."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.constants import JARVIS_OBJECTS_DIR
from src.jarvis.storage.base import IntegrityError, ObjectInfo, StorageBackend, StorageError, validate_key

_META = ".meta.json"


class LocalBackend(StorageBackend):
    name = "local"

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or JARVIS_OBJECTS_DIR).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if self.root not in path.parents:
            raise StorageError("chave fora do diretorio de objetos")
        return path

    def put_bytes(self, key: str, data: bytes, metadata: Optional[Dict[str, str]] = None) -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        with open(str(path) + _META, "w", encoding="utf-8") as f:
            json.dump({**(metadata or {}), "sha256": digest}, f)
        self._meter("put", "A", len(data))
        return ObjectInfo(key=key, size=len(data), sha256=digest, metadata=metadata or {})

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"objeto nao encontrado: {key}")
        data = path.read_bytes()
        info = self.head(key)
        if info and info.sha256 and info.sha256 != hashlib.sha256(data).hexdigest():
            raise IntegrityError(f"checksum divergente: {key}")
        self._meter("get", "B", len(data))
        return data

    def head(self, key: str) -> Optional[ObjectInfo]:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            meta = json.loads(Path(str(path) + _META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return ObjectInfo(key=key, size=path.stat().st_size, sha256=meta.pop("sha256", ""), metadata=meta,
                          last_modified=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        Path(str(path) + _META).unlink(missing_ok=True)
        self._meter("delete", "free")

    def list(self, prefix: str = "", limit: int = 100000) -> List[ObjectInfo]:
        out: List[ObjectInfo] = []
        if self.root.is_dir():
            for path in sorted(self.root.rglob("*")):
                if not path.is_file() or path.name.endswith((_META, ".tmp")):
                    continue
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(self.head(key))
                    if len(out) >= limit:
                        break
        self._meter("list", "A")
        return out
