from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.jarvis import config, persona, router
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import ModelUsage


def _ep(id_, name, url, models, kind="auto", tools=None):
    import json
    return SimpleNamespace(id=id_, name=name, base_url=url, cached_models=json.dumps(models), hidden_models=None,
                           pinned_models=None, model_type="llm", endpoint_kind=kind, supports_tools=tools)


ENDPOINTS = [
    _ep("o", "Ollama", "http://localhost:11434/v1", ["llama3.2", "nomic-embed-text"]),
    _ep("r", "OpenRouter", "https://openrouter.ai/api/v1", ["openrouter/free", "meta/llama-4:free", "openai/gpt-5.4"]),
    _ep("a", "Anthropic", "https://api.anthropic.com/v1", ["claude-opus-5-5"]),
]


def _names(models):
    return [m.model for m in models]


def test_catalog_identifies_provider_tier_and_capabilities():
    by = {m.model: m for m in router.catalog("", ENDPOINTS)}
    assert (by["llama3.2"].provider, by["llama3.2"].tier) == ("ollama", router.LOCAL)
    assert by["meta/llama-4:free"].tier == router.FREE and by["openrouter/free"].tier == router.FREE
    assert (by["claude-opus-5-5"].provider, by["claude-opus-5-5"].tier) == ("anthropic", router.PAID)
    assert "embeddings" in by["nomic-embed-text"].capabilities and "text" not in by["nomic-embed-text"].capabilities
    assert "vision" in by["claude-opus-5-5"].capabilities


def test_tier_treats_lookalike_public_host_as_paid():
    assert router.tier_for("https://localhost.evil.com/v1", "m") == router.PAID
    assert router.tier_for("http://10.0.0.5:8000/v1", "m") == router.LOCAL
    assert router.tier_for("https://proxy.example.com/v1", "m", "local") == router.LOCAL


def test_embeddings_never_selected_for_chat():
    assert "nomic-embed-text" not in _names(router.rank(profile="equilibrado", endpoints=ENDPOINTS))


def test_local_profile_is_local_only():
    assert _names(router.rank(profile="local", endpoints=ENDPOINTS)) == ["llama3.2"]


def test_economic_profile_never_includes_paid():
    assert all(m.tier != router.PAID for m in router.rank(profile="economico", endpoints=ENDPOINTS))


def test_balanced_prefers_local_then_free():
    ranked = router.rank(profile="equilibrado", endpoints=ENDPOINTS)
    assert ranked[0].tier == router.LOCAL and ranked[1].tier == router.FREE


def test_paid_model_not_selected_without_authorization(monkeypatch):
    monkeypatch.setattr(router, "_configured_chain", lambda owner: set())
    assert "claude-opus-5-5" not in _names(router.rank(profile="alta_qualidade", endpoints=ENDPOINTS))
    config.update_config({"router": {"paid_model_allowlist": ["claude-opus-5-5"]}})
    assert router.rank(profile="alta_qualidade", endpoints=ENDPOINTS)[0].model == "claude-opus-5-5"


def test_multimodal_requires_vision():
    config.update_config({"router": {"paid_model_allowlist": ["claude-opus-5-5"]}})
    models = router.rank(profile="multimodal", endpoints=ENDPOINTS)
    assert models and all("vision" in m.capabilities for m in models)


def test_unavailable_model_does_not_break_selection(monkeypatch):
    monkeypatch.setattr(router, "_configured_chain", lambda owner: set())
    for _ in range(3):
        router.record_usage(url="http://localhost:11434/v1", model="llama3.2", ok=False, error="connection refused")
    top = router.select(profile="equilibrado", endpoints=ENDPOINTS)
    assert top is not None and top.model != "llama3.2"
    with session_scope() as db:
        db.query(ModelUsage).update({"ts": utcnow() - timedelta(minutes=6)})
    assert router.select(profile="equilibrado", endpoints=ENDPOINTS).model == "llama3.2"


def test_rate_limit_cools_model_down(monkeypatch):
    router.record_usage(url="http://localhost:11434/v1", model="llama3.2", ok=False, error="HTTP 429 too many")
    assert "llama3.2" not in _names(router.rank(profile="equilibrado", endpoints=ENDPOINTS))


def test_configured_rpm_limit():
    config.update_config({"router": {"rate_limit_rpm": {"llama3.2": 1}}})
    router.record_usage(url="http://localhost:11434/v1", model="llama3.2", ok=True, latency_ms=10)
    assert "llama3.2" not in _names(router.rank(profile="local", endpoints=ENDPOINTS))


CANDS = [("https://openrouter.ai/api/v1/chat/completions", "meta/llama-4:free", {}),
         ("https://api.openai.com/v1/chat/completions", "gpt-4o", {}),
         ("http://localhost:11434/v1/chat/completions", "llama3.2", {})]


def test_fallback_mode_never_strips_paid(monkeypatch):
    monkeypatch.setattr(router, "_configured_chain", lambda owner: {"gpt-4o"})
    config.update_config({"router": {"paid_fallback_mode": "never"}})
    assert _models(router.filter_fallback_candidates(CANDS)) == ["meta/llama-4:free", "llama3.2"]


def _models(cands):
    return [c[1] for c in cands]


def test_fallback_mode_configured_keeps_user_chain_only(monkeypatch):
    monkeypatch.setattr(router, "_configured_chain", lambda owner: {"gpt-4o"})
    assert "gpt-4o" in _models(router.filter_fallback_candidates(CANDS))
    monkeypatch.setattr(router, "_configured_chain", lambda owner: set())
    assert "gpt-4o" not in _models(router.filter_fallback_candidates(CANDS))


def test_budget_exceeded_blocks_paid_fallback_in_every_mode(monkeypatch):
    monkeypatch.setattr(router, "_configured_chain", lambda owner: {"gpt-4o"})
    config.update_config({"pricing": {"gpt-4o": {"input_per_mtok": 1000, "output_per_mtok": 1000}},
                          "budget": {"monthly_usd": 1.0}, "router": {"paid_fallback_mode": "always"}})
    assert "gpt-4o" in _models(router.filter_fallback_candidates(CANDS))
    router.record_usage(url="https://api.openai.com/v1", model="gpt-4o", input_tokens=2000, output_tokens=0)
    assert router.budget_exceeded() is True
    assert "gpt-4o" not in _models(router.filter_fallback_candidates(CANDS))
    assert "llama3.2" in _models(router.filter_fallback_candidates(CANDS))


def test_filter_fails_closed_for_paid_on_internal_error(monkeypatch):
    monkeypatch.setattr(router, "paid_allowed", lambda *a, **k: 1 / 0)
    assert _models(router.filter_fallback_candidates(CANDS)) == ["meta/llama-4:free", "llama3.2"]


def test_unknown_price_is_null_not_guessed_and_free_costs_zero():
    router.record_usage(url="https://api.openai.com/v1", model="gpt-x", input_tokens=10)
    router.record_usage(url="https://openrouter.ai/api/v1", model="a:free", input_tokens=10)
    summary = {r["model"]: r for r in router.usage_summary()["models"]}
    assert summary["gpt-x"]["calls_without_price"] == 1
    assert summary["a:free"]["calls_without_price"] == 0 and summary["a:free"]["cost_usd"] == 0


def test_chat_metrics_are_recorded_per_owner():
    router.record_chat_metrics("ana", "https://openrouter.ai/api/v1", "openrouter/free",
                               {"model": "x/y:free", "input_tokens": 5, "output_tokens": 7, "total_time": 1.5})
    row = router.usage_summary("ana")["models"][0]
    assert (row["model"], row["input_tokens"], row["output_tokens"], row["avg_latency_ms"]) == ("x/y:free", 5, 7, 1500)
    assert router.usage_summary("bob")["models"] == []


@pytest.mark.parametrize("err,kind", [("HTTP 429", "rate_limit"), ("401 Unauthorized", "auth"), ("read timed out", "timeout"), ("502 bad", "server")])
def test_error_classification(err, kind):
    assert router.classify_error(err) == kind


def test_openrouter_pricing_refresh_with_mock():
    import asyncio
    import httpx
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "a/b", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}, {"id": "bad", "pricing": {"prompt": "n/a"}}]}))

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await router.refresh_openrouter_pricing(client)
    assert asyncio.run(run()) == 1
    assert config.get("pricing")["a/b"]["input_per_mtok"] == pytest.approx(1.0)


def test_persona_is_static_ptbr_and_can_be_disabled():
    text = persona.system_preface()
    assert "J.A.R.V.I.S." in text and "português do Brasil" in text
    assert persona.system_preface() == text
    config.update_config({"persona": {"enabled": False}})
    assert persona.system_preface() == ""


def test_direct_shortcut_keeps_identity_memory_and_recent_turns():
    from src.prompt_security import untrusted_context_message
    memory_msg = untrusted_context_message("jarvis memory", "Time: Sport")
    msgs = [{"role": "system", "content": "persona"}, memory_msg] + [{"role": "user", "content": f"u{i}"} for i in range(10)]
    out = persona.direct_reply_messages(msgs, "u9")
    assert out[0]["content"] == "persona" and out[1] is memory_msg
    assert [m["content"] for m in out[2:]] == [f"u{i}" for i in range(4, 10)]


def test_direct_shortcut_is_unchanged_with_persona_disabled():
    config.update_config({"persona": {"enabled": False}})
    assert persona.direct_reply_messages([{"role": "system", "content": "x"}, {"role": "user", "content": "oi"}], "oi") == [{"role": "user", "content": "oi"}]


def test_direct_shortcut_handles_missing_history():
    assert persona.direct_reply_messages([], "oi")[-1] == {"role": "user", "content": "oi"}
