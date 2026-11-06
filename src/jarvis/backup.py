"""Encrypted, verifiable backups of everything that isn't in Git.

Format (JRVB1): magic | scrypt salt | base nonce | AES-256-GCM chunks. Each chunk
is authenticated with its index and an end-of-file flag, so reordering or
truncating the file fails verification. The plaintext is a tar with a manifest
holding a SHA-256 per file. ChromaDB is exported logically through its API, so a
backup restores into any Chroma deployment rather than one volume layout.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger(__name__)

MAGIC = b"JRVB1\n"
CHUNK = 1024 * 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SKIP_TOP = {"fastembed_cache", "huggingface", "local", "cache", "tts_cache", "logs", "ssh", "chroma", "backups"}
SECRET_FILES = {".app_key", "auth.json", "sessions.json", "vault.json"}
TEMP_SUFFIXES = (".tmp", ".db-wal", ".db-shm", ".db-journal")
MIN_PASSPHRASE = 12
_CHROMA_PAGE = 500


class BackupError(RuntimeError):
    pass


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise BackupError(f"a chave de recuperação precisa de pelo menos {MIN_PASSPHRASE} caracteres")
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(passphrase.encode("utf-8"))


def _nonce(base: bytes, counter: int) -> bytes:
    return base[:4] + struct.pack(">Q", counter)


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    salt, base = os.urandom(16), os.urandom(4)
    aead = AESGCM(_derive_key(passphrase, salt))
    tmp = dst.with_name(dst.name + ".partial")
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        os.fchmod(fout.fileno(), 0o600)
        fout.write(MAGIC + salt + base)
        counter, current = 0, fin.read(CHUNK)
        while True:
            following = fin.read(CHUNK)
            aad = struct.pack(">QB", counter, 0 if following else 1)
            sealed = aead.encrypt(_nonce(base, counter), current, aad)
            fout.write(struct.pack(">I", len(sealed)) + sealed)
            if not following:
                break
            current, counter = following, counter + 1
    os.replace(tmp, dst)


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        os.fchmod(fout.fileno(), 0o600)
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError("arquivo não é um backup do Jarvis")
        salt, base = fin.read(16), fin.read(4)
        aead = AESGCM(_derive_key(passphrase, salt))
        counter = 0
        header = fin.read(4)
        while True:
            if len(header) != 4:
                raise BackupError("backup truncado")
            sealed = fin.read(struct.unpack(">I", header)[0])
            header = fin.read(4)
            aad = struct.pack(">QB", counter, 0 if header else 1)
            try:
                fout.write(aead.decrypt(_nonce(base, counter), sealed, aad))
            except InvalidTag:
                raise BackupError("chave incorreta ou backup corrompido") from None
            if not header:
                return
            counter += 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if path.suffix not in (".db", ".sqlite", ".sqlite3"):
        return False
    with open(path, "rb") as f:
        return f.read(16).startswith(b"SQLite format 3")


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Online backup API: safe while the app is writing."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()


def _iter_data_files(data_dir: Path, include_secrets: bool) -> Iterable[Path]:
    for root, dirs, files in os.walk(data_dir, followlinks=False):
        rel_root = Path(root).relative_to(data_dir)
        if rel_root == Path("."):
            dirs[:] = [d for d in dirs if d not in SKIP_TOP]
        for name in files:
            path = Path(root) / name
            if path.is_symlink() or name.endswith(TEMP_SUFFIXES) or ".tmp." in name:
                continue
            if not include_secrets and rel_root == Path(".") and name in SECRET_FILES:
                continue
            yield path


def export_chroma(client: Any, dest: Path) -> Dict[str, int]:
    """Dump every collection, embeddings included, to JSONL through the API."""
    counts: Dict[str, int] = {}
    dest.mkdir(parents=True, exist_ok=True)
    for listed in client.list_collections():
        name = listed if isinstance(listed, str) else listed.name
        collection = client.get_collection(name)
        path, offset, written = dest / f"{name}.jsonl", 0, 0
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"collection": name, "metadata": collection.metadata}, ensure_ascii=False) + "\n")
            while True:
                page = collection.get(limit=_CHROMA_PAGE, offset=offset, include=["embeddings", "documents", "metadatas"])
                ids = page.get("ids") or []
                if not ids:
                    break
                embeddings = page.get("embeddings")
                for i, item_id in enumerate(ids):
                    vector = embeddings[i] if embeddings is not None else None
                    f.write(json.dumps({"id": item_id, "embedding": [float(x) for x in vector] if vector is not None else None,
                                        "document": (page.get("documents") or [None] * len(ids))[i],
                                        "metadata": (page.get("metadatas") or [None] * len(ids))[i]}, ensure_ascii=False) + "\n")
                written += len(ids)
                offset += len(ids)
        counts[name] = written
    return counts


def import_chroma(client: Any, src: Path) -> Dict[str, int]:
    """Upsert exported vectors. Existing ids are updated, others left untouched."""
    counts: Dict[str, int] = {}
    for path in sorted(src.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            header = json.loads(f.readline())
            collection = client.get_or_create_collection(header["collection"], metadata=header.get("metadata") or None)
            batch: List[Dict[str, Any]] = []
            total = 0

            def flush() -> None:
                if batch:
                    kwargs: Dict[str, Any] = {"ids": [b["id"] for b in batch], "documents": [b["document"] for b in batch]}
                    if all(b["embedding"] is not None for b in batch):
                        kwargs["embeddings"] = [b["embedding"] for b in batch]
                    if all(b["metadata"] for b in batch):
                        kwargs["metadatas"] = [b["metadata"] for b in batch]
                    collection.upsert(**kwargs)
                    batch.clear()

            for line in f:
                batch.append(json.loads(line))
                total += 1
                if len(batch) >= _CHROMA_PAGE:
                    flush()
            flush()
        counts[header["collection"]] = total
    return counts


def _git_commit(repo: Optional[Path]) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def create_backup(out_path: Path, passphrase: str, *, data_dir: Path, env_file: Optional[Path] = None, include_secrets: bool = True,
                  chroma_client: Any = None, repo: Optional[Path] = None, secrets_dir: Optional[Path] = None) -> Dict[str, Any]:
    out_path, data_dir = Path(out_path), Path(data_dir)
    if data_dir.resolve() in out_path.resolve().parents:
        raise BackupError("o backup não pode ficar dentro do diretório de dados")
    _derive_key(passphrase, b"\0" * 16)  # fail early on a weak passphrase
    with tempfile.TemporaryDirectory(prefix="jarvis-backup-") as tmpdir:
        tmp, files, sqlite_files = Path(tmpdir), [], []
        tar_path = tmp / "payload.tar"
        with tarfile.open(tar_path, "w") as tar:
            for path in _iter_data_files(data_dir, include_secrets):
                rel = Path("data") / path.relative_to(data_dir)
                source = path
                if _is_sqlite(path):
                    source = tmp / "sqlite-copy"
                    _sqlite_copy(path, source)
                    sqlite_files.append(rel.as_posix())
                files.append({"path": rel.as_posix(), "sha256": _sha256(source), "size": source.stat().st_size})
                tar.add(source, arcname=rel.as_posix(), recursive=False)
            if include_secrets and env_file and Path(env_file).is_file():
                files.append({"path": "env/.env", "sha256": _sha256(Path(env_file)), "size": Path(env_file).stat().st_size})
                tar.add(env_file, arcname="env/.env", recursive=False)
            if include_secrets and secrets_dir and Path(secrets_dir).is_dir():
                for path in sorted(Path(secrets_dir).iterdir()):
                    if path.is_file() and not path.is_symlink():
                        arc = f"secrets/{path.name}"
                        files.append({"path": arc, "sha256": _sha256(path), "size": path.stat().st_size})
                        tar.add(path, arcname=arc, recursive=False)
            chroma_counts: Optional[Dict[str, int]] = None
            if chroma_client is not None:
                chroma_dir = tmp / "chroma"
                chroma_counts = export_chroma(chroma_client, chroma_dir)
                for path in sorted(chroma_dir.glob("*.jsonl")):
                    arc = f"chroma/{path.name}"
                    files.append({"path": arc, "sha256": _sha256(path), "size": path.stat().st_size})
                    tar.add(path, arcname=arc, recursive=False)
            manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "git_commit": _git_commit(repo),
                        "include_secrets": include_secrets, "files": files, "sqlite": sqlite_files, "chroma": chroma_counts}
            body = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(body), int(time.time())
            tar.addfile(info, io.BytesIO(body))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        encrypt_file(tar_path, out_path, passphrase)
    return {"path": str(out_path), "size": out_path.stat().st_size, "sha256": _sha256(out_path), "files": len(files),
            "chroma": chroma_counts, "include_secrets": include_secrets}


def _safe_members(tar: tarfile.TarFile) -> List[tarfile.TarInfo]:
    members = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
            raise BackupError(f"entrada insegura no backup: {member.name}")
        if parts and parts[0] not in ("data", "env", "chroma", "secrets", "manifest.json"):
            raise BackupError(f"entrada inesperada no backup: {member.name}")
        members.append(member)
    return members


def _open_payload(path: Path, passphrase: str, workdir: Path) -> Dict[str, Any]:
    payload = workdir / "payload.tar"
    decrypt_file(path, payload, passphrase)
    extract = workdir / "x"
    extract.mkdir()
    with tarfile.open(payload) as tar:
        tar.extractall(extract, members=_safe_members(tar), filter="data")
    manifest = json.loads((extract / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    for entry in manifest["files"]:
        target = extract / entry["path"]
        if not target.is_file():
            problems.append(f"ausente: {entry['path']}")
        elif _sha256(target) != entry["sha256"]:
            problems.append(f"hash divergente: {entry['path']}")
    for rel in manifest.get("sqlite", []):
        try:
            con = sqlite3.connect(f"file:{extract / rel}?mode=ro", uri=True)
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            if result != "ok":
                problems.append(f"sqlite corrompido: {rel}")
        except sqlite3.Error as exc:
            problems.append(f"sqlite ilegível: {rel}: {exc}")
    if problems:
        raise BackupError("; ".join(problems[:10]))
    return {"manifest": manifest, "root": extract}


def verify_backup(path: Path, passphrase: str) -> Dict[str, Any]:
    """Decrypt, re-hash every file and integrity-check every SQLite database."""
    with tempfile.TemporaryDirectory(prefix="jarvis-verify-") as tmpdir:
        result = _open_payload(Path(path), passphrase, Path(tmpdir))
    manifest = result["manifest"]
    return {"ok": True, "created_at": manifest["created_at"], "git_commit": manifest.get("git_commit", ""), "files": len(manifest["files"]),
            "sqlite": manifest.get("sqlite", []), "chroma": manifest.get("chroma"), "include_secrets": manifest.get("include_secrets")}


def restore_backup(path: Path, passphrase: str, *, data_dir: Path, env_target: Optional[Path] = None, force: bool = False,
                   chroma_client: Any = None, secrets_target: Optional[Path] = None) -> Dict[str, Any]:
    """Restore into data_dir. A non-empty target needs force, and is moved aside, never overwritten."""
    data_dir = Path(data_dir)
    moved_to = None
    with tempfile.TemporaryDirectory(prefix="jarvis-restore-") as tmpdir:
        result = _open_payload(Path(path), passphrase, Path(tmpdir))  # verifies before touching the target
        root: Path = result["root"]
        if data_dir.exists() and any(data_dir.iterdir()):
            if not force:
                raise BackupError(f"{data_dir} não está vazio; use force para mover o conteúdo atual para o lado")
            moved_to = data_dir.with_name(f"{data_dir.name}.pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            data_dir.rename(moved_to)
        data_dir.mkdir(parents=True, exist_ok=True)
        restored = 0
        for source in sorted((root / "data").rglob("*")) if (root / "data").exists() else []:
            if source.is_file():
                target = data_dir / source.relative_to(root / "data")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                restored += 1
        for key_file in (data_dir / ".app_key", data_dir / "auth.json"):
            if key_file.exists():
                os.chmod(key_file, 0o600)
        env_restored = False
        if env_target and (root / "env" / ".env").is_file():
            env_target = Path(env_target)
            if env_target.exists() and not force:
                raise BackupError(f"{env_target} já existe; use force")
            if env_target.exists():
                shutil.copy2(env_target, env_target.with_name(env_target.name + ".pre-restore"))
            shutil.copy2(root / "env" / ".env", env_target)
            os.chmod(env_target, 0o600)
            env_restored = True
        secrets_restored = 0
        if secrets_target and (root / "secrets").is_dir():
            target_dir = Path(secrets_target)
            target_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(target_dir, 0o700)
            for source in sorted((root / "secrets").iterdir()):
                target = target_dir / source.name
                if target.exists() and not force:
                    raise BackupError(f"{target} ja existe; use force")
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
                secrets_restored += 1
        chroma_counts = import_chroma(chroma_client, root / "chroma") if chroma_client is not None and (root / "chroma").exists() else None
    return {"ok": True, "restored_files": restored, "moved_previous_to": str(moved_to) if moved_to else None, "env_restored": env_restored, "secrets_restored": secrets_restored, "chroma": chroma_counts}


def validate_restored(data_dir: Path) -> Dict[str, Any]:
    """Post-restore checks: databases open, and encrypted values match the restored key."""
    data_dir = Path(data_dir)
    report: Dict[str, Any] = {"app_db": False, "jarvis_db": False, "app_key_present": (data_dir / ".app_key").exists(), "tables": {}, "problems": []}
    for label, rel, tables in (("app_db", "app.db", ("sessions", "chat_messages", "model_endpoints", "scheduled_tasks")),
                               ("jarvis_db", "jarvis/jarvis.db", ("jarvis_memory", "jarvis_audit"))):
        path = data_dir / rel
        if not path.exists():
            if label == "app_db":
                report["problems"].append("app.db ausente")
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            report[label] = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            for table in tables:
                try:
                    report["tables"][table] = con.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608 - fixed names
                except sqlite3.Error:
                    report["problems"].append(f"tabela ausente: {table}")
        finally:
            con.close()
    app_db = data_dir / "app.db"
    if app_db.exists() and report["app_key_present"]:
        from cryptography.fernet import Fernet, InvalidToken
        fernet = Fernet((data_dir / ".app_key").read_bytes().strip())
        con = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
        try:
            ok = bad = 0
            for (value,) in con.execute("select api_key from model_endpoints where api_key like 'enc:%'"):
                try:
                    fernet.decrypt(value[4:].encode())
                    ok += 1
                except (InvalidToken, ValueError):
                    bad += 1
            report["encrypted_keys"] = {"readable": ok, "unreadable": bad}
            if bad:
                report["problems"].append(f"{bad} chave(s) de API não abrem com a .app_key restaurada")
        except sqlite3.Error:
            pass
        finally:
            con.close()
    report["ok"] = not report["problems"] and report["app_db"]
    return report
