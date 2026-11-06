from datetime import timedelta

import pytest

from src.jarvis import memory
from src.jarvis.db import configure, session_scope, utcnow
from src.jarvis.models import MemoryItem, MemoryVersion


class FakeVectors:
    healthy = True

    def __init__(self):
        self.docs = {}

    def add(self, memory_id, text):
        self.docs[memory_id] = text

    def remove(self, memory_id):
        self.docs.pop(memory_id, None)

    def search(self, query, k=8):
        return [{"memory_id": i, "score": 0.9} for i, t in self.docs.items() if memory.similarity(t, query) > 0][:k]


@pytest.fixture(autouse=True)
def vectors():
    fake = FakeVectors()
    memory.set_vector_store(fake)
    yield fake
    memory.flush_index()
    memory.set_vector_store(None)


def test_memory_is_created_searched_and_survives_restart(jarvis_env):
    """Mandatory scenario 2: restart modeled by rebuilding engine from the same file."""
    item = memory.remember("Prefiro respostas em portugues", owner="ana", category="preference")["item"]
    url = f"sqlite:///{jarvis_env / 'jarvis.db'}"
    memory.flush_index()
    configure(url)
    hits = memory.search("idioma das respostas portugues", owner="ana")
    assert [h["id"] for h in hits] == [item["id"]]


def test_search_is_owner_scoped_even_through_indexes():
    memory.remember("Senha do portao e guardada no cofre", owner="ana")
    memory.flush_index()
    assert memory.search("portao cofre", owner="bob") == []
    assert memory.search("portao cofre", owner="ana")


def test_accent_insensitive_text_search(vectors):
    memory.set_vector_store(None)
    memory._vector_failed = True
    try:
        memory.remember("Minha reuniao e na Sao Paulo", owner="")
        assert memory.search("reunião são paulo", owner="")
    finally:
        memory._vector_failed = False


def test_exact_and_near_duplicates_do_not_create_rows():
    first = memory.remember("Prefiro cafe sem acucar", owner="ana")
    second = memory.remember("prefiro café sem açúcar!", owner="ana")
    third = memory.remember("Eu prefiro cafe sem acucar", owner="ana")
    assert first["action"] == "created" and second["action"] == "duplicate" and third["action"] == "duplicate"
    assert len(memory.list_items("ana")) == 1


def test_same_text_for_two_owners_is_not_a_duplicate():
    memory.remember("Moro em Recife", owner="ana")
    assert memory.remember("Moro em Recife", owner="bob")["action"] == "created"


def test_conflict_detected_and_resolved():
    a = memory.remember("Meu aniversario e 10/05", owner="ana")["item"]
    b = memory.remember("Meu aniversario e 12/06", owner="ana")
    assert b["action"] == "conflict" and b["conflicts"][0]["id"] == a["id"]
    assert memory.get(a["id"], "ana")["status"] == "conflicted"
    kept = memory.resolve_conflict(b["item"]["id"], a["id"], owner="ana")
    assert kept["status"] == "fact"
    assert [i["id"] for i in memory.list_items("ana")] == [b["item"]["id"]]


def test_negation_is_conflict_but_unrelated_text_is_not():
    memory.remember("Gosto de reunioes pela manha", owner="")
    assert memory.remember("Não gosto de reunioes pela manha", owner="")["action"] == "conflict"
    assert memory.remember("Tenho um gato chamado Tom", owner="")["action"] == "created"


def test_untrusted_source_cannot_become_fact_or_degrade_a_fact():
    fact = memory.remember("Meu banco e o Banco A", owner="ana")["item"]
    forged = memory.remember("Meu banco e o Banco B", owner="ana", source_type="email", status="fact", confidence=1.0)
    assert forged["item"]["status"] == "hypothesis" and forged["item"]["trust"] == "untrusted"
    assert forged["item"]["confidence"] <= 0.4
    assert memory.get(fact["id"], "ana")["status"] == "fact"
    assert [h["text"] for h in memory.search("banco", owner="ana")] == ["Meu banco e o Banco A"]


def test_user_confirmation_promotes_hypothesis():
    memory.remember("O escritorio fica no centro", owner="", source_type="web")
    result = memory.remember("O escritorio fica no centro", owner="", source_type="user")
    assert result["action"] == "duplicate" and result["item"]["status"] == "fact" and result["item"]["trust"] == "user"


def test_correction_keeps_version_history():
    item = memory.remember("Viagem em marco", owner="")["item"]
    memory.correct(item["id"], "Viagem em abril", owner="")
    history = memory.versions(item["id"], owner="")
    assert [v["text"] for v in history] == ["Viagem em marco", "Viagem em abril"]
    assert memory.get(item["id"], "")["version"] == 2
    assert memory.search("viagem abril", owner="")[0]["text"] == "Viagem em abril"


def test_expired_memory_is_not_retrieved():
    memory.remember("Promocao ate sexta", owner="", valid_until=utcnow() - timedelta(days=1))
    assert memory.search("promocao", owner="") == []


def test_soft_delete_hides_and_purge_erases(vectors):
    a = memory.remember("Dado sensivel um", owner="")["item"]
    b = memory.remember("Outro assunto completamente diferente", owner="")["item"]
    memory.flush_index()
    memory.delete(a["id"], owner="")
    memory.flush_index()
    assert memory.search("sensivel", owner="") == [] and a["id"] not in vectors.docs
    with session_scope() as db:
        assert db.get(MemoryItem, a["id"]) is not None
    memory.delete(b["id"], owner="", purge=True)
    with session_scope() as db:
        assert db.get(MemoryItem, b["id"]) is None
        assert db.query(MemoryVersion).filter(MemoryVersion.memory_id == b["id"]).count() == 0


def test_delete_by_another_owner_is_refused():
    item = memory.remember("x y z da ana", owner="ana")["item"]
    with pytest.raises(LookupError):
        memory.delete(item["id"], owner="bob")


def test_consolidate_merges_rows_that_escaped_dedup():
    with session_scope() as db:
        for text in ("Prefiro voo de manha cedo", "Prefiro voo pela manha cedo"):
            db.add(MemoryItem(owner="", text=text, norm_hash=memory.content_hash(text)))
    assert memory.consolidate("")["merged"] == 1
    assert len(memory.list_items("")) == 1


def test_reindex_rebuilds_vectors_from_sqlite(vectors):
    item = memory.remember("Fato para reindexar", owner="")["item"]
    memory.flush_index()
    vectors.docs.clear()
    assert memory.reindex()["indexed"] == 1
    memory.flush_index()
    assert item["id"] in vectors.docs


def test_vector_failure_does_not_break_search():
    class Broken(FakeVectors):
        def search(self, query, k=8):
            raise RuntimeError("chroma down")
    memory.set_vector_store(Broken())
    memory.remember("Reuniao de orcamento", owner="")
    assert memory.search("orçamento", owner="")[0]["text"] == "Reuniao de orcamento"


def test_import_from_odysseus_is_idempotent_and_read_only():
    entries = [{"id": "1", "text": "Fato antigo", "category": "fact"}, {"id": "2", "text": ""}]
    snapshot = [dict(e) for e in entries]
    assert memory.import_odysseus(entries, "ana") == {"created": 1, "duplicates": 0}
    assert memory.import_odysseus(entries, "ana") == {"created": 0, "duplicates": 1}
    assert entries == snapshot


def test_invalid_input_rejected():
    with pytest.raises(ValueError):
        memory.remember("   ")
    with pytest.raises(ValueError):
        memory.remember("x", kind="nope")


def test_mirror_copies_everything_once_and_follows_deletes():
    entries = [{"id": str(i), "text": f"Moro em lugar {i}", "owner": "ana", "category": "identity"} for i in range(40)]
    assert memory.sync_from_odysseus(entries) == {"created": 40, "total": 40}
    assert memory.sync_from_odysseus(entries) == {"created": 0, "total": 40}
    assert memory.stats("ana")["by_status"] == {"fact": 40}  # import never invents conflicts
    assert memory.delete_mirrored("0", owner="ana") is True
    assert memory.delete_mirrored("0", owner="bob") is False
    assert len(memory.list_items("ana")) == 39


def test_core_identity_reaches_unrelated_questions():
    memory.remember("Moro no Rio de Janeiro", owner="ana", category="identity")
    memory.flush_index()
    assert "Rio de Janeiro" in memory.context_for_chat("previsão do tempo para hoje", owner="ana")
    assert memory.context_for_chat("previsão do tempo", owner="bob") == ""
