"""Regression for real data loss: importing 130 memories left 4 to 10 of them."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.memory import MemoryManager


def _mem(i):
    return {"text": f"Memória número {i}", "category": "fact"}


def test_slow_writer_cannot_erase_what_was_added_meanwhile(tmp_path):
    """The exact mechanism: a tidy reads, waits for a model, then writes its old copy."""
    tidy, web = MemoryManager(str(tmp_path)), MemoryManager(str(tmp_path))
    web.add_many([_mem(i) for i in range(10)], owner="ana")
    stale = tidy.load_all()
    web.add_many([_mem(i) for i in range(10, 130)], owner="ana")
    removed_by_tidy = stale[0]["id"]
    tidy.save([e for e in stale if e["id"] != removed_by_tidy])
    final = web.load_all()
    assert len(final) == 129 and removed_by_tidy not in {e["id"] for e in final}


def test_deliberate_delete_still_deletes(tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.add_many([_mem(1), _mem(2)], owner="ana")
    entries = manager.load_all()
    manager.save(entries[1:])
    assert [e["text"] for e in manager.load_all()] == [entries[1]["text"]]


def test_shrinking_keeps_a_recoverable_copy(tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.add_many([_mem(i) for i in range(5)], owner="ana")
    manager.save(manager.load_all()[:1])
    history = list((tmp_path / "memory_history").glob("*.json"))
    assert len(history) == 1 and len(json.loads(history[0].read_text())) == 5


def test_concurrent_additions_all_survive(tmp_path):
    def worker(start):
        MemoryManager(str(tmp_path)).add_many([_mem(i) for i in range(start, start + 20)], owner="ana")
    threads = [threading.Thread(target=worker, args=(n * 20,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(MemoryManager(str(tmp_path)).load_all()) == 160


def test_add_many_skips_only_exact_repeats_and_is_per_owner(tmp_path):
    manager = MemoryManager(str(tmp_path))
    result = manager.add_many([{"text": "Moro no Rio"}, {"text": "moro  no rio"}, {"text": "Moro no Rio de Janeiro"}], owner="ana")
    assert len(result["created"]) == 2 and result["duplicates"] == 1
    assert len(manager.add_many([{"text": "Moro no Rio"}], owner="bob")["created"]) == 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    from routes.memory_routes import setup_memory_routes
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, key: "ana")
    manager = MemoryManager(str(tmp_path))
    app = FastAPI()

    @app.middleware("http")
    async def user(request, call_next):
        request.state.current_user = "ana"
        request.state.api_token = False
        return await call_next(request)
    app.include_router(setup_memory_routes(manager, SimpleNamespace(get_session=lambda s: None), memory_vector=None))
    monkeypatch.setattr("routes.memory_routes.resolve_task_endpoint", lambda *a, **k: ("http://x", "m", {}))
    return TestClient(app), manager


def _upload(client, name, text):
    return client.post("/api/memory/import", files={"file": (name, text.encode("utf-8"))})


@pytest.mark.parametrize("name,body", [
    ("m.txt", "\n".join(f"Memória número {i}" for i in range(130))),
    ("m.md", "# Minhas memórias\n" + "\n".join(f"- Memória número {i}" for i in range(130))),
    ("m.txt", "\n".join(f"{i}. Memória número {i}" for i in range(130))),
    ("m.json", json.dumps([{"text": f"Memória número {i}", "category": "fact"} for i in range(130)])),
    ("m.json", json.dumps({"memories": [{"content": f"Memória número {i}"} for i in range(130)]})),
    ("m.json", json.dumps([f"Memória número {i}" for i in range(130)])),
    ("m.csv", "text,category\n" + "\n".join(f"Memória número {i},fact" for i in range(130))),
])
def test_list_files_import_every_entry_without_a_model(api, name, body, monkeypatch):
    client, manager = api

    async def no_llm(*a, **k):
        raise AssertionError("uma lista não deve passar por modelo")
    monkeypatch.setattr("routes.memory_routes.llm_call_async", no_llm)
    suggestions = _upload(client, name, body).json()["suggestions"]
    assert len(suggestions) == 130
    saved = client.post("/api/memory/add-bulk", json={"items": suggestions}).json()
    assert saved["created"] == 130 and saved["count"] == 130 and len(manager.load_all()) == 130


def test_long_prose_is_read_in_pieces_not_truncated(api, monkeypatch):
    client, _ = api
    calls = []

    async def fake_llm(url, model, messages, **kwargs):
        calls.append(messages[1]["content"])
        return json.dumps([{"text": f"Fato da parte {len(calls)}", "category": "fact"}])
    monkeypatch.setattr("routes.memory_routes.llm_call_async", fake_llm)
    body = ("Um parágrafo longo sobre a minha vida e meu trabalho. " * 400 + "\n\n") * 3
    out = _upload(client, "bio.txt", body).json()
    assert len(calls) >= 5 and len(out["suggestions"]) == len(calls) and out["failed_parts"] == 0


def test_one_failed_part_does_not_discard_the_rest(api, monkeypatch):
    client, _ = api
    calls = []

    async def flaky(url, model, messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("provedor caiu")
        return '[{"text": "ok", "category": "fact"}]'
    monkeypatch.setattr("routes.memory_routes.llm_call_async", flaky)
    out = _upload(client, "bio.txt", "Um parágrafo sobre a minha vida. " * 1000).json()
    assert out["failed_parts"] == 1 and out["suggestions"] == [{"text": "ok", "category": "fact"}]


def test_tidy_action_removes_only_exact_duplicates_without_a_model(tmp_path, monkeypatch):
    from src import builtin_actions
    manager = MemoryManager(str(tmp_path))
    manager.save([{"id": "1", "text": "Moro no Rio", "owner": "ana"}, {"id": "2", "text": "moro no  rio", "owner": "ana"}, {"id": "3", "text": "Moro no Rio de Janeiro", "owner": "ana"}, {"id": "4", "text": "Tenho um cachorro", "owner": "ana"}])
    monkeypatch.setattr(builtin_actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.llm_core.llm_call_async_with_fallback", lambda *a, **k: (_ for _ in ()).throw(AssertionError("não deve chamar modelo")))
    action = next(v for k, v in vars(builtin_actions).items() if k == "action_consolidate_memory" or k == "action_tidy_memory")
    asyncio.run(action("ana"))
    assert sorted(e["id"] for e in manager.load_all()) == ["1", "3", "4"]
