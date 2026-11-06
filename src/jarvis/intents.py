"""Deterministic commands: answered from code and databases, spending no tokens.

Patterns are strict on purpose. Anything not clearly matched returns None and
goes to the LLM as usual, so normal conversation is never hijacked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple

from src.jarvis import agenda, audit, config, home, learning, memory, policy

_WEEKDAYS = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}
_WEEKDAY_NAMES = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_NUMBERS = {"uma": 1, "um": 1, "duas": 2, "dois": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "meia": 0.5}
_DAY = r"(hoje|amanha|depois de amanha|(?:n[ao] )?(?:segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?|(?:esta|essa|nesta|nessa|proxima) semana)"
_AGENDA = re.compile(
    rf"^(?:(?:quais|que) (?:sao )?(?:os |meus |os meus )?(?:compromissos|eventos|reunioes)(?: (?:que )?(?:eu )?(?:tenho|tem))?|"
    rf"(?:o que|oque) (?:eu )?(?:tenho|tem)(?: marcado| agendado| na agenda)?|"
    rf"(?:eu )?tenho (?:algo|alguma coisa|algum compromisso|alguma reuniao|compromissos?|reunioes?)(?: marcad[oa]s?| agendad[oa]s?)?|"
    rf"(?:como esta |qual e )?(?:a )?minha agenda(?: de| para| d[ao])?) ?(?:para |de |d[ao] )?{_DAY}$")
_CONFLICT = re.compile(rf"^(?:verifique se |existe |ha |tem |tenho )?(?:tenho )?(?:algum |alguns )?conflitos?(?: de agenda| na (?:minha )?agenda)?(?: para | de )?(?:{_DAY})?$")
_FREE = re.compile(r"^(?:encontre|ache|procure|busque|sugira|tem|ha|existe)(?: um| algum)? (?:horario|espaco|tempo) (?:livre|disponivel)(?: de (\d+(?:[.,]\d)?|um|uma|duas|dois|tres|quatro|cinco|seis|meia) ?(?:h|hora|horas|min|minuto|minutos))?(?: (?:para|na|nesta|essa|esta|na proxima|proxima) ?(semana|hoje|amanha|proxima semana))?$")
_RECALL = re.compile(r"^(?:o que|oque) (?:voce )?(?:sabe|lembra|se lembra) (?:sobre|de|do|da) (.{2,80})$")
_APPROVALS = re.compile(r"^(?:quais |o que |tem |ha |existe )?.*(?:aprovac(?:ao|oes)|aprovar)(?: pendentes?)?.*$")
_DAILY = re.compile(r"^(?:todos os dias|todo dia)(?: as| a)? (\d{1,2})(?::(\d{2})| ?h(?:oras?)?(?: e (\d{1,2}))?)? ?(?:me )?(?:informe|avise|diga|mostre|envie|fale)?.*(?:compromissos|agenda)")
_WEEKLY_AGENDA = re.compile(r"^(?:toda|todo) (segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?(?: as| a)? ?(\d{1,2})?.*(?:compromissos|agenda)")
_MONITOR_MAIL = re.compile(r"^monitore (?:os |meus |os meus )?e ?-?mails?(?: de (\w+))?$")
_REMEMBER = re.compile(r"^(?:por favor,? )?(?:lembre|lembra|memorize|guarde|grave|anote|esqueca)\b")


@dataclass
class Intent:
    name: str
    params: Dict[str, Any] = field(default_factory=dict)


def _strip(text: str) -> Tuple[str, bool]:
    norm = memory.normalize(text).rstrip(" .!?")
    match = re.match(r"^jarvis[,: ]+", norm)
    return (norm[match.end():] if match else norm), bool(match)


def parse(text: str) -> Optional[Intent]:
    if not text or len(text) > 200 or "\n" in text.strip():
        return None
    norm, addressed = _strip(text)
    if addressed and re.fullmatch(r"(bom dia|boa tarde|boa noite|ola|oi)", norm):
        return Intent("brief")
    match = _DAILY.match(norm)
    if match:
        return Intent("routine", {"cron": f"{int(match.group(2) or match.group(3) or 0)} {int(match.group(1))} * * *", "title": "Compromissos do dia"})
    match = _WEEKLY_AGENDA.match(norm)
    if match:
        day = (_WEEKDAYS[match.group(1)] + 1) % 7  # cron: 0 = domingo
        return Intent("routine", {"cron": f"0 {int(match.group(2) or 8)} * * {day}", "title": f"Agenda de {match.group(1)}"})
    match = _MONITOR_MAIL.match(norm)
    if match:
        return Intent("monitor_mail", {"category": (match.group(1) or "").capitalize()})
    match = _AGENDA.match(norm)
    if match:
        return Intent("agenda", {"day": match.group(1)})
    match = _CONFLICT.match(norm)
    if match and "conflito" in norm:
        return Intent("conflicts", {"day": match.group(1) or "esta semana"})
    match = _FREE.match(norm)
    if match:
        return Intent("free_slot", {"amount": match.group(1) or "1", "unit_minutes": "min" in norm.split(match.group(1) or "\0")[-1][:6] if match.group(1) else False, "when": match.group(2) or "semana"})
    match = _RECALL.match(norm)
    if match:
        return Intent("recall", {"topic": match.group(1)})
    if addressed and _APPROVALS.match(norm):
        return Intent("approvals")
    if addressed and _REMEMBER.match(norm):
        return Intent("learn")
    if norm.split(" ", 1)[0] in {"ligue", "ligar", "liga", "desligue", "desligar", "desliga", "acenda", "apague", "abra", "feche", "tranque", "destranque", "ajuste", "coloque", "defina", "mude"}:
        return Intent("device")
    return None


def _day_range(day: str) -> Tuple[datetime, datetime, str]:
    now = agenda.now_local()
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    day = re.sub(r"^(?:n[ao]) ", "", day).replace("-feira", "")
    if day == "hoje":
        return now, midnight + timedelta(days=1), "hoje"
    if day == "amanha":
        return midnight + timedelta(days=1), midnight + timedelta(days=2), "amanhã"
    if day == "depois de amanha":
        return midnight + timedelta(days=2), midnight + timedelta(days=3), "depois de amanhã"
    if day in _WEEKDAYS:
        delta = (_WEEKDAYS[day] - now.weekday()) % 7 or 7
        start = midnight + timedelta(days=delta)
        return start, start + timedelta(days=1), f"{_WEEKDAY_NAMES[_WEEKDAYS[day]]} ({start.strftime('%d/%m')})"
    if "próxima" in day:
        start = midnight + timedelta(days=7 - now.weekday())
        return start, start + timedelta(days=7), "a próxima semana"
    return now, midnight + timedelta(days=7 - now.weekday()), "esta semana"


def _minutes(params: Dict[str, Any]) -> int:
    raw = str(params.get("amount", "1"))
    value = _NUMBERS.get(raw)
    if value is None:
        value = float(raw.replace(",", "."))
    return max(5, int(value if params.get("unit_minutes") else value * 60))


def _do_agenda(params: Dict[str, Any], owner: str) -> str:
    start, end, label = _day_range(params["day"])
    events = agenda.events_between(owner, start, end)
    multi_day = (end - start) > timedelta(days=1)
    if not events:
        return f"Nada na agenda para {label}."
    text = f"Agenda para {label}:\n{agenda.format_events(events, with_day=multi_day)}"
    conflicts = agenda.find_conflicts(events)
    if conflicts:
        text += f"\n\nAtencao: {len(conflicts)} conflito(s) de horário."
    return text


def _do_conflicts(params: Dict[str, Any], owner: str) -> str:
    start, end, label = _day_range(params["day"])
    conflicts = agenda.find_conflicts(agenda.events_between(owner, start, end))
    if not conflicts:
        return f"Nenhum conflito de agenda para {label}."
    lines = [f"- {a['start'].strftime('%d/%m %H:%M')}: {a['title']} x {b['title']} ({b['start'].strftime('%H:%M')})" for a, b in conflicts]
    return f"{len(conflicts)} conflito(s) em {label}:\n" + "\n".join(lines)


def _do_free_slot(params: Dict[str, Any], owner: str) -> str:
    minutes = _minutes(params)
    when = params.get("when", "semana")
    start, end, label = _day_range({"hoje": "hoje", "amanha": "amanha", "próxima semana": "próxima semana"}.get(when, "esta semana"))
    if end - start < timedelta(days=1) and when == "semana":
        start, end, label = _day_range("próxima semana")
    slots = agenda.free_slots(agenda.events_between(owner, start, end), start, end, minutes)
    if not slots:
        return f"Não encontrei {minutes} minutos livres em {label}, no horário comercial."
    lines = [f"- {a.strftime('%d/%m')} ({_WEEKDAY_NAMES[a.weekday()]}) {a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in slots]
    return f"Horários livres de pelo menos {minutes} minutos em {label}:\n" + "\n".join(lines)


def _do_brief(owner: str) -> str:
    now = agenda.now_local()
    greeting = "Bom dia" if now.hour < 12 else ("Boa tarde" if now.hour < 18 else "Boa noite")
    parts = [f"{greeting}. Hoje é {_WEEKDAY_NAMES[now.weekday()]}, {now.strftime('%d/%m/%Y, %H:%M')}."]
    try:
        midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo) + timedelta(days=1)
        events = agenda.events_between(owner, now, midnight)
        parts.append(f"Compromissos restantes hoje:\n{agenda.format_events(events)}" if events else "Sem compromissos restantes hoje.")
        conflicts = agenda.find_conflicts(events)
        if conflicts:
            parts.append(f"Há {len(conflicts)} conflito(s) de horário.")
    except Exception:
        parts.append("Não consegui consultar a agenda agora.")
    pending = policy.list_approvals(owner)
    if pending:
        parts.append(f"{len(pending)} ação(oes) aguardando sua aprovação.")
    candidates = learning.list_candidates(owner)
    if candidates:
        parts.append(f"{len(candidates)} item(ns) aguardando revisão na memória.")
    return "\n\n".join(parts)


def _do_recall(params: Dict[str, Any], owner: str) -> str:
    hits = memory.search(params["topic"], owner=owner, k=5, touch=True)
    if not hits:
        return f"Não tenho nada guardado sobre {params['topic']}."
    return "O que tenho guardado:\n" + "\n".join(f"- {h['text']}" for h in hits)


def _do_approvals(owner: str) -> str:
    pending = policy.list_approvals(owner)
    if not pending:
        return "Nenhuma ação aguardando aprovação."
    return "Aguardando sua aprovação:\n" + "\n".join(f"- {p['summary']}" for p in pending[:10])


def _do_learn(text: str, owner: str) -> Optional[str]:
    result = learning.observe_user_message(text, owner=owner)
    if result["remembered"]:
        entry = result["remembered"][0]
        if entry["action"] == "conflict":
            return f"Guardado, mas isso conflita com: \"{entry['conflicts'][0]['text']}\". Qual está correto?"
        return "Já estava guardado." if entry["action"] == "duplicate" else f"Entendido, vou lembrar: {entry['item']['text']}"
    if result["forgotten"]:
        return "Pronto, esqueci."
    return None if result["candidates"] else "Não encontrei nada correspondente para esquecer."


def _do_routine(params: Dict[str, Any], owner: str) -> str:
    from src.jarvis import missions
    mission = missions.create(owner, params["title"], [{"title": "Consultar agenda", "handler": "agenda.brief"},
                                                       {"title": "Notificar", "handler": "notify", "params": {"title": params["title"], "from_step": 0}, "depends_on": [0]}],
                              cron=params["cron"], tz=config.get("timezone", "America/Sao_Paulo"))
    return f"Rotina criada: {params['title']}. Próxima execução em {_local(mission['next_run_at'])}."


def _do_monitor_mail(params: Dict[str, Any], owner: str) -> str:
    from src.jarvis import missions
    category = {"Trabalho": "Trabalho", "Pessoal": "Pessoal", "Financeiro": "Financeiro", "Viagens": "Viagens"}.get(params["category"])
    step_params = {"category": category} if category else {}
    missions.create(owner, f"Monitorar e-mails{' de ' + category if category else ''}",
                    [{"title": "Verificar e-mails novos", "handler": "gmail.important", "params": step_params},
                     {"title": "Notificar", "handler": "notify", "params": {"title": "E-mails", "from_step": 0}, "depends_on": [0]}],
                    cron="*/15 * * * *", tz=config.get("timezone", "America/Sao_Paulo"))
    return "Vou verificar a cada 15 minutos e avisar sobre os importantes. Nada será respondido sem sua aprovação."


def _local(iso: Optional[str]) -> str:
    if not iso:
        return "data desconhecida"
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(agenda.local_tz())
    return moment.strftime("%d/%m as %H:%M")


async def _do_device(text: str, owner: str) -> Optional[str]:
    cmd = home.parse_command(text, owner)
    if not cmd:
        return None  # not one of the registered devices: let the LLM handle it
    device = cmd["device"]
    try:
        await home.execute(device, cmd["op"], cmd["value"], owner=owner)
        return f"Feito: {device['name']}."
    except policy.ApprovalRequired:
        return f"Essa ação em {device['name']} precisa da sua aprovação. Confirme no painel do Jarvis."
    except policy.ActionDenied:
        return f"A política atual não permite essa ação em {device['name']}."
    except home.UnsafeAction as exc:
        return f"Não executei: {exc}."
    except home.HomeNotConfigured:
        return "O Home Assistant ainda não está configurado."
    except Exception as exc:
        audit.record("home.control", owner=owner, risk="reversible", outcome="error", error=str(exc)[:200])
        return f"Não consegui falar com o dispositivo {device['name']}."


async def handle(text: str, owner: Optional[str] = "") -> Optional[Dict[str, Any]]:
    """Return {"intent", "text"} if handled directly, else None. Never raises to the caller."""
    owner = owner or ""
    if not config.get("enabled", True) or not config.get("intents.enabled", True):
        return None
    intent = parse(text)
    if intent is None:
        return None
    try:
        if intent.name == "agenda":
            answer: Optional[str] = _do_agenda(intent.params, owner)
        elif intent.name == "conflicts":
            answer = _do_conflicts(intent.params, owner)
        elif intent.name == "free_slot":
            answer = _do_free_slot(intent.params, owner)
        elif intent.name == "brief":
            answer = _do_brief(owner)
        elif intent.name == "recall":
            answer = _do_recall(intent.params, owner)
        elif intent.name == "approvals":
            answer = _do_approvals(owner)
        elif intent.name == "learn":
            answer = _do_learn(text, owner)
        elif intent.name == "routine":
            answer = _do_routine(intent.params, owner)
        elif intent.name == "monitor_mail":
            answer = _do_monitor_mail(intent.params, owner)
        else:
            answer = await _do_device(text, owner)
    except Exception as exc:
        audit.record("intent.error", owner=owner, outcome="error", intent=intent.name, error=str(exc)[:200])
        return None  # degrade to the normal LLM path
    if answer is None:
        return None
    audit.record("intent.direct", owner=owner, target=intent.name)
    return {"intent": intent.name, "text": answer}
