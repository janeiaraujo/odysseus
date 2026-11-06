import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.jarvis import config, memory, missions, notify, observability, policy, voice, worker
from src.jarvis.db import configure, session_scope, utcnow
from src.jarvis.models import Mission, MissionStep


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    memory._vector_failed = True
    saved = dict(missions.REGISTRY)
    calls = []

    async def ok(owner, params, ctx):
        calls.append(("ok", owner, params, dict(ctx["previous"])))
        return {"n": len(calls)}
    missions.register("t.ok", ok, policy.READ, params=("x",))
    missions.calls = calls
    yield
    missions.REGISTRY.clear()
    missions.REGISTRY.update(saved)
    memory._vector_failed = False


def _tick(n=1):
    for _ in range(n):
        out = asyncio.run(missions.tick())
    return out


def test_dependencies_run_in_order_and_results_flow():
    m = missions.create("ana", "M", [{"handler": "t.ok", "params": {"x": 1}}, {"handler": "t.ok", "depends_on": [0]}, {"handler": "t.ok", "depends_on": [0, 1]}])
    _tick(4)
    done = missions.get("ana", m["id"])
    assert done["status"] == "completed" and all(s["status"] == "succeeded" for s in done["steps"])
    assert missions.calls[2][3][0] and missions.calls[2][3][1]


def test_failed_dependency_cancels_downstream_and_marks_failed():
    async def boom(owner, params, ctx):
        raise RuntimeError("falhou token=abc123secret")
    missions.register("t.boom", boom, policy.READ, idempotent=False)
    m = missions.create("ana", "M", [{"handler": "t.boom"}, {"handler": "t.ok", "depends_on": [0]}])
    _tick(3)
    done = missions.get("ana", m["id"])
    assert done["status"] == "failed" and [s["status"] for s in done["steps"]] == ["failed", "cancelled"]
    assert "abc123secret" not in done["steps"][0]["error"] and missions.calls == []


def test_idempotent_handler_retries_with_backoff_then_succeeds():
    attempts = []

    async def flaky(owner, params, ctx):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("rede")
        return "ok"
    missions.register("t.flaky", flaky, policy.READ)
    m = missions.create("ana", "M", [{"handler": "t.flaky"}])
    _tick()
    step = missions.get("ana", m["id"])["steps"][0]
    assert step["status"] == "pending" and step["attempts"] == 1
    _tick()
    assert len(attempts) == 1  # backoff respected
    with session_scope() as db:
        db.query(MissionStep).update({"retry_at": utcnow() - timedelta(seconds=1)})
    _tick()
    assert missions.get("ana", m["id"])["status"] == "completed" and len(attempts) == 2


def test_retry_limit_is_respected():
    async def boom(owner, params, ctx):
        raise RuntimeError("x")
    missions.register("t.boom", boom, policy.READ)
    m = missions.create("ana", "M", [{"handler": "t.boom", "max_attempts": 2}])
    for _ in range(3):
        _tick()
        with session_scope() as db:
            db.query(MissionStep).update({"retry_at": None})
    step = missions.get("ana", m["id"])["steps"][0]
    assert step["status"] == "failed" and step["attempts"] == 2


def test_task_survives_restart(jarvis_env):
    """Mandatory scenario 10."""
    m = missions.create("ana", "M", [{"handler": "t.ok"}], cron="0 8 * * *")
    configure(f"sqlite:///{jarvis_env / 'jarvis.db'}")  # new engine, as after a reboot
    again = missions.get("ana", m["id"])
    assert again["cron"] == "0 8 * * *" and again["next_run_at"]
    with session_scope() as db:
        db.query(Mission).update({"next_run_at": utcnow() - timedelta(minutes=1)})
    assert _tick()["activated"] == 1 and len(missions.calls) == 1


def test_crash_mid_step_idempotent_resumes_and_external_is_not_repeated():
    sent = []

    async def external(owner, params, ctx):
        sent.append(1)
    missions.register("t.send", external, policy.READ, idempotent=False)
    a = missions.create("ana", "A", [{"handler": "t.ok"}])
    b = missions.create("ana", "B", [{"handler": "t.send"}])
    with session_scope() as db:
        db.query(MissionStep).update({"status": "running", "attempts": 1, "lease_until": utcnow() - timedelta(minutes=1)})
    _tick(2)
    assert missions.get("ana", a["id"])["status"] == "completed"
    assert missions.get("ana", b["id"])["steps"][0]["status"] == "failed" and sent == []


def test_cron_firing_runs_once_and_does_not_overlap():
    m = missions.create("ana", "M", [{"handler": "t.ok"}], cron="*/5 * * * *")
    with session_scope() as db:
        db.query(Mission).update({"next_run_at": utcnow() - timedelta(minutes=1)})
    assert _tick()["activated"] == 1
    assert _tick()["activated"] == 0
    assert len(missions.calls) == 1
    with session_scope() as db:
        db.query(Mission).update({"next_run_at": utcnow() - timedelta(minutes=1)})
        db.query(MissionStep).update({"status": "running", "lease_until": utcnow() + timedelta(minutes=5)})
    assert _tick()["activated"] == 0 and missions.get("ana", m["id"])["steps"][0]["status"] == "running"


def test_cron_uses_user_timezone():
    moment = datetime(2026, 11, 6, 12, 0)  # 09:00 in Sao Paulo
    assert missions.next_run("0 8 * * *", "America/Sao_Paulo", moment) == datetime(2026, 11, 7, 11, 0)


def test_risky_step_waits_for_approval_then_runs_exactly_once():
    ran = []

    async def external(owner, params, ctx):
        ran.append(1)
    missions.register("t.ext", external, policy.EXTERNAL, idempotent=False)
    m = missions.create("ana", "M", [{"handler": "t.ext"}, {"handler": "t.ok", "depends_on": [0]}])
    _tick(2)
    state = missions.get("ana", m["id"])
    assert state["status"] == "waiting_approval" and ran == [] and missions.calls == []
    asyncio.run(policy.decide(state["steps"][0]["approval_id"], True, owner="ana"))
    _tick(3)
    assert len(ran) == 1 and missions.get("ana", m["id"])["status"] == "completed" and len(missions.calls) == 1


def test_rejected_approval_keeps_step_blocked():
    async def external(owner, params, ctx):
        raise AssertionError("nao deveria rodar")
    missions.register("t.ext", external, policy.EXTERNAL, idempotent=False)
    m = missions.create("ana", "M", [{"handler": "t.ext"}])
    _tick()
    asyncio.run(policy.decide(missions.get("ana", m["id"])["steps"][0]["approval_id"], False, owner="ana"))
    _tick(2)
    assert missions.get("ana", m["id"])["steps"][0]["status"] == "waiting_approval"
    missions.cancel("ana", m["id"])
    assert missions.get("ana", m["id"])["status"] == "cancelled"


def test_cancel_stops_pending_work_and_is_owner_scoped():
    m = missions.create("ana", "M", [{"handler": "t.ok"}])
    with pytest.raises(LookupError):
        missions.cancel("bob", m["id"])
    with pytest.raises(LookupError):
        missions.get("bob", m["id"])
    missions.cancel("ana", m["id"])
    _tick()
    assert missions.calls == []


@pytest.mark.parametrize("steps", [[], [{"handler": "nao.existe"}], [{"handler": "t.ok", "depends_on": [0]}], [{"handler": "t.ok", "depends_on": [5]}], [{"handler": "t.ok", "params": "x"}]])
def test_invalid_mission_rejected(steps):
    with pytest.raises(ValueError):
        missions.create("ana", "M", steps)


def test_invalid_cron_rejected():
    with pytest.raises(ValueError):
        missions.create("ana", "M", [{"handler": "t.ok"}], cron="todo dia")


def test_model_plan_can_only_use_registered_actions_and_params():
    good = '```json\n[{"title": "a", "handler": "t.ok", "params": {"x": 1}}]\n```'
    assert missions.validate_plan(good)[0]["handler"] == "t.ok"
    with pytest.raises(ValueError):
        missions.validate_plan('[{"handler": "shell.run", "params": {"cmd": "rm -rf /"}}]')
    with pytest.raises(ValueError):
        missions.validate_plan('[{"handler": "t.ok", "params": {"cmd": "x"}}]')
    with pytest.raises(ValueError):
        missions.validate_plan("nao e json")


def test_plan_goal_creates_mission_from_llm_output():
    async def llm(messages):
        assert "t.ok" in messages[0]["content"]
        return '[{"handler": "t.ok"}, {"handler": "t.ok", "depends_on": [0]}]'
    m = asyncio.run(missions.plan_goal("ana", "organize a semana", llm))
    assert len(m["steps"]) == 2 and m["goal"] == "organize a semana"


def test_notify_stores_inapp_and_pushes_ntfy(monkeypatch):
    from src import integrations
    monkeypatch.setattr(integrations, "load_integrations", lambda: [{"preset": "ntfy", "id": "ntfy", "name": "ntfy", "base_url": "http://ntfy:80"}])
    monkeypatch.setattr("src.service_health._ntfy_integration", lambda items: items[0])
    posts = []
    out = asyncio.run(notify.notify("ana", "Oi", "corpo", transport=httpx.MockTransport(lambda r: posts.append(str(r.url)) or httpx.Response(200))))
    assert out["inapp"] and out["ntfy"] and posts == ["http://ntfy/jarvis"]
    assert notify.list_notifications("ana")[0]["title"] == "Oi" and notify.list_notifications("bob") == []
    assert notify.mark_read("ana") == 1 and notify.list_notifications("ana", unread_only=True) == []


def test_notify_survives_ntfy_outage(monkeypatch):
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [{"base_url": "http://ntfy"}])
    monkeypatch.setattr("src.service_health._ntfy_integration", lambda items: items[0])

    def down(request):
        raise httpx.ConnectError("x")
    out = asyncio.run(notify.notify("ana", "Oi", transport=httpx.MockTransport(down)))
    assert out["inapp"] and not out["ntfy"]


def test_worker_isolates_failures(monkeypatch):
    async def boom():
        raise RuntimeError("mission db locked")

    async def fine():
        return {"sent": 0}
    monkeypatch.setattr(worker.missions, "tick", boom)
    monkeypatch.setattr(worker.mailqueue, "tick", fine)
    out = asyncio.run(worker.run_once())
    assert "error" in out["missions"] and out["mail"] == {"sent": 0}


def test_worker_respects_inprocess_switch(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_INPROCESS_TASKS", "0")
    assert worker.enabled() is False


# --- voice ------------------------------------------------------------------------

def _tts(text):
    return "QVVESU8="


async def _llm(owner, text):
    return f"resposta para {text}"


def test_voice_deterministic_command_skips_model(monkeypatch):
    from src.jarvis import agenda
    monkeypatch.setattr(agenda, "events_between", lambda *a: [])

    async def no_llm(owner, text):
        raise AssertionError("nao deveria chamar modelo")
    out = asyncio.run(voice.turn("ana", audio=b"x", transcribe=lambda a: "Jarvis, tenho algo marcado amanhã?", llm=no_llm, tts=_tts))
    assert out["handled_by"] == "agenda" and "Nada na agenda" in out["reply"] and out["audio"] == "QVVESU8="


def test_voice_conversation_falls_to_model_and_keeps_transcript_only():
    out = asyncio.run(voice.turn("ana", audio=b"audio-bytes", transcribe=lambda a: "como vai voce", llm=_llm, tts=_tts))
    assert out["reply"] == "resposta para como vai voce"
    history = voice.history("ana")
    assert history[0]["transcript"] == "como vai voce" and "audio-bytes" not in json.dumps(history)
    assert voice.history("bob") == []
    assert voice.clear_history("ana") == 1 and voice.history("ana") == []


def test_wake_word_required_means_ignored_speech_leaves_no_trace():
    out = asyncio.run(voice.turn("ana", audio=b"x", require_wake_word=True, transcribe=lambda a: "conversa com outra pessoa", llm=_llm, tts=_tts))
    assert out["handled_by"] == "no_wake_word" and out["reply"] == "" and voice.history("ana") == []
    assert asyncio.run(voice.turn("ana", audio=b"x", require_wake_word=True, transcribe=lambda a: "Ei Jarvis, como vai?", llm=_llm, tts=_tts))["reply"] == "resposta para como vai?"


@pytest.mark.parametrize("heard", ["Djarvis, como vai?", "Jarves como vai?", "Ok jarviz, como vai?"])
def test_wake_word_survives_common_transcription_variants(heard):
    out = asyncio.run(voice.turn("ana", audio=b"x", require_wake_word=True, transcribe=lambda a: heard, llm=_llm, tts=_tts))
    assert out["handled_by"] == "llm" and out["reply"].endswith("como vai?")


def test_misspelled_name_still_reaches_direct_commands():
    out = asyncio.run(voice.turn("ana", audio=b"x", require_wake_word=True, transcribe=lambda a: "Jarves, lembre que uso oculos", llm=_llm, tts=_tts))
    assert out["handled_by"] == "learn"


def test_voice_cancel_and_silence():
    assert asyncio.run(voice.turn("ana", audio=b"x", transcribe=lambda a: "Jarvis, cancelar", llm=_llm, tts=_tts))["cancel"] is True
    assert asyncio.run(voice.turn("ana", audio=b"x", transcribe=lambda a: "", llm=_llm, tts=_tts))["handled_by"] == "silence"


def test_voice_unconfigured_is_a_clear_error(monkeypatch):
    class Off:
        available = False
    monkeypatch.setattr("services.stt.get_stt_service", lambda: Off())
    with pytest.raises(voice.VoiceUnavailable):
        voice._transcribe(b"x")


# --- observability -----------------------------------------------------------------

def test_snapshot_survives_all_services_down(monkeypatch):
    monkeypatch.setattr(observability, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(observability.router, "catalog", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    snap = observability.snapshot("ana")
    assert all(not s["online"] for s in snap["services"]) and snap["models"]["error"] == "RuntimeError"
    assert snap["resources"]["disk_total"] > 0 and snap["memory"]["total"] == 0


def test_snapshot_is_owner_scoped():
    missions.create("ana", "M", [{"handler": "t.ok"}])
    assert observability.snapshot("ana")["missions"] == {"active": 1}
    assert observability.snapshot("bob")["missions"] == {}
