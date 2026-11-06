"""Persistent missions: goals split into steps with dependencies, retries and schedules.

Everything lives in the database, so missions survive restarts. Steps are claimed
with an atomic pending -> running update under a lease, so a second worker can't
run one too. A step that may have had external effects is not re-run after a
crash: only handlers declared idempotent are retried automatically.

A model may PROPOSE a plan, but only registered handlers with validated parameters
can run, each with a risk class fixed in code.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from src.jarvis import audit, policy
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import Mission, MissionStep
from src.jarvis.redact import redact_text

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=10)
BACKOFF_SECONDS = 30
MAX_STEPS = 25
TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass
class Handler:
    fn: Callable[[str, Dict[str, Any], Dict[str, Any]], Any]
    risk: str
    idempotent: bool = True
    description: str = ""
    params: tuple = ()


REGISTRY: Dict[str, Handler] = {}


def register(name: str, fn: Callable, risk: str = policy.READ, *, idempotent: bool = True, description: str = "", params: tuple = ()) -> None:
    REGISTRY[name] = Handler(fn, risk, idempotent, description, tuple(params))


def next_run(cron: str, tz: str, after: Optional[datetime] = None) -> datetime:
    """Next UTC-naive firing. The cron expression is interpreted in the user's timezone."""
    zone = ZoneInfo(tz)
    base = (after or utcnow()).replace(tzinfo=timezone.utc).astimezone(zone)
    return croniter(cron, base).get_next(datetime).astimezone(timezone.utc).replace(tzinfo=None)


def create(owner: str, title: str, steps: List[Dict[str, Any]], *, goal: str = "", cron: Optional[str] = None, tz: str = "America/Sao_Paulo") -> Dict[str, Any]:
    if not steps or len(steps) > MAX_STEPS:
        raise ValueError(f"a missao precisa de 1 a {MAX_STEPS} passos")
    for i, step in enumerate(steps):
        if step.get("handler") not in REGISTRY:
            raise ValueError(f"acao desconhecida: {step.get('handler')}")
        for dep in step.get("depends_on", []) or []:
            if not isinstance(dep, int) or dep < 0 or dep >= i:
                raise ValueError(f"dependencia invalida no passo {i}")  # earlier steps only: no cycles
        if not isinstance(step.get("params", {}), dict):
            raise ValueError(f"parametros invalidos no passo {i}")
    if cron and not croniter.is_valid(cron):
        raise ValueError("agendamento cron invalido")
    with session_scope() as db:
        mission = Mission(owner=owner or "", title=title[:200], goal=goal[:2000], cron=cron, timezone=tz, next_run_at=next_run(cron, tz) if cron else None)
        db.add(mission)
        db.flush()
        for i, step in enumerate(steps):
            db.add(MissionStep(mission_id=mission.id, idx=i, title=str(step.get("title") or step["handler"])[:200], handler=step["handler"],
                               params=json.dumps(step.get("params") or {}, ensure_ascii=False), depends_on=json.dumps(step.get("depends_on") or []),
                               max_attempts=int(step.get("max_attempts") or 3), status="cancelled" if cron else "pending"))
        mission_id = mission.id
    audit.record("mission.create", owner=owner, risk="reversible", target=mission_id, steps=len(steps), cron=cron or "")
    return get(owner, mission_id)


def _view(mission: Mission, steps: List[MissionStep]) -> Dict[str, Any]:
    return {"id": mission.id, "title": mission.title, "goal": mission.goal, "status": mission.status, "cron": mission.cron, "timezone": mission.timezone,
            "next_run_at": mission.next_run_at.isoformat() + "Z" if mission.next_run_at else None, "run_count": mission.run_count,
            "steps": [{"id": s.id, "idx": s.idx, "title": s.title, "handler": s.handler, "status": s.status, "attempts": s.attempts, "max_attempts": s.max_attempts,
                       "depends_on": json.loads(s.depends_on or "[]"), "result": s.result, "error": s.error, "approval_id": s.approval_id} for s in steps]}


def get(owner: Optional[str], mission_id: str) -> Dict[str, Any]:
    with session_scope() as db:
        q = db.query(Mission).filter(Mission.id == mission_id)
        if owner is not None:
            q = q.filter(Mission.owner == (owner or ""))
        mission = q.first()
        if mission is None:
            raise LookupError("missao nao encontrada")
        return _view(mission, db.query(MissionStep).filter(MissionStep.mission_id == mission.id).order_by(MissionStep.idx).all())


def list_missions(owner: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = db.query(Mission)
        if owner is not None:
            q = q.filter(Mission.owner == (owner or ""))
        missions = q.order_by(Mission.created_at.desc()).limit(limit).all()
        return [_view(m, db.query(MissionStep).filter(MissionStep.mission_id == m.id).order_by(MissionStep.idx).all()) for m in missions]


def cancel(owner: Optional[str], mission_id: str) -> None:
    with session_scope() as db:
        q = db.query(Mission).filter(Mission.id == mission_id)
        if owner is not None:
            q = q.filter(Mission.owner == (owner or ""))
        mission = q.first()
        if mission is None:
            raise LookupError("missao nao encontrada")
        mission.status, mission.next_run_at, mission.updated_at = "cancelled", None, utcnow()
        db.query(MissionStep).filter(MissionStep.mission_id == mission.id, MissionStep.status.in_(("pending", "waiting_approval"))).update({"status": "cancelled"}, synchronize_session=False)
    audit.record("mission.cancel", owner=owner, risk="reversible", target=mission_id)


def _approved_step(owner: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Approval flips the step back to pending; the worker runs it with `approved` set."""
    with session_scope() as db:
        updated = db.query(MissionStep).filter(MissionStep.id == payload.get("step_id"), MissionStep.status == "waiting_approval").update({"status": "pending", "retry_at": None, "approval_id": "approved"}, synchronize_session=False)
    if not updated:
        raise LookupError("passo nao aguarda aprovacao")
    return {"resumed": payload["step_id"]}


policy.register_executor("mission.step", _approved_step)


def _activate_due(now: datetime) -> int:
    """Start a new run of due recurring missions. next_run_at moves first, atomically, so one firing starts one run."""
    started = 0
    with session_scope() as db:
        due = [(m.id, m.cron, m.timezone, m.next_run_at) for m in db.query(Mission).filter(Mission.status == "active", Mission.cron.isnot(None), Mission.next_run_at <= now).all()]
        for mission_id, cron, tz, previous in due:
            if not db.query(Mission).filter(Mission.id == mission_id, Mission.next_run_at == previous).update({"next_run_at": next_run(cron, tz, now), "run_count": Mission.run_count + 1}, synchronize_session=False):
                continue
            if db.query(MissionStep).filter(MissionStep.mission_id == mission_id, MissionStep.status.in_(("running", "waiting_approval"))).count():
                continue  # the previous run is still going: skip, don't overlap
            db.query(MissionStep).filter(MissionStep.mission_id == mission_id).update({"status": "pending", "attempts": 0, "retry_at": None, "error": "", "approval_id": None}, synchronize_session=False)
            started += 1
    return started


def _recover_leases(now: datetime) -> int:
    recovered = 0
    with session_scope() as db:
        for step in db.query(MissionStep).filter(MissionStep.status == "running", MissionStep.lease_until < now).all():
            handler = REGISTRY.get(step.handler)
            if handler and handler.idempotent and step.attempts < step.max_attempts:
                step.status, step.retry_at, step.error = "pending", now, "retomado apos interrupcao"
            else:
                step.status, step.error = "failed", "interrompido; nao repetido automaticamente porque pode ter tido efeito externo"
            recovered += 1
    return recovered


def _runnable(limit: int, now: datetime) -> List[str]:
    claimed: List[str] = []
    with session_scope() as db:
        rows = (db.query(MissionStep, Mission).join(Mission, MissionStep.mission_id == Mission.id)
                .filter(MissionStep.status == "pending", Mission.status.in_(("active", "waiting_approval"))).order_by(MissionStep.idx).limit(200).all())
        statuses: Dict[str, Dict[int, str]] = {}
        for step, _mission in rows:
            if step.retry_at and step.retry_at > now:
                continue
            siblings = statuses.setdefault(step.mission_id, {s.idx: s.status for s in db.query(MissionStep.idx, MissionStep.status).filter(MissionStep.mission_id == step.mission_id).all()})
            deps = json.loads(step.depends_on or "[]")
            if any(siblings.get(d) in ("failed", "cancelled") for d in deps):
                step.status, step.error = "cancelled", "dependencia nao concluida"
                siblings[step.idx] = "cancelled"
                continue
            if not all(siblings.get(d) == "succeeded" for d in deps):
                continue
            if db.query(MissionStep).filter(MissionStep.id == step.id, MissionStep.status == "pending").update(
                    {"status": "running", "lease_until": now + LEASE, "lease_owner": "worker", "attempts": MissionStep.attempts + 1}, synchronize_session=False):
                claimed.append(step.id)
                if len(claimed) >= limit:
                    break
    return claimed


async def _run_step(step_id: str) -> str:
    with session_scope() as db:
        step = db.get(MissionStep, step_id)
        mission = db.get(Mission, step.mission_id)
        name, params, owner, attempts, max_attempts, approved, mission_id, idx = step.handler, json.loads(step.params or "{}"), mission.owner, step.attempts, step.max_attempts, step.approval_id == "approved", mission.id, step.idx
        previous = {s.idx: s.result for s in db.query(MissionStep).filter(MissionStep.mission_id == mission_id, MissionStep.status == "succeeded").all()}
    handler = REGISTRY.get(name)
    if handler is None:
        _finish(step_id, "failed", error=f"acao desconhecida: {name}")
        return "failed"
    decision = policy.evaluate(f"mission.{name}", handler.risk, owner)
    if decision.mode == policy.DENY:
        _finish(step_id, "failed", error=f"negado pela politica: {decision.reason}")
        return "failed"
    if decision.mode == policy.CONFIRM and not approved:
        approval = policy.request_approval("mission.step", handler.risk, owner, f"Missao: executar '{name}'", {"step_id": step_id})
        with session_scope() as db:
            row = db.get(MissionStep, step_id)
            row.status, row.approval_id, row.attempts = "waiting_approval", approval, max(0, row.attempts - 1)
            db.get(Mission, mission_id).status = "waiting_approval"
        return "waiting_approval"
    try:
        result = handler.fn(owner, params, {"previous": previous, "mission_id": mission_id, "idx": idx})
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        safe = redact_text(exc)[:500]
        if handler.idempotent and attempts < max_attempts:
            with session_scope() as db:
                row = db.get(MissionStep, step_id)
                row.status, row.error, row.retry_at = "pending", safe, utcnow() + timedelta(seconds=BACKOFF_SECONDS * 2 ** attempts)
            return "retry"
        _finish(step_id, "failed", error=safe)
        return "failed"
    _finish(step_id, "succeeded", result=redact_text(json.dumps(result, ensure_ascii=False, default=str))[:4000])
    audit.record(f"mission.{name}", owner=owner, risk=handler.risk, target=mission_id)
    return "succeeded"


def _finish(step_id: str, status: str, *, result: str = "", error: str = "") -> None:
    with session_scope() as db:
        row = db.get(MissionStep, step_id)
        row.status, row.result, row.error, row.lease_until, row.updated_at = status, result, error, None, utcnow()


def _settle_missions() -> List[Dict[str, Any]]:
    finished: List[Dict[str, Any]] = []
    with session_scope() as db:
        for mission in db.query(Mission).filter(Mission.status.in_(("active", "waiting_approval"))).all():
            statuses = [s.status for s in db.query(MissionStep.status).filter(MissionStep.mission_id == mission.id).all()]
            if "waiting_approval" in statuses:
                mission.status = "waiting_approval"
            elif mission.status == "waiting_approval":
                mission.status = "active"
            if mission.cron or any(s not in TERMINAL for s in statuses):
                continue
            mission.status = "completed" if all(s == "succeeded" for s in statuses) else "failed"
            mission.updated_at = utcnow()
            finished.append({"id": mission.id, "owner": mission.owner, "title": mission.title, "status": mission.status})
    return finished


async def tick(limit: int = 10) -> Dict[str, int]:
    """One worker pass. Safe to run concurrently and after a crash."""
    now = utcnow()
    stats = {"activated": _activate_due(now), "recovered": _recover_leases(now), "ran": 0, "finished": 0}
    for step_id in _runnable(limit, now):
        await _run_step(step_id)
        stats["ran"] += 1
    finished = _settle_missions()
    stats["finished"] = len(finished)
    for mission in finished:
        audit.record("mission.finished", owner=mission["owner"], target=mission["id"], outcome=mission["status"])
        try:
            from src.jarvis.notify import notify
            await notify(mission["owner"], f"Missao {'concluida' if mission['status'] == 'completed' else 'falhou'}: {mission['title']}")
        except Exception as exc:
            logger.info("mission notification failed: %s", redact_text(exc))
    return stats


# --- planning --------------------------------------------------------------------

def handler_catalog() -> List[Dict[str, Any]]:
    return [{"name": n, "risk": h.risk, "description": h.description, "params": list(h.params)} for n, h in sorted(REGISTRY.items())]


def validate_plan(raw: Any) -> List[Dict[str, Any]]:
    """Accept only registered handlers and declared parameter names from a model's output."""
    if isinstance(raw, str):
        match = re.search(r"\[.*\]", raw, re.S)
        if not match:
            raise ValueError("plano sem lista JSON")
        raw = json.loads(match.group(0))
    if not isinstance(raw, list):
        raise ValueError("plano invalido")
    plan = []
    for i, step in enumerate(raw[:MAX_STEPS]):
        handler = REGISTRY.get(step.get("handler")) if isinstance(step, dict) else None
        if handler is None:
            raise ValueError(f"passo {i}: acao nao permitida")
        params = step.get("params") or {}
        if not isinstance(params, dict) or set(params) - set(handler.params):
            raise ValueError(f"passo {i}: parametros nao permitidos")
        plan.append({"title": str(step.get("title") or step["handler"])[:200], "handler": step["handler"], "params": params, "depends_on": step.get("depends_on") or []})
    return plan


def planner_messages(goal: str) -> List[Dict[str, Any]]:
    catalog = json.dumps(handler_catalog(), ensure_ascii=False)
    return [{"role": "system", "content": "Divida o objetivo do usuario em passos usando SOMENTE estas acoes. Responda apenas com uma lista JSON de objetos {title, handler, params, depends_on}, onde depends_on sao indices de passos anteriores.\nAcoes: " + catalog},
            {"role": "user", "content": goal[:2000]}]


async def plan_goal(owner: str, goal: str, llm: Callable[[List[Dict[str, Any]]], Awaitable[str]], *, cron: Optional[str] = None) -> Dict[str, Any]:
    return create(owner, goal[:80], validate_plan(await llm(planner_messages(goal))), goal=goal, cron=cron)


# --- built-in handlers -----------------------------------------------------------

async def _h_brief(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from src.jarvis.intents import _do_brief
    return _do_brief(owner)


async def _h_agenda(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from src.jarvis.intents import _do_agenda
    return _do_agenda({"day": params.get("day", "hoje")}, owner)


async def _h_notify(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, bool]:
    from src.jarvis.notify import notify
    body = params.get("body") or ""
    if params.get("from_step") is not None:
        raw = ctx["previous"].get(int(params["from_step"]), "")
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return await notify(owner, str(params.get("title") or "J.A.R.V.I.S."), str(body))


async def _h_consolidate(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, int]:
    from src.jarvis import memory
    return memory.consolidate(owner)


async def _h_gmail_triage(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.jarvis.google import gmail
    out = await gmail.triage(owner, apply_labels=bool(params.get("apply_labels")))
    return {"processed": out["processed"], "important": [m["subject"] for m in out["messages"] if m["important"]]}


async def _h_gmail_important(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from src.jarvis.google import gmail
    out = await gmail.triage(owner)
    wanted = params.get("category")
    subjects = [m["subject"] for m in out["messages"] if m["important"] or (wanted and m["category"] == wanted)]
    return "Novos e-mails importantes:\n- " + "\n- ".join(subjects) if subjects else "Nenhum e-mail importante novo."


async def _h_calendar_sync(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, int]:
    from src.jarvis.google import calendar
    return await calendar.sync(owner)


async def _h_drive_sync(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, int]:
    from src.jarvis.google import drive
    return await drive.sync_changes(owner)


async def _h_home(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Runs its own policy check using the device's real risk."""
    from src.jarvis import home
    device = next((d for d in home.list_devices(owner) if d["entity_id"] == params.get("entity_id")), None)
    if device is None:
        raise LookupError("dispositivo nao encontrado")
    return await home.execute(device, params.get("op", ""), params.get("value"), owner=owner)


async def _h_memory_note(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.jarvis import memory
    return memory.remember(str(params.get("text", "")), owner=owner, kind="episodic", category="missao", source_type="system")["item"]


async def _h_llm(owner: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from src.jarvis.llm import complete
    return await complete(owner, [{"role": "user", "content": str(params.get("prompt", ""))[:8000]}], purpose="mission")


register("agenda.brief", _h_brief, description="Resumo do dia: data, compromissos e pendencias")
register("agenda.day", _h_agenda, description="Compromissos de um dia", params=("day",))
register("notify", _h_notify, policy.REVERSIBLE, idempotent=False, description="Notifica o usuario; from_step usa o resultado de um passo", params=("title", "body", "from_step"))
register("memory.consolidate", _h_consolidate, policy.REVERSIBLE, description="Consolida memorias duplicadas")
register("memory.note", _h_memory_note, policy.REVERSIBLE, description="Registra um fato episodico", params=("text",))
register("gmail.triage", _h_gmail_triage, policy.REVERSIBLE, description="Classifica e-mails novos, sem enviar nada", params=("apply_labels",))
register("gmail.important", _h_gmail_important, policy.REVERSIBLE, description="Lista e-mails novos importantes", params=("category",))
register("calendar.sync", _h_calendar_sync, policy.REVERSIBLE, description="Sincroniza o Google Calendar")
register("drive.sync", _h_drive_sync, policy.REVERSIBLE, description="Sincroniza pastas autorizadas do Drive")
register("home.control", _h_home, policy.REVERSIBLE, idempotent=False, description="Aciona dispositivo; a politica do dispositivo tambem se aplica", params=("entity_id", "op", "value"))
register("llm.prompt", _h_llm, policy.REVERSIBLE, description="Pergunta a um modelo escolhido pelo roteador", params=("prompt",))
