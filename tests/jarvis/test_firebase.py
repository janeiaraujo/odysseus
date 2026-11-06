import asyncio
import base64
import json
import os
import socket

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.jarvis import config, firebase, memory, replication
from src.jarvis.db import session_scope
from src.jarvis.models import ReplicationOutbox


@pytest.fixture(autouse=True)
def text_only():
    memory._vector_failed = True
    yield
    memory._vector_failed = False


class FakeFirestore:
    def __init__(self, fail=False):
        self.docs, self.fail = {}, fail

    async def upsert(self, path, fields):
        if self.fail:
            raise RuntimeError("503 unavailable token=abc123secret")
        self.docs[path] = fields

    async def delete(self, path):
        self.docs.pop(path, None)


def _enable():
    config.update_config({"firebase": {"enabled": True}})


def test_nothing_is_queued_while_disabled():
    memory.remember("Fato local", owner="ana")
    assert replication.status() == {"enabled": False, "pending": 0, "stuck": 0}


def test_memory_replicates_encrypted_and_without_username():
    _enable()
    item = memory.remember("Texto muito pessoal", owner="ana")["item"]
    store = FakeFirestore()
    assert asyncio.run(replication.flush(store))["sent"] == 1
    path, doc = next(iter(store.docs.items()))
    assert path.endswith(item["id"]) and "ana" not in path
    assert "Texto" not in json.dumps(doc) and doc["text_enc"].startswith("enc:") and doc["kind"] == "semantic"


def test_plain_replication_only_when_opted_in():
    config.update_config({"firebase": {"enabled": True, "encrypt_payloads": False}})
    memory.remember("Texto claro", owner="ana")
    store = FakeFirestore()
    asyncio.run(replication.flush(store))
    assert next(iter(store.docs.values()))["text"] == "Texto claro"


def test_delete_propagates():
    _enable()
    item = memory.remember("Apagar depois", owner="ana")["item"]
    store = FakeFirestore()
    asyncio.run(replication.flush(store))
    memory.delete(item["id"], owner="ana")
    asyncio.run(replication.flush(store))
    assert store.docs == {}


def test_firebase_outage_keeps_local_data_and_retries_without_leaking():
    _enable()
    memory.remember("Sobrevive a queda", owner="ana")
    assert asyncio.run(replication.flush(FakeFirestore(fail=True)))["failed"] == 1
    assert memory.search("queda", owner="ana")
    assert replication.status()["pending"] == 1
    with session_scope() as db:
        row = db.query(ReplicationOutbox).one()
        assert row.attempts == 1 and "abc123secret" not in row.last_error
        row.attempts = 0
    store = FakeFirestore()
    assert asyncio.run(replication.flush(store))["sent"] == 1 and replication.status()["pending"] == 0


def test_value_codec_round_trip():
    doc = {"s": "x", "i": 3, "f": 1.5, "b": True, "n": None, "l": [1, "a"], "m": {"k": 2}}
    assert {k: firebase.from_value(firebase.to_value(v)) for k, v in doc.items()} == doc


def test_missing_credentials_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("JARVIS_FIREBASE_CREDENTIALS", raising=False)
    with pytest.raises(firebase.FirebaseNotConfigured):
        firebase.FirestoreClient.from_config()


def test_service_account_flow_signs_a_valid_jwt_with_mock():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    seen = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            assertion = dict(x.split("=") for x in request.read().decode().split("&"))["assertion"]
            header, claims, signature = assertion.split(".")
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            key.public_key().verify(pad(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
            seen["claims"] = json.loads(pad(claims))
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.setdefault("auth", []).append(request.headers["authorization"])
        return httpx.Response(200, json={"fields": {}})
    client = firebase.FirestoreClient("p", credentials={"client_email": "svc@p.iam", "private_key": pem}, transport=httpx.MockTransport(handler))
    asyncio.run(client.upsert("users/u/memory/1", {"a": 1}))
    asyncio.run(client.upsert("users/u/memory/2", {"a": 1}))
    assert seen["claims"]["iss"] == "svc@p.iam" and seen["auth"] == ["Bearer tok", "Bearer tok"]


emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")


@pytest.mark.skipif(not emulator, reason="FIRESTORE_EMULATOR_HOST not set")
def test_against_official_firestore_emulator():
    _enable()
    item = memory.remember("Validado no emulador", owner="ana")["item"]
    client = firebase.FirestoreClient(f"demo-jarvis{os.getpid()}", emulator_host=emulator)
    assert asyncio.run(replication.flush(client))["sent"] == 1
    path = f"users/{replication.owner_key('ana')}/memory/{item['id']}"
    doc = asyncio.run(client.get(path))
    assert doc["kind"] == "semantic" and doc["version"] == 1 and doc["text_enc"].startswith("enc:")
    assert [d["id"] for d in asyncio.run(client.list(f"users/{replication.owner_key('ana')}/memory"))] == [item["id"]]
    memory.delete(item["id"], owner="ana")
    asyncio.run(replication.flush(client))
    assert asyncio.run(client.get(path)) is None


def test_env_switch_enables_replication(monkeypatch):
    monkeypatch.setenv("JARVIS_FIREBASE_ENABLED", "true")
    memory.remember("Via variavel de ambiente", owner="ana")
    assert replication.status() == {"enabled": True, "pending": 1, "stuck": 0}
