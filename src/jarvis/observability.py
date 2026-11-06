"""System snapshot for the monitoring panel. Each probe fails on its own."""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import func

from src.constants import APP_DB, DATA_DIR, JARVIS_DB
from src.jarvis import config, memory, policy, replication, router
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import MissionStep, Mission, OAuthAccount, OutboundEmail, SyncState

logger = logging.getLogger(__name__)


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def services() -> List[Dict[str, Any]]:
    out = []
    chroma = (os.getenv("CHROMADB_HOST", "localhost"), int(os.getenv("CHROMADB_PORT", "8100")))
    out.append({"name": "ChromaDB", "online": _port_open(*chroma), "required": True})
    searx = urlparse(os.getenv("SEARXNG_INSTANCE", "http://localhost:8080"))
    out.append({"name": "SearXNG", "online": _port_open(searx.hostname or "localhost", searx.port or 80), "required": False})
    ollama = urlparse(os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    out.append({"name": "Ollama", "online": _port_open(ollama.hostname or "localhost", ollama.port or 11434), "required": False})
    from src.jarvis import home
    if home.status()["configured"]:
        ha = urlparse(home.status()["url"])
        out.append({"name": "Home Assistant", "online": _port_open(ha.hostname or "", ha.port or 8123), "required": False})
    return out


def _meminfo() -> Dict[str, int]:
    info: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return info


def _gpu() -> Optional[List[Dict[str, Any]]]:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        rows = []
        for line in out.stdout.strip().splitlines():
            name, util, used, total = [p.strip() for p in line.split(",")]
            rows.append({"name": name, "utilization_pct": int(util), "memory_used_mb": int(used), "memory_total_mb": int(total)})
        return rows
    except Exception as exc:
        logger.debug("gpu probe failed: %s", exc)
        return None


def resources() -> Dict[str, Any]:
    disk = shutil.disk_usage(DATA_DIR if os.path.isdir(DATA_DIR) else "/")
    mem = _meminfo()
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    total, available = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    return {"cpu_count": os.cpu_count(), "load_1m": round(load[0], 2), "load_5m": round(load[1], 2), "memory_total": total, "memory_used": total - available,
            "memory_pct": round((total - available) / total * 100, 1) if total else None, "disk_total": disk.total, "disk_free": disk.free,
            "disk_pct": round((disk.total - disk.free) / disk.total * 100, 1), "gpu": _gpu()}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def backups() -> List[Dict[str, Any]]:
    from src.jarvis.backup import backup_folder
    folder = backup_folder()
    if not folder.is_dir():
        return []
    try:
        rows = [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime} for p in folder.glob("*.jrvb")]
    except OSError:
        return []
    return sorted(rows, key=lambda r: r["modified"], reverse=True)[:20]


def snapshot(owner: Optional[str] = None) -> Dict[str, Any]:
    """`owner` scopes personal data; system resources are global, so callers gate them to admins."""
    with session_scope() as db:
        accounts = db.query(OAuthAccount.status, func.count(OAuthAccount.id)).filter(OAuthAccount.status != "revoked")
        mail = db.query(OutboundEmail.status, func.count(OutboundEmail.id))
        missions = db.query(Mission.status, func.count(Mission.id))
        running = db.query(func.count(MissionStep.id)).join(Mission, MissionStep.mission_id == Mission.id).filter(MissionStep.status == "running")
        sync = db.query(SyncState)
        if owner is not None:
            accounts, mail, missions = accounts.filter(OAuthAccount.owner == owner), mail.filter(OutboundEmail.owner == owner), missions.filter(Mission.owner == owner)
            running, sync = running.filter(Mission.owner == owner), sync.filter(SyncState.owner == owner)
        summary = {"google_accounts": dict(accounts.group_by(OAuthAccount.status).all()), "mail_queue": dict(mail.group_by(OutboundEmail.status).all()),
                   "missions": dict(missions.group_by(Mission.status).all()), "steps_running": running.scalar() or 0,
                   "sync": [{"provider": s.provider, "resource": s.resource.split(":")[0], "last_ok_at": s.last_ok_at.isoformat() + "Z" if s.last_ok_at else None, "last_error": s.last_error} for s in sync.limit(50).all()]}
    try:
        models = router.catalog(owner or "")
        tiers: Dict[str, int] = {}
        for model in models:
            tiers[model.tier] = tiers.get(model.tier, 0) + 1
        model_summary = {"total": len(models), "by_tier": tiers}
    except Exception as exc:
        model_summary = {"total": 0, "by_tier": {}, "error": type(exc).__name__}
    from src.jarvis.storage import usage_this_month
    return {"generated_at": utcnow().isoformat() + "Z", "services": services(), "resources": resources(), "models": model_summary, "usage": router.usage_summary(owner),
            "memory": memory.stats(owner), "approvals_pending": len(policy.list_approvals(owner)), "replication": replication.status(), "storage": usage_this_month(),
            "databases": {"app_db_bytes": _size(APP_DB), "jarvis_db_bytes": _size(JARVIS_DB)}, "backups": backups(), **summary}
