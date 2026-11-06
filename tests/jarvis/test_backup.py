import json
import os
import sqlite3
import tarfile

import pytest
from cryptography.fernet import Fernet

from src.jarvis import backup

KEY = "chave-de-recuperacao-de-teste"


class FakeCollection:
    def __init__(self, name, metadata=None):
        self.name, self.metadata, self.rows = name, metadata, {}

    def get(self, limit, offset, include):
        ids = list(self.rows)[offset:offset + limit]
        return {"ids": ids, "embeddings": [self.rows[i]["embedding"] for i in ids], "documents": [self.rows[i]["document"] for i in ids],
                "metadatas": [self.rows[i]["metadata"] for i in ids]}

    def upsert(self, ids, documents, embeddings=None, metadatas=None):
        for i, item in enumerate(ids):
            self.rows[item] = {"embedding": embeddings[i] if embeddings else None, "document": documents[i], "metadata": metadatas[i] if metadatas else None}


class FakeChroma:
    def __init__(self):
        self.collections = {}

    def list_collections(self):
        return list(self.collections.values())

    def get_collection(self, name):
        return self.collections[name]

    def get_or_create_collection(self, name, metadata=None):
        return self.collections.setdefault(name, FakeCollection(name, metadata))


@pytest.fixture
def source(tmp_path):
    data = tmp_path / "data"
    (data / "jarvis").mkdir(parents=True)
    (data / "fastembed_cache").mkdir()
    (data / "fastembed_cache" / "huge.bin").write_bytes(b"0" * 100)
    key = Fernet.generate_key()
    (data / ".app_key").write_bytes(key)
    (data / "memory.json").write_text('[{"text": "memoria pessoal unica"}]')
    con = sqlite3.connect(data / "app.db")
    for table in ("sessions", "chat_messages", "scheduled_tasks"):
        con.execute(f"create table {table}(id)")
    con.execute("create table model_endpoints(id, api_key)")
    con.execute("insert into model_endpoints values ('a', ?)", ("enc:" + Fernet(key).encrypt(b"sk-real").decode(),))
    con.execute("insert into scheduled_tasks values ('t1')")
    con.commit()
    con.close()
    con = sqlite3.connect(data / "jarvis" / "jarvis.db")
    con.execute("create table jarvis_memory(id)")
    con.execute("create table jarvis_audit(id)")
    con.execute("insert into jarvis_memory values ('m1')")
    con.commit()
    con.close()
    env = tmp_path / ".env"
    env.write_text("SECRET=valor\n")
    return data, env


def _backup(tmp_path, source, **kwargs):
    data, env = source
    out = tmp_path / "out" / "b.jrvb"
    return out, backup.create_backup(out, KEY, data_dir=data, env_file=env, **kwargs)


def test_output_is_opaque_and_skips_caches(tmp_path, source):
    out, info = _backup(tmp_path, source)
    raw = out.read_bytes()
    assert raw.startswith(backup.MAGIC) and b"memoria pessoal" not in raw and b"SECRET=valor" not in raw
    assert os.stat(out).st_mode & 0o077 == 0
    verified = backup.verify_backup(out, KEY)
    assert verified["ok"] and "data/app.db" in verified["sqlite"]
    assert verified["files"] == info["files"]


def test_full_restore_on_clean_machine(tmp_path, source):
    """Mandatory scenario 11: everything comes back in a new location."""
    chroma = FakeChroma()
    chroma.get_or_create_collection("odysseus_memories", {"hnsw:space": "cosine"}).upsert(["a"], ["doc"], [[0.1, 0.2]], [{"k": "v"}])
    out, info = _backup(tmp_path, source, chroma_client=chroma)
    assert info["chroma"] == {"odysseus_memories": 1}
    target, env_target, new_chroma = tmp_path / "new" / "data", tmp_path / "new" / ".env", FakeChroma()
    target.parent.mkdir()
    result = backup.restore_backup(out, KEY, data_dir=target, env_target=env_target, chroma_client=new_chroma)
    assert result["ok"] and result["env_restored"] and result["chroma"] == {"odysseus_memories": 1}
    assert json.loads((target / "memory.json").read_text())[0]["text"] == "memoria pessoal unica"
    assert env_target.read_text() == "SECRET=valor\n" and os.stat(target / ".app_key").st_mode & 0o077 == 0
    assert new_chroma.collections["odysseus_memories"].rows["a"]["embedding"] == [0.1, 0.2]
    assert not (target / "fastembed_cache").exists()
    report = backup.validate_restored(target)
    assert report["ok"] and report["encrypted_keys"] == {"readable": 1, "unreadable": 0}
    assert report["tables"]["jarvis_memory"] == 1 and report["tables"]["scheduled_tasks"] == 1


def test_wrong_key_rejected_and_target_untouched(tmp_path, source):
    out, _ = _backup(tmp_path, source)
    target = tmp_path / "t"
    with pytest.raises(backup.BackupError, match="chave incorreta"):
        backup.restore_backup(out, "outra-chave-qualquer-aqui", data_dir=target)
    assert not target.exists()


@pytest.mark.parametrize("damage", ["flip", "truncate"])
def test_tampering_is_detected(tmp_path, source, damage):
    out, _ = _backup(tmp_path, source)
    raw = bytearray(out.read_bytes())
    if damage == "flip":
        raw[len(raw) // 2] ^= 1
    else:
        raw = raw[: len(raw) - 100]
    out.write_bytes(raw)
    with pytest.raises(backup.BackupError):
        backup.verify_backup(out, KEY)


def test_dropping_trailing_chunk_is_detected(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    src.write_bytes(os.urandom(backup.CHUNK * 2 + 10))
    backup.encrypt_file(src, dst, KEY)
    raw = dst.read_bytes()
    first_len = int.from_bytes(raw[len(backup.MAGIC) + 20:len(backup.MAGIC) + 24], "big")
    dst.write_bytes(raw[:len(backup.MAGIC) + 24 + first_len])
    with pytest.raises(backup.BackupError):
        backup.decrypt_file(dst, tmp_path / "x", KEY)


def test_existing_target_is_never_overwritten(tmp_path, source):
    out, _ = _backup(tmp_path, source)
    target = tmp_path / "live"
    target.mkdir()
    (target / "keep.txt").write_text("atual")
    with pytest.raises(backup.BackupError, match="não está vazio"):
        backup.restore_backup(out, KEY, data_dir=target)
    assert (target / "keep.txt").read_text() == "atual"
    result = backup.restore_backup(out, KEY, data_dir=target, force=True)
    assert (target / "memory.json").exists() and not (target / "keep.txt").exists()
    assert open(os.path.join(result["moved_previous_to"], "keep.txt")).read() == "atual"


def test_no_secrets_mode_omits_keys(tmp_path, source):
    out, _ = _backup(tmp_path, source, include_secrets=False)
    target = tmp_path / "t"
    result = backup.restore_backup(out, KEY, data_dir=target, env_target=tmp_path / "e")
    assert not (target / ".app_key").exists() and not result["env_restored"]
    assert (target / "memory.json").exists()


def test_weak_passphrase_refused(tmp_path, source):
    data, env = source
    with pytest.raises(backup.BackupError, match="pelo menos"):
        backup.create_backup(tmp_path / "x", "curta", data_dir=data)


def test_backup_inside_data_dir_refused(tmp_path, source):
    data, env = source
    with pytest.raises(backup.BackupError, match="dentro"):
        backup.create_backup(data / "x.jrvb", KEY, data_dir=data)


def test_path_traversal_entry_rejected(tmp_path):
    payload = tmp_path / "evil.tar"
    evil = tmp_path / "evil.txt"
    evil.write_text("x")
    with tarfile.open(payload, "w") as tar:
        tar.add(evil, arcname="../../etc/evil")
    enc = tmp_path / "evil.jrvb"
    backup.encrypt_file(payload, enc, KEY)
    with pytest.raises(backup.BackupError, match="insegura"):
        backup.verify_backup(enc, KEY)


def test_mismatched_key_reported_by_validation(tmp_path, source):
    data, _ = source
    (data / ".app_key").write_bytes(Fernet.generate_key())
    report = backup.validate_restored(data)
    assert not report["ok"] and report["encrypted_keys"]["unreadable"] == 1


def test_secrets_dir_travels_encrypted_with_strict_permissions(tmp_path, source):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "firebase-sa.json").write_text('{"private_key": "chave-ultra-secreta"}')
    out, _ = _backup(tmp_path, source, secrets_dir=secrets)
    assert b"chave-ultra-secreta" not in out.read_bytes()
    target = tmp_path / "novo-secrets"
    result = backup.restore_backup(out, KEY, data_dir=tmp_path / "t", secrets_target=target)
    assert result["secrets_restored"] == 1 and "ultra" in (target / "firebase-sa.json").read_text()
    assert os.stat(target / "firebase-sa.json").st_mode & 0o077 == 0 and os.stat(target).st_mode & 0o077 == 0
    with pytest.raises(backup.BackupError, match="ja existe|já existe"):
        backup.restore_backup(out, KEY, data_dir=tmp_path / "t2", secrets_target=target)


def test_no_secrets_mode_skips_secrets_dir(tmp_path, source):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "k.json").write_text("x")
    out, info = _backup(tmp_path, source, secrets_dir=secrets, include_secrets=False)
    target = tmp_path / "s2"
    assert backup.restore_backup(out, KEY, data_dir=tmp_path / "t", secrets_target=target)["secrets_restored"] == 0
