import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.jarvis import agenda, audit, config, home, intents, learning, memory, policy

TZ = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(autouse=True)
def text_only():
    memory._vector_failed = True
    yield
    memory._vector_failed = False


def ev(title, start, end, all_day=False):
    return {"uid": title, "title": title, "location": "", "start": start, "end": end, "all_day": all_day}


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize("text,name", [
    ("Jarvis, quais compromissos tenho hoje?", "agenda"),
    ("Jarvis, tenho algo marcado amanhã?", "agenda"),
    ("o que tenho na sexta?", "agenda"),
    ("minha agenda da próxima semana", "agenda"),
    ("Jarvis, existe algum conflito na minha agenda?", "conflicts"),
    ("verifique se tenho algum conflito de agenda", "conflicts"),
    ("Jarvis, encontre um horário livre de duas horas.", "free_slot"),
    ("Jarvis, bom dia.", "brief"),
    ("o que você sabe sobre minha viagem", "recall"),
    ("Jarvis, lembre que prefiro respostas em português.", "learn"),
    ("Jarvis, ligue a televisão da sala.", "device"),
])
def test_parse_recognizes_commands(text, name):
    assert intents.parse(text).name == name


@pytest.mark.parametrize("text", [
    "bom dia", "como funciona um calendário juliano?", "Explique conflitos de agenda em empresas grandes",
    "Escreva um poema sobre o amanhã", "tenho medo de altura", "resuma meus e-mails importantes",
    "Jarvis, agende uma reunião para amanhã às 14 horas.", "linha 1\nlinha 2", "x" * 500,
])
def test_normal_conversation_is_not_hijacked(text):
    assert intents.parse(text) is None


def test_normalize_handles_iso_utc_and_all_day():
    events = agenda.normalize_events([{"uid": "1", "summary": "A", "dtstart": "2026-11-06T13:00:00Z", "dtend": "2026-11-06T14:00:00Z"},
                                      {"uid": "2", "summary": "B", "dtstart": "2026-11-05", "dtend": "2026-11-06"}])
    assert events[0]["all_day"] and events[1]["start"].hour == 10 and not events[1]["all_day"]


def test_find_conflicts_ignores_adjacent_and_all_day():
    events = [ev("A", at(9, 9), at(9, 10)), ev("B", at(9, 10), at(9, 11)), ev("C", at(9, 10, 30), at(9, 12)),
              ev("D", at(9, 0), at(10, 0), all_day=True)]
    pairs = agenda.find_conflicts(events)
    assert [(a["title"], b["title"]) for a, b in pairs] == [("B", "C")]


def test_free_slots_skip_busy_and_weekend():
    events = [ev("A", at(9, 8), at(9, 17))]  # Monday 9/Nov 2026
    slots = agenda.free_slots(events, at(7, 0), at(10, 23), 120)  # Sat 7 -> Tue 10
    assert slots[0][0] == at(10, 8) and all(a.weekday() < 5 for a, _ in slots)


def test_free_slot_found_between_events():
    events = [ev("A", at(9, 8), at(9, 10)), ev("B", at(9, 13), at(9, 18))]
    assert agenda.free_slots(events, at(9, 0), at(9, 23), 120) == [(at(9, 10), at(9, 13))]


def _run(text, owner="ana"):
    return asyncio.run(intents.handle(text, owner))


def test_agenda_query_is_answered_without_llm(monkeypatch):
    """Mandatory scenario 6."""
    captured = {}
    def fake(owner, start, end):
        captured["owner"] = owner
        return [ev("Dentista", start + timedelta(minutes=30), start + timedelta(minutes=60))]
    monkeypatch.setattr(agenda, "events_between", fake)
    out = _run("Jarvis, tenho algo marcado amanhã?")
    assert out["intent"] == "agenda" and "Dentista" in out["text"] and captured["owner"] == "ana"


def test_empty_agenda_message(monkeypatch):
    monkeypatch.setattr(agenda, "events_between", lambda *a: [])
    assert "Nada na agenda" in _run("quais compromissos tenho hoje")["text"]


def test_calendar_failure_falls_back_to_llm(monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(agenda, "events_between", boom)
    assert _run("quais compromissos tenho hoje") is None


def test_brief_survives_calendar_failure(monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(agenda, "events_between", boom)
    policy.request_approval("gmail.send", policy.EXTERNAL, "ana", "enviar relatorio", {})
    text = _run("Jarvis, bom dia")["text"]
    assert "Não consegui consultar" in text and "aguardando sua aprovação" in text


def test_remember_and_recall_round_trip():
    assert "vou lembrar" in _run("Jarvis, lembre que prefiro respostas em português.")["text"]
    assert "portugues" in memory.normalize(_run("o que você sabe sobre respostas")["text"])
    assert "Já estava" in _run("Jarvis, lembre que prefiro respostas em português.")["text"]


def test_disabled_config_returns_none():
    config.update_config({"intents": {"enabled": False}})
    assert _run("quais compromissos tenho hoje") is None


# --- home ---------------------------------------------------------------------

def _ha(calls):
    def handler(request):
        calls.append((request.method, request.url.path, request.read().decode(), request.headers.get("authorization")))
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "light.sala", "attributes": {"friendly_name": "Luz da Sala"}},
                                             {"entity_id": "lock.porta", "attributes": {}}, {"entity_id": "automation.x", "attributes": {}}])
        return httpx.Response(200, json=[])
    return httpx.MockTransport(handler)


@pytest.fixture
def devices():
    home.configure("http://ha.local:8123", "tok-secret")
    return {
        "tv": home.upsert_device("ana", name="Televisao", entity_id="media_player.tv", area="Sala"),
        "tv2": home.upsert_device("ana", name="Televisao", entity_id="media_player.tv2", area="Quarto"),
        "ar": home.upsert_device("ana", name="Ar condicionado", entity_id="climate.ar", area="Quarto"),
        "porta": home.upsert_device("ana", name="Porta da frente", entity_id="lock.porta", critical=True),
    }


def test_parse_command_uses_area_to_disambiguate(devices):
    assert home.parse_command("Jarvis, ligue a televisão da sala", "ana")["device"]["entity_id"] == "media_player.tv"
    assert home.parse_command("desligue a televisão do quarto", "ana")["device"]["entity_id"] == "media_player.tv2"


def test_parse_command_ambiguous_or_unknown_returns_none(devices):
    assert home.parse_command("ligue a televisão", "ana") is None
    assert home.parse_command("ligue o carro", "ana") is None


def test_safety_table_rejects_off_list_operations_and_ranges(devices):
    with pytest.raises(home.UnsafeAction):
        home.build_action(devices["tv"], "unlock")
    with pytest.raises(home.UnsafeAction):
        home.build_action({**devices["ar"]}, "set_temperature", 45)
    with pytest.raises(home.UnsafeAction):
        home.build_action({**devices["ar"]}, "set_temperature", "; rm -rf /")
    assert home.build_action(devices["ar"], "set_temperature", "22").data == {"temperature": 22.0}


def test_reversible_action_runs_and_calls_exact_service(devices):
    calls = []
    asyncio.run(home.execute(devices["tv"], "on", owner="ana", transport=_ha(calls)))
    method, path, body, auth = calls[0]
    assert (method, path) == ("POST", "/api/services/media_player/turn_on") and "media_player.tv" in body and auth == "Bearer tok-secret"


def test_critical_action_waits_for_human_even_with_auto_rule(devices):
    config.update_config({"autonomy": {"global": {"home.*": "auto", "physical_critical": "auto"}}})
    calls = []
    with pytest.raises(policy.ApprovalRequired):
        asyncio.run(home.execute(devices["porta"], "unlock", owner="ana", transport=_ha(calls)))
    assert calls == []


def test_user_flagged_critical_switch_needs_approval():
    plug = home.upsert_device("ana", name="Bomba", entity_id="switch.bomba", critical=True)
    assert home.build_action(plug, "on").risk == policy.PHYSICAL_CRITICAL


def test_discovery_registers_supported_only_and_flags_locks(devices):
    calls = []
    assert asyncio.run(home.discover("bob", transport=_ha(calls))) == {"discovered": 2}
    by_id = {d["entity_id"]: d for d in home.list_devices("bob")}
    assert by_id["lock.porta"]["critical"] and not by_id["light.sala"]["critical"] and "automation.x" not in by_id


def test_home_token_encrypted_at_rest(devices):
    assert "tok-secret" not in str(config.get("home"))


def test_device_intent_reports_pending_approval_and_never_acts(devices):
    out = _run("Jarvis, destranque a porta da frente")
    assert "aprovação" in out["text"] and policy.list_approvals("ana")


def test_device_of_another_owner_is_not_reachable(devices):
    assert home.parse_command("ligue a televisão da sala", "bob") is None


# --- real Odysseus calendar tables (temp database, not the user's) -------------------

@pytest.fixture
def real_calendar(monkeypatch):
    from tests.helpers.sqlite_db import make_temp_sqlite
    import core.database as cdb
    session_local, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", session_local)
    yield session_local
    engine.dispose()
    tmp.close()
    os.remove(tmp.name)


def _seed(session_local):
    from core.database import CalendarCal, CalendarEvent
    db = session_local()
    db.add(CalendarCal(id="c-ana", owner="ana", name="Ana"))
    db.add(CalendarCal(id="c-bob", owner="bob", name="Bob"))
    db.add(CalendarEvent(uid="e1", calendar_id="c-ana", summary="Consulta", dtstart=datetime(2026, 11, 9, 13, 0), dtend=datetime(2026, 11, 9, 14, 0), is_utc=True, rrule=""))
    db.add(CalendarEvent(uid="e2", calendar_id="c-ana", summary="Daily", dtstart=datetime(2026, 11, 2, 12, 0), dtend=datetime(2026, 11, 2, 12, 30), is_utc=True, rrule="FREQ=DAILY"))
    db.add(CalendarEvent(uid="e3", calendar_id="c-bob", summary="Segredo do Bob", dtstart=datetime(2026, 11, 9, 13, 0), dtend=datetime(2026, 11, 9, 14, 0), is_utc=True, rrule=""))
    db.commit()
    db.close()


def test_events_between_real_tables_owner_scoped_and_expands_recurrence(real_calendar):
    _seed(real_calendar)
    events = agenda.events_between("ana", at(9, 0), at(10, 0))
    titles = [e["title"] for e in events]
    assert "Consulta" in titles and "Daily" in titles and "Segredo do Bob" not in titles
    consulta = next(e for e in events if e["title"] == "Consulta")
    assert consulta["start"].hour == 10  # 13:00 UTC is 10:00 in Sao Paulo


def test_google_sync_writes_real_tables_additively(real_calendar):
    from core.database import CalendarEvent
    from src.jarvis.google import calendar as gcal
    _seed(real_calendar)
    out = gcal._apply_local("ana", "acc1", "Google", [
        {"id": "g1", "summary": "Voo", "status": "confirmed", "start": {"dateTime": "2026-11-09T15:00:00-03:00"}, "end": {"dateTime": "2026-11-09T16:00:00-03:00"}},
        {"id": "g2", "summary": "Feriado", "status": "confirmed", "start": {"date": "2026-11-15"}, "end": {"date": "2026-11-16"}}])
    assert out == {"created": 2, "updated": 0, "removed": 0}
    titles = [e["title"] for e in agenda.events_between("ana", at(9, 0), at(10, 0))]
    assert "Voo" in titles and "Consulta" in titles
    assert gcal._apply_local("ana", "acc1", "Google", [{"id": "g1", "status": "cancelled"}])["removed"] == 1
    db = real_calendar()
    assert db.get(CalendarEvent, "e1") is not None and db.get(CalendarEvent, "gcal:acc1:g1") is None
    assert db.get(CalendarEvent, "gcal:acc1:g2").origin == "google"
    db.close()
