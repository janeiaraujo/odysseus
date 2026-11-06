"""Jarvis Model Router: catalog, capabilities, cost gate, ranking, fallback filter, usage ledger.

It reads Odysseus's ModelEndpoint rows and never changes them. Prices come only
from config (`pricing`) or the OpenRouter public catalog; an unknown price stays
NULL rather than being estimated, and the UI reports it as unknown.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from sqlalchemy import func

from src.jarvis import config
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import ModelUsage
from src.jarvis.redact import redact_text

logger = logging.getLogger(__name__)

LOCAL, FREE, PAID = "local", "free", "paid"
PROVIDERS = {
    "api.openai.com": "openai", "api.anthropic.com": "anthropic",
    "generativelanguage.googleapis.com": "google", "openrouter.ai": "openrouter",
    "integrate.api.nvidia.com": "nvidia", "api.groq.com": "groq", "api.mistral.ai": "mistral",
    "api.deepseek.com": "deepseek", "api.together.xyz": "together",
}
PROFILES: Dict[str, Dict[str, Any]] = {
    "economico": {"label": "Economico", "tiers": [LOCAL, FREE], "quality": 0.5, "requires": set()},
    "local": {"label": "Local", "tiers": [LOCAL], "quality": 1.0, "requires": set()},
    "equilibrado": {"label": "Equilibrado", "tiers": [LOCAL, FREE, PAID], "quality": 1.0, "requires": set()},
    "alta_qualidade": {"label": "Alta qualidade", "tiers": [PAID, LOCAL, FREE], "quality": 3.0, "requires": set()},
    "pesquisa": {"label": "Pesquisa profunda", "tiers": [PAID, LOCAL, FREE], "quality": 3.0, "requires": {"reasoning"}},
    "multimodal": {"label": "Multimodal", "tiers": [LOCAL, FREE, PAID], "quality": 1.5, "requires": {"vision"}},
}
_CAPABILITY_RULES: List[Tuple[str, str]] = [
    ("embeddings", r"embed|bge-|e5-|minilm"),
    ("audio", r"whisper|-tts|tts-|audio|transcribe|realtime"),
    ("image_generation", r"dall-e|imagen|flux|stable-diffusion|sdxl|image-gen|gpt-image"),
    ("vision", r"vision|llava|moondream|gpt-4o|gpt-4\.1|gpt-5|claude-(3|4|5|opus|sonnet|haiku|fable)|gemini|gemma-[34]|pixtral|qwen.*-vl"),
    ("reasoning", r"\bo[134]\b|o[134]-|gpt-5|opus|fable|deepseek-r|thinking|-pro\b|sonnet"),
    ("tools", r"gpt-|claude|gemini|mistral|qwen|llama-?3|command-r|deepseek"),
]
_QUALITY_RULES: List[Tuple[int, str]] = [
    (10, r"opus|fable|gpt-5\.\d+(?!.*(mini|nano))|-pro\b"),
    (8, r"sonnet|gpt-5(?!.*(mini|nano))|gpt-4o(?!-mini)|gpt-4\.1(?!-(mini|nano))"),
    (6, r"flash|haiku|mini|llama-?[34]|qwen|deepseek|mistral"),
    (4, r"nano|lite|small|tiny|phi"),
]
_NON_CHAT = {"embeddings", "audio", "image_generation"}
FAILURE_COOLDOWN = timedelta(minutes=5)
RATE_LIMIT_COOLDOWN = timedelta(seconds=60)


@dataclass
class ModelInfo:
    endpoint_id: str
    endpoint_name: str
    base_url_host: str
    model: str
    provider: str
    tier: str
    capabilities: List[str] = field(default_factory=list)
    quality: int = 5
    score: float = 0.0
    available: bool = True
    reason: str = ""
    avg_latency_ms: Optional[int] = None
    error_rate: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def provider_for(url: str) -> str:
    host = _host(url)
    for known, name in PROVIDERS.items():
        if host == known or host.endswith("." + known):
            return name
    port = urlparse(url or "").port if host else None
    if port == 11434:
        return "ollama"
    return "local" if _is_private_host(host) else (host or "unknown")


def _is_private_host(host: str) -> bool:
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".lan", ".ts.net")) or "." not in host:
        return bool(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def tier_for(url: str, model: str, endpoint_kind: Optional[str] = None) -> str:
    kind = (endpoint_kind or "auto").lower()
    if kind == "local" or (kind == "auto" and _is_private_host(_host(url))):
        return LOCAL
    for pattern in config.get("router.free_model_patterns", []) or []:
        try:
            if re.search(pattern, model or "", re.IGNORECASE):
                return FREE
        except re.error:
            continue
    return PAID


def capabilities_for(model: str, supports_tools: Optional[bool] = None) -> List[str]:
    name = (model or "").lower()
    caps = {cap for cap, pattern in _CAPABILITY_RULES if re.search(pattern, name)}
    if supports_tools is True:
        caps.add("tools")
    elif supports_tools is False:
        caps.discard("tools")
    if not caps & _NON_CHAT:
        caps.add("text")
    return sorted(caps)


def quality_for(model: str) -> int:
    override = (config.get("router.quality_overrides", {}) or {}).get(model)
    if isinstance(override, (int, float)):
        return int(override)
    name = (model or "").lower()
    for score, pattern in _QUALITY_RULES:
        if re.search(pattern, name):
            return score
    return 5


def _load_endpoints(owner: Optional[str]) -> List[Any]:
    from core.database import ModelEndpoint, SessionLocal
    from src.auth_helpers import owner_filter

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            q = owner_filter(q, ModelEndpoint, owner)
        rows = q.all()
        db.expunge_all()
        return rows
    finally:
        db.close()


def _configured_chain(owner: Optional[str]) -> Set[str]:
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        out: Set[str] = set()
        for key in ("default_model_fallbacks", "utility_model_fallbacks", "vision_model_fallbacks"):
            chain = get_user_setting(key, owner or "", settings.get(key) or []) or []
            out.update(e.get("model", "") for e in chain if isinstance(e, dict))
        return out
    except Exception as exc:
        logger.debug("fallback chain unavailable: %s", exc)
        return set()


def catalog(owner: Optional[str] = "", endpoints: Optional[Iterable[Any]] = None) -> List[ModelInfo]:
    """Every enabled model visible to `owner`, hiding what the user hid on the endpoint."""
    from src.endpoint_resolver import _endpoint_enabled_models

    out: List[ModelInfo] = []
    for ep in (endpoints if endpoints is not None else _load_endpoints(owner)):
        if (getattr(ep, "model_type", "llm") or "llm") not in ("llm", "chat"):
            continue
        for model in _endpoint_enabled_models(ep):
            out.append(ModelInfo(
                endpoint_id=ep.id, endpoint_name=ep.name, base_url_host=_host(ep.base_url), model=model,
                provider=provider_for(ep.base_url),
                tier=tier_for(ep.base_url, model, getattr(ep, "endpoint_kind", None)),
                capabilities=capabilities_for(model, getattr(ep, "supports_tools", None)),
                quality=quality_for(model),
            ))
    return out


def month_start():
    return utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_cost(owner: Optional[str] = None) -> float:
    with session_scope() as db:
        q = db.query(func.coalesce(func.sum(ModelUsage.cost_usd), 0.0)).filter(ModelUsage.ts >= month_start())
        if owner is not None:
            q = q.filter(ModelUsage.owner == owner)
        return float(q.scalar() or 0.0)


def budget_exceeded(owner: Optional[str] = None) -> bool:
    budget = config.get("budget", {}) or {}
    limit = float(budget.get("monthly_usd") or 0)
    return bool(limit > 0 and budget.get("block_paid_when_exceeded", True) and month_cost(owner) >= limit)


def paid_allowed(model: str, owner: Optional[str] = "", chain: Optional[Set[str]] = None) -> Tuple[bool, str]:
    """May a paid model be used WITHOUT the user having manually selected it?"""
    if budget_exceeded(owner):
        return False, "orcamento mensal atingido"
    mode = config.get("router.paid_fallback_mode", "configured")
    if mode == "always":
        return True, "modo always"
    if mode == "never":
        return False, "modo never"
    allow = set(config.get("router.paid_model_allowlist", []) or [])
    if model in allow or model in (chain if chain is not None else _configured_chain(owner)):
        return True, "listado pelo usuario"
    return False, "modelo pago sem autorizacao"


def filter_fallback_candidates(candidates: Sequence[Tuple[str, str, Dict]], owner: Optional[str] = "") -> List[Tuple[str, str, Dict]]:
    """Drop chat fallbacks the cost rules forbid. Fails closed for paid models on error."""
    chain = _configured_chain(owner)
    kept = []
    for candidate in candidates or []:
        url, model = candidate[0], candidate[1]
        try:
            if tier_for(url, model) == PAID:
                ok, why = paid_allowed(model, owner, chain)
                if not ok:
                    logger.info("[jarvis.router] fallback bloqueado: %s (%s)", model, why)
                    continue
        except Exception as exc:
            logger.warning("[jarvis.router] cost check failed, dropping paid fallback %s: %s", model, redact_text(exc))
            continue
        kept.append(candidate)
    return kept


def _recent_stats(models: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    now, stats = utcnow(), {}
    wanted = set(models)
    with session_scope() as db:
        rows = (db.query(ModelUsage.model, ModelUsage.ok, ModelUsage.latency_ms, ModelUsage.error_kind, ModelUsage.ts)
                .filter(ModelUsage.ts >= now - timedelta(days=7), ModelUsage.model.in_(wanted))
                .order_by(ModelUsage.ts.desc()).limit(5000).all())
    for model, ok, latency, error_kind, ts in rows:
        s = stats.setdefault(model, {"n": 0, "errors": 0, "latency": [], "streak": 0, "streak_open": True,
                                     "last_ts": ts, "last_err": "", "last_minute": 0})
        if s["n"] >= 30:
            continue
        s["n"] += 1
        if ts >= now - timedelta(minutes=1):
            s["last_minute"] += 1
        if ok:
            s["streak_open"] = False
            if latency:
                s["latency"].append(latency)
        else:
            s["errors"] += 1
            if s["streak_open"]:
                s["streak"] += 1
                s["last_err"] = s["last_err"] or error_kind
    return stats


def _availability(stat: Optional[Dict[str, Any]], model: str) -> Tuple[bool, str]:
    if not stat:
        return True, ""
    age = utcnow() - stat["last_ts"]
    if stat["streak"] and stat["last_err"] == "rate_limit" and age < RATE_LIMIT_COOLDOWN:
        return False, "limite de requisicoes (429), aguardando"
    if stat["streak"] >= 3 and age < FAILURE_COOLDOWN:
        return False, f"{stat['streak']} falhas seguidas, em pausa"
    rpm = (config.get("router.rate_limit_rpm", {}) or {}).get(model)
    if rpm and stat["last_minute"] >= int(rpm):
        return False, f"limite configurado de {rpm} req/min"
    return True, ""


def rank(*, owner: Optional[str] = "", profile: Optional[str] = None, requires: Optional[Iterable[str]] = None,
         endpoints: Optional[Iterable[Any]] = None, include_unavailable: bool = False) -> List[ModelInfo]:
    """Models that satisfy the profile, best first. Paid models need authorization."""
    name = profile or config.get("router.profile", "equilibrado")
    prof = PROFILES.get(name) or PROFILES["equilibrado"]
    needed = set(requires or ()) | set(prof["requires"]) | {"text"}
    models = [m for m in catalog(owner, endpoints) if m.tier in prof["tiers"] and needed <= set(m.capabilities)]
    stats, chain, out = _recent_stats(m.model for m in models), _configured_chain(owner), []
    tiers = prof["tiers"]
    for m in models:
        stat = stats.get(m.model)
        m.available, m.reason = _availability(stat, m.model)
        if m.tier == PAID and m.available:
            m.available, why = paid_allowed(m.model, owner, chain)
            m.reason = "" if m.available else why
        penalty = 0.0
        if stat and stat["n"]:
            m.error_rate = round(stat["errors"] / stat["n"], 3)
            penalty += m.error_rate * 20
            if stat["latency"]:
                m.avg_latency_ms = int(sum(stat["latency"]) / len(stat["latency"]))
                penalty += min(m.avg_latency_ms / 1000.0, 10)
        m.score = round((len(tiers) - tiers.index(m.tier)) * 20 + m.quality * prof["quality"] - penalty, 2)
        if m.available or include_unavailable:
            out.append(m)
    out.sort(key=lambda m: (m.available, m.score), reverse=True)
    return out


def select(**kwargs: Any) -> Optional[ModelInfo]:
    ranked = rank(**kwargs)
    return ranked[0] if ranked else None


def _cost(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    price = (config.get("pricing", {}) or {}).get(model)
    if not isinstance(price, dict):
        return None
    try:
        return round(input_tokens / 1e6 * float(price.get("input_per_mtok", 0))
                     + output_tokens / 1e6 * float(price.get("output_per_mtok", 0)), 6)
    except (TypeError, ValueError):
        return None


def classify_error(error: Any) -> str:
    text = str(error or "").lower()
    if "429" in text or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "auth"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(code in text for code in ("500", "502", "503", "504")):
        return "server"
    return "other" if text else ""


def record_usage(*, owner: Optional[str] = "", url: str = "", model: str = "", input_tokens: int = 0, output_tokens: int = 0,
                 latency_ms: int = 0, ok: bool = True, error: Any = None, purpose: str = "chat") -> None:
    """Never raises: accounting must not break a chat turn."""
    try:
        tier = tier_for(url, model)
        cost = 0.0 if tier != PAID else _cost(model, int(input_tokens or 0), int(output_tokens or 0))
        with session_scope() as db:
            db.add(ModelUsage(owner=owner or "", provider=provider_for(url), model=model or "", tier=tier, purpose=purpose,
                              input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
                              latency_ms=int(latency_ms or 0), ok=ok, error_kind=classify_error(error) if not ok else "",
                              cost_usd=cost))
    except Exception as exc:
        logger.warning("[jarvis.router] usage record failed: %s", redact_text(exc))


def record_chat_metrics(owner: Optional[str], url: str, requested_model: str, metrics: Optional[Dict[str, Any]], purpose: str = "chat") -> None:
    metrics = metrics or {}
    total = metrics.get("total_time") or 0
    record_usage(owner=owner, url=url, model=str(metrics.get("model") or requested_model or ""),
                 input_tokens=metrics.get("input_tokens") or 0, output_tokens=metrics.get("output_tokens") or 0,
                 latency_ms=int(float(total) * 1000) if total else 0, purpose=purpose)


def usage_summary(owner: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    with session_scope() as db:
        q = db.query(ModelUsage.provider, ModelUsage.model, ModelUsage.tier, func.count(ModelUsage.id),
                     func.sum(ModelUsage.input_tokens), func.sum(ModelUsage.output_tokens),
                     func.avg(ModelUsage.latency_ms), func.sum(ModelUsage.cost_usd),
                     func.sum(func.iif(ModelUsage.ok == False, 1, 0)),  # noqa: E712
                     func.sum(func.iif(ModelUsage.cost_usd == None, 1, 0))  # noqa: E711
                     ).filter(ModelUsage.ts >= since)
        if owner is not None:
            q = q.filter(ModelUsage.owner == owner)
        rows = q.group_by(ModelUsage.provider, ModelUsage.model, ModelUsage.tier).all()
    models = [{"provider": p, "model": m, "tier": t, "calls": n, "input_tokens": int(i or 0), "output_tokens": int(o or 0),
               "avg_latency_ms": int(l or 0), "cost_usd": round(float(c or 0), 6), "errors": int(e or 0),
               "calls_without_price": int(u or 0)} for p, m, t, n, i, o, l, c, e, u in rows]
    models.sort(key=lambda r: r["calls"], reverse=True)
    budget = float(config.get("budget.monthly_usd", 0) or 0)
    return {"days": days, "models": models, "month_cost_usd": round(month_cost(owner), 6), "monthly_budget_usd": budget,
            "budget_exceeded": budget_exceeded(owner), "calls_without_price": sum(r["calls_without_price"] for r in models)}


async def refresh_openrouter_pricing(client: Any = None) -> int:
    """Copy real prices from OpenRouter's public catalog. Returns models updated."""
    import httpx

    owns = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get("https://openrouter.ai/api/v1/models")
        resp.raise_for_status()
        data = resp.json().get("data", [])
    finally:
        if owns:
            await client.aclose()
    pricing = dict(config.get("pricing", {}) or {})
    updated = 0
    for row in data:
        price = row.get("pricing") or {}
        try:
            pricing[row["id"]] = {"input_per_mtok": float(price.get("prompt", 0)) * 1e6,
                                  "output_per_mtok": float(price.get("completion", 0)) * 1e6, "source": "openrouter"}
            updated += 1
        except (KeyError, TypeError, ValueError):
            continue
    config.update_config({"pricing": pricing})
    return updated
