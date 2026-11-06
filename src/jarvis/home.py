"""Home Assistant integration with a safety layer independent of any model.

Text never reaches a device directly. A command becomes a structured action,
is checked against the allow-table below, and passes the approval policy. Only
listed domain/operation pairs exist; temperature and volume are range-checked.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from src.jarvis import audit, config, memory, policy
from src.jarvis.db import session_scope
from src.jarvis.models import Device
from src.secret_storage import decrypt, encrypt

TEMP_RANGE = (16.0, 30.0)
_OP_LABELS = {"on": "Ligar", "off": "Desligar", "open": "Abrir", "close": "Fechar", "lock": "Trancar", "unlock": "Destrancar",
              "arm": "Armar", "disarm": "Desarmar", "set_temperature": "Ajustar temperatura de"}
_OPS: Dict[str, Dict[str, tuple]] = {
    "light": {"on": ("turn_on", policy.REVERSIBLE), "off": ("turn_off", policy.REVERSIBLE)},
    "switch": {"on": ("turn_on", policy.REVERSIBLE), "off": ("turn_off", policy.REVERSIBLE)},
    "media_player": {"on": ("turn_on", policy.REVERSIBLE), "off": ("turn_off", policy.REVERSIBLE)},
    "fan": {"on": ("turn_on", policy.REVERSIBLE), "off": ("turn_off", policy.REVERSIBLE)},
    "climate": {"on": ("turn_on", policy.REVERSIBLE), "off": ("turn_off", policy.REVERSIBLE),
                "set_temperature": ("set_temperature", policy.REVERSIBLE)},
    "cover": {"open": ("open_cover", policy.PHYSICAL_CRITICAL), "close": ("close_cover", policy.PHYSICAL_CRITICAL)},
    "lock": {"lock": ("lock", policy.PHYSICAL_CRITICAL), "unlock": ("unlock", policy.PHYSICAL_CRITICAL)},
    "alarm_control_panel": {"arm": ("alarm_arm_away", policy.PHYSICAL_CRITICAL), "disarm": ("alarm_disarm", policy.PHYSICAL_CRITICAL)},
}
_VERBS = [(re.compile(r"^(ligue|ligar|liga|acenda|acender|acende)\b"), "on"),
          (re.compile(r"^(desligue|desligar|desliga|apague|apagar|apaga)\b"), "off"),
          (re.compile(r"^(abra|abrir|abre)\b"), "open"), (re.compile(r"^(feche|fechar|fecha)\b"), "close"),
          (re.compile(r"^(destranque|destrancar)\b"), "unlock"), (re.compile(r"^(tranque|trancar)\b"), "lock")]
_TEMP = re.compile(r"(?:ajuste|coloque|defina|mude|ponha)\s+(?:o |a )?(.+?)\s+(?:para|em|a)\s+(\d{2}(?:[.,]\d)?)\s*(?:graus|°)?$")
_FILLER = {"o", "a", "os", "as", "do", "da", "de", "no", "na", "em", "por", "favor"}


class HomeNotConfigured(RuntimeError):
    pass


class UnsafeAction(ValueError):
    pass


@dataclass
class Action:
    device_id: str
    entity_id: str
    domain: str
    op: str
    service: str
    risk: str
    data: Dict[str, Any] = field(default_factory=dict)
    name: str = ""


def _device_dict(row: Device) -> Dict[str, Any]:
    return {"id": row.id, "name": row.name, "aliases": json.loads(row.aliases or "[]"), "domain": row.domain, "entity_id": row.entity_id,
            "area": row.area, "source": row.source, "critical": bool(row.critical), "enabled": bool(row.enabled)}


def list_devices(owner: Optional[str] = "") -> List[Dict[str, Any]]:
    with session_scope() as db:
        return [_device_dict(r) for r in db.query(Device).filter(Device.owner == (owner or "")).order_by(Device.area, Device.name).all()]


def upsert_device(owner: Optional[str], *, name: str, entity_id: str, domain: Optional[str] = None, area: str = "",
                  aliases: Optional[List[str]] = None, critical: bool = False, source: str = "manual") -> Dict[str, Any]:
    if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id or ""):
        raise ValueError("entity_id inválido")
    with session_scope() as db:
        row = db.query(Device).filter(Device.owner == (owner or ""), Device.entity_id == entity_id).first()
        if row is None:
            row = Device(owner=owner or "", entity_id=entity_id)
            db.add(row)
        row.name, row.domain, row.area, row.source = name, domain or entity_id.split(".")[0], area, source
        row.aliases, row.critical = json.dumps(aliases or []), bool(critical)
        db.flush()
        return _device_dict(row)


def delete_device(owner: Optional[str], device_id: str) -> bool:
    with session_scope() as db:
        return bool(db.query(Device).filter(Device.owner == (owner or ""), Device.id == device_id).delete())


def build_action(device: Dict[str, Any], op: str, value: Any = None) -> Action:
    """The only path to an Action. Raises UnsafeAction for anything off the table."""
    if not device.get("enabled", True):
        raise UnsafeAction("dispositivo desativado")
    entry = _OPS.get(device["domain"], {}).get(op)
    if entry is None:
        raise UnsafeAction(f"operacao '{op}' não permitida para {device['domain']}")
    service, risk = entry
    data: Dict[str, Any] = {}
    if op == "set_temperature":
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            raise UnsafeAction("temperatura inválida") from None
        if not TEMP_RANGE[0] <= temperature <= TEMP_RANGE[1]:
            raise UnsafeAction(f"temperatura fora da faixa segura ({TEMP_RANGE[0]:.0f}-{TEMP_RANGE[1]:.0f} graus)")
        data["temperature"] = temperature
    if device.get("critical"):
        risk = policy.PHYSICAL_CRITICAL
    return Action(device["id"], device["entity_id"], device["domain"], op, service, risk, data, device["name"])


def _match_device(phrase: str, devices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    words = {w for w in memory.normalize(phrase).split() if w not in _FILLER}
    scored = []
    for device in devices:
        for label in [device["name"], *device.get("aliases", [])]:
            label_words = {w for w in memory.normalize(label).split() if w not in _FILLER}
            if label_words and label_words <= words | {w for w in memory.normalize(device.get("area", "")).split() if w not in _FILLER}:
                extra = words - label_words - set(memory.normalize(device.get("area", "")).split())
                scored.append((len(extra), -len(label_words), device))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]))
    best = [s for s in scored if s[:2] == scored[0][:2]]
    return best[0][2] if len({s[2]["id"] for s in best}) == 1 else None  # ambiguous: ask, don't guess


def parse_command(text: str, owner: Optional[str] = "") -> Optional[Dict[str, Any]]:
    """Map a Portuguese command to {device, op, value}; None if it isn't one."""
    norm = re.sub(r"^jarvis[,: ]+", "", memory.normalize(text)).rstrip(" .!")
    devices = [d for d in list_devices(owner) if d["enabled"]]
    if not devices:
        return None
    temp = _TEMP.match(norm)
    if temp:
        device = _match_device(temp.group(1), devices)
        return {"device": device, "op": "set_temperature", "value": temp.group(2).replace(",", ".")} if device else None
    for pattern, op in _VERBS:
        match = pattern.match(norm)
        if match:
            device = _match_device(norm[match.end():], devices)
            return {"device": device, "op": op, "value": None} if device else None
    return None


# --- Home Assistant REST --------------------------------------------------------

def configure(url: str, token: Optional[str]) -> None:
    patch: Dict[str, Any] = {"enabled": bool(url), "url": (url or "").rstrip("/")}
    if token:
        patch["token_enc"] = encrypt(token)
    config.update_config({"home": patch})


def status() -> Dict[str, Any]:
    home = config.get("home", {}) or {}
    return {"configured": bool(home.get("url") and home.get("token_enc")), "url": home.get("url", "")}


def _client(transport: Any = None) -> httpx.AsyncClient:
    home = config.get("home", {}) or {}
    if not home.get("url") or not home.get("token_enc"):
        raise HomeNotConfigured("Home Assistant não configurado")
    return httpx.AsyncClient(base_url=home["url"], timeout=15, transport=transport,
                             headers={"Authorization": f"Bearer {decrypt(home['token_enc'])}"})


async def discover(owner: Optional[str] = "", *, transport: Any = None) -> Dict[str, int]:
    """Register supported entities. Locks, covers and alarms are flagged critical."""
    async with _client(transport) as client:
        resp = await client.get("/api/states")
        resp.raise_for_status()
        states = resp.json()
    added = 0
    for state in states:
        entity = state.get("entity_id", "")
        domain = entity.split(".")[0]
        if domain in _OPS or domain in ("sensor", "binary_sensor", "camera"):
            upsert_device(owner, name=(state.get("attributes") or {}).get("friendly_name") or entity, entity_id=entity, domain=domain,
                          source="home_assistant", critical=domain in ("lock", "cover", "alarm_control_panel"))
            added += 1
    return {"discovered": added}


async def read_state(entity_id: str, *, transport: Any = None) -> Dict[str, Any]:
    async with _client(transport) as client:
        resp = await client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        return resp.json()


async def _call(action: Action, transport: Any = None) -> Dict[str, Any]:
    async with _client(transport) as client:
        resp = await client.post(f"/api/services/{action.domain}/{action.service}", json={"entity_id": action.entity_id, **action.data})
        resp.raise_for_status()
    return {"entity_id": action.entity_id, "service": action.service, "done": True}


def _approved_executor(owner: str, payload: Dict[str, Any]) -> Any:
    """Rebuild from the device table at approval time, so a stored payload can't widen scope."""
    device = next((d for d in list_devices(owner) if d["id"] == payload.get("device_id")), None)
    if device is None:
        raise LookupError("dispositivo não encontrado")
    return _call(build_action(device, payload.get("op", ""), payload.get("value")))


policy.register_executor("home.control", _approved_executor)


async def execute(device: Dict[str, Any], op: str, value: Any = None, *, owner: Optional[str] = "", transport: Any = None) -> Dict[str, Any]:
    action = build_action(device, op, value)
    return await policy.guarded(
        "home.control", action.risk, owner, f"{_OP_LABELS.get(op, op)} {action.name}",
        {"device_id": device["id"], "op": op, "value": value}, lambda: _call(action, transport))
