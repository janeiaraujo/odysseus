"""Centralized Jarvis configuration stored in data/jarvis/config.json."""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict

from core.atomic_io import atomic_write_json
from src.constants import JARVIS_CONFIG_FILE

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "timezone": "America/Sao_Paulo",
    "intents": {"enabled": True},
    "persona": {"enabled": True, "name": "J.A.R.V.I.S.", "language": "pt-BR", "address_as": ""},
    "router": {
        "profile": "equilibrado",
        # never = paid models never used as fallback; configured = only models the
        # user listed in a fallback chain or the allowlist; always = router may pick any.
        "paid_fallback_mode": "configured",
        "free_model_patterns": [":free$", "^openrouter/free$"],
        "paid_model_allowlist": [],
        "rate_limit_rpm": {},
        "quality_overrides": {},
    },
    "budget": {"monthly_usd": 0.0, "block_paid_when_exceeded": True},
    "pricing": {},
    "autonomy": {"global": {}, "users": {}},
    "learning": {"enabled": True, "auto_accept_explicit": True},
    "memory": {"inject_in_chat": True, "max_injected": 6},
    "voice": {"store_audio": False, "language": "pt", "tts_voice": ""},
    "firebase": {"enabled": False, "project_id": "", "credentials_file": "", "collections": ["memory", "devices"], "encrypt_payloads": True},
    "storage": {
        "backend": "local",
        # Stop before leaving Cloudflare's free tier (1M class A, 10M class B per month).
        "max_class_a_month": 900000,
        "max_class_b_month": 9000000,
        "retention": {"backups_keep_last": 14},
    },
    "home": {"enabled": False},
    "google": {"drive_allowed_folders": {}, "email_categories": {}, "photo_cache_hours": 1},
}

_lock = threading.RLock()


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> Dict[str, Any]:
    try:
        with open(JARVIS_CONFIG_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    return _deep_merge(DEFAULTS, stored)


def get(path: str, default: Any = None) -> Any:
    node: Any = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        merged = _deep_merge(load_config(), patch or {})
        os.makedirs(os.path.dirname(JARVIS_CONFIG_FILE), exist_ok=True)
        atomic_write_json(JARVIS_CONFIG_FILE, merged)
        return merged
