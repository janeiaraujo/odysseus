import datetime as dt
import hashlib
import json
import os
import time

import httpx
import pytest

from src.jarvis import config
from src.jarvis.db import session_scope
from src.jarvis.models import ObjectIndex
from src.jarvis.storage import IntegrityError, QuotaExceeded, StorageError, apply_retention, usage_this_month
from src.jarvis.storage.local import LocalBackend
from src.jarvis.storage.r2 import R2Backend, sign_v4
from src.jarvis.storage.zerokey import ValueTooLarge, ZeroKeyStore, benchmark, compare_costs

MASTER = b"k" * 32


@pytest.fixture
def local(tmp_path):
    return LocalBackend(tmp_path / "objs")


@pytest.fixture(scope="module")
def moto_server():
    moto = pytest.importorskip("moto.server")
    server = moto.ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3(moto_server):
    """R2Backend against a real S3-protocol server (moto), not a request mock."""
    bucket = f"b{time.time_ns()}"
    assert httpx.put(f"{moto_server}/{bucket}").status_code == 200
    return R2Backend(moto_server, bucket, "testing", "testing")


@pytest.fixture(params=["local", "s3"])
def backend(request):
    return request.getfixturevalue(request.param)


def test_put_get_head_delete_round_trip(backend):
    info = backend.put_bytes("jarvis/docs/a b.txt", b"conteudo", {"tipo": "doc"})
    assert info.sha256 == hashlib.sha256(b"conteudo").hexdigest()
    assert backend.get_bytes("jarvis/docs/a b.txt") == b"conteudo"
    head = backend.head("jarvis/docs/a b.txt")
    assert head.size == 8 and head.sha256 == info.sha256 and head.metadata["tipo"] == "doc"
    backend.delete("jarvis/docs/a b.txt")
    assert backend.head("jarvis/docs/a b.txt") is None


def test_listing_filters_prefix_and_paginates(backend):
    for i in range(3):
        backend.put_bytes(f"jarvis/x/{i}", b"1")
    backend.put_bytes("jarvis/y/0", b"1")
    assert sorted(o.key for o in backend.list("jarvis/x/")) == [f"jarvis/x/{i}" for i in range(3)]


def test_zero_byte_objects_supported(backend):
    backend.put_bytes("jarvis/zero", b"")
    assert backend.head("jarvis/zero").size == 0 and backend.get_bytes("jarvis/zero") == b""


def test_file_upload_and_download(backend, tmp_path):
    src = tmp_path / "bk.jrvb"
    src.write_bytes(os.urandom(5000))
    backend.put_file("jarvis/backups/bk.jrvb", src)
    got = backend.get_file("jarvis/backups/bk.jrvb", tmp_path / "out" / "bk.jrvb")
    assert (tmp_path / "out" / "bk.jrvb").read_bytes() == src.read_bytes() and got["size"] == 5000


@pytest.mark.parametrize("key", ["/abs", "a/../b", "", "x" * 1100])
def test_invalid_keys_rejected(local, key):
    with pytest.raises(StorageError):
        local.put_bytes(key, b"x")


def test_local_corruption_detected(local):
    local.put_bytes("a/b", b"original")
    (local.root / "a" / "b").write_bytes(b"alterado")
    with pytest.raises(IntegrityError):
        local.get_bytes("a/b")
    assert local.verify()["corrupted"] == ["a/b"]


def test_r2_corruption_detected_with_mock():
    def handler(request):
        return httpx.Response(200, content=b"alterado", headers={"x-amz-meta-sha256": hashlib.sha256(b"original").hexdigest()})
    with pytest.raises(IntegrityError):
        R2Backend("https://x.example", "b", "k", "s", transport=httpx.MockTransport(handler)).get_bytes("a")


def test_retention_keeps_newest_and_refuses_zero(backend):
    for name in ("a", "b", "c"):
        backend.put_bytes(f"jarvis/backups/{name}", b"1")
        time.sleep(1.05)
    out = apply_retention(backend, "jarvis/backups/", keep_last=2)
    assert out == {"kept": 2, "removed": ["jarvis/backups/a"]}
    with pytest.raises(StorageError):
        apply_retention(backend, "jarvis/backups/", keep_last=0)


def test_signature_matches_botocore_reference():
    auth = pytest.importorskip("botocore.auth")
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    now = dt.datetime(2026, 11, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
    url = "https://acc.r2.cloudflarestorage.com/bucket/jarvis/a%20b.txt"
    payload = hashlib.sha256(b"oi").hexdigest()
    mine = sign_v4("PUT", url, {"x-amz-meta-sha256": payload}, payload, "AK", "SK", now=now)
    request = AWSRequest(method="PUT", url=url, data=b"oi", headers={"x-amz-meta-sha256": payload, "x-amz-content-sha256": payload, "X-Amz-Date": "20261106T120000Z"})

    class Signer(auth.S3SigV4Auth):
        def add_auth(self, req):
            self._timestamp = "20261106T120000Z"
            canonical = self.canonical_request(req)
            string_to_sign = self.string_to_sign(req, canonical)
            req.context["signature"] = self.signature(string_to_sign, req)
            self._inject_signature_to_request(req, req.context["signature"])
    request.context["timestamp"] = "20261106T120000Z"
    request.context["payload_signing_enabled"] = True
    signer = Signer(Credentials("AK", "SK"), "s3", "auto")
    signer.add_auth(request)
    assert mine["Authorization"].split("Signature=")[1] == request.headers["Authorization"].split("Signature=")[1]


def test_r2_network_failure_does_not_leak_credentials():
    def handler(request):
        raise httpx.ConnectError("refused")
    backend = R2Backend("https://x.example", "b", "AK", "SECRETKEY", transport=httpx.MockTransport(handler))
    with pytest.raises(StorageError) as exc:
        backend.put_bytes("a", b"1")
    assert "SECRETKEY" not in str(exc.value)


def test_quota_stops_operations_before_billing():
    config.update_config({"storage": {"max_class_a_month": 1}})
    backend = R2Backend("https://x.example", "b", "k", "s", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    backend.put_bytes("a", b"1")
    with pytest.raises(QuotaExceeded):
        backend.put_bytes("b", b"1")
    assert usage_this_month("r2")["class_a"] == 1


def test_r2_requires_configuration(monkeypatch):
    for name in list(os.environ):
        if name.startswith("JARVIS_R2_"):
            monkeypatch.delenv(name)
    with pytest.raises(StorageError, match="JARVIS_R2_BUCKET"):
        R2Backend.from_environment()


# --- zero-byte experiment ---------------------------------------------------------

def test_zero_key_round_trip_keeps_names_opaque(backend):
    store = ZeroKeyStore(backend, "preferences", master_key=MASTER, owner="ana")
    key = store.put({"pref": "cafe sem acucar", "email": "ana@example.com"})
    assert key.startswith("jarvis/zk/preferences/") and "cafe" not in key and "ana" not in key
    assert backend.head(key).size == 0
    assert store.values() == [{"pref": "cafe sem acucar", "email": "ana@example.com"}]


def test_zero_key_is_idempotent_and_skips_duplicate_put(local):
    store = ZeroKeyStore(local, "n", master_key=MASTER)
    first = store.put({"a": 1})
    before = usage_this_month("local")["class_a"]
    assert store.put({"a": 1}) == first
    assert usage_this_month("local")["class_a"] == before


def test_zero_key_values_read_index_not_bucket(local):
    store = ZeroKeyStore(local, "n", master_key=MASTER)
    store.put({"a": 1})
    before = usage_this_month("local")
    store.values()
    assert usage_this_month("local") == before


def test_zero_key_rebuilds_index_on_new_machine(backend):
    store = ZeroKeyStore(backend, "events", master_key=MASTER)
    store.put({"a": 1})
    store.put({"a": 2})
    with session_scope() as db:
        db.query(ObjectIndex).delete()
    assert store.values() == []
    assert store.validate()["only_in_bucket"] == 2
    assert store.rebuild_index() == {"rebuilt": 2, "unreadable": 0}
    assert sorted(v["a"] for v in store.values()) == [1, 2] and store.validate()["ok"]


def test_zero_key_wrong_master_cannot_read(local):
    ZeroKeyStore(local, "n", master_key=MASTER).put({"segredo": 1})
    assert ZeroKeyStore(local, "n", master_key=b"z" * 32).rebuild_index() == {"rebuilt": 0, "unreadable": 1}


def test_zero_key_namespaces_are_cryptographically_separate(local):
    a = ZeroKeyStore(local, "a", master_key=MASTER)
    key = a.put({"x": 1})
    with pytest.raises(Exception):
        ZeroKeyStore(local, "b", master_key=MASTER)._open(key)


def test_zero_key_rejects_value_over_key_limit(local):
    with pytest.raises(ValueTooLarge):
        ZeroKeyStore(local, "n", master_key=MASTER).put({"t": "x" * 900})


def test_zero_key_delete(local):
    store = ZeroKeyStore(local, "n", master_key=MASTER)
    store.put({"a": 1})
    store.delete({"a": 1})
    assert store.values() == [] and local.list("jarvis/zk/n/") == []


def test_cost_comparison_never_claims_free_storage():
    out = compare_costs(5_000_000)
    assert out["zero_byte"]["class_a"] == 5_000_000 and out["zero_byte"]["write_cost_usd"] == 18.0
    assert out["batched"]["class_a"] == 5000 and out["batched"]["write_cost_usd"] == 0
    assert "não documentado" in out["zero_byte"]["storage_cost"] and out["recommendation"] == "batched"


def test_benchmark_measures_and_cleans_up(backend):
    result = benchmark(backend, master_key=MASTER, records=5)
    assert result["zero_byte_class_a"] == 5 and result["batched_class_a"] == 1
    assert backend.list("jarvis/bench/") == [] and backend.list("jarvis/zk/") == []
