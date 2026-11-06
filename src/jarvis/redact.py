"""Mask secrets and personal identifiers before anything reaches logs or audit."""
from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[-_]?key|authorization|cookie|credential|private[-_]?key|refresh|session[-_]?id)",
    re.IGNORECASE,
)
_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{12,}"), "***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "***"),
    (re.compile(r"\bya29\.[0-9A-Za-z._-]{10,}"), "***"),
    (re.compile(r"\b1//[0-9A-Za-z_-]{20,}"), "***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{12,}"), "***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+"), "***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "***"),
    (re.compile(r"(?i)\b(api[-_]?key|access_token|refresh_token|client_secret|password)=([^&\s]+)"), r"\1=***"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<email>"),
]


def redact_text(text: Any) -> str:
    out = "" if text is None else str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a copy with secret-looking keys masked and strings scrubbed."""
    if _depth > 8:
        return "***"
    if isinstance(value, dict):
        return {
            k: "***" if isinstance(k, str) and _SENSITIVE_KEY.search(k) and v not in (None, "") else redact(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
