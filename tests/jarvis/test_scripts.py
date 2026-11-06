import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _run(*args, env=None):
    return subprocess.run([sys.executable, *map(str, args)], cwd=REPO, capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})


def test_doctor_is_read_only_and_reports_json(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "app.db")
    con.execute("create table t(x)")
    con.close()
    before = (data / "app.db").stat().st_mtime_ns
    out = _run(REPO / "scripts/jarvis-doctor", "--json", env={"ODYSSEUS_DATA_DIR": str(data), "APP_PORT": "9", "JARVIS_BACKUP_DIR": str(tmp_path / "none")})
    report = json.loads(out.stdout)
    checks = {c["check"]: c for c in report["checks"]}
    assert "integro" in checks["Banco Odysseus"]["detail"] and checks["Backup"]["status"] == "aviso"
    assert checks["Segredos no Git"]["status"] == "ok"
    assert (data / "app.db").stat().st_mtime_ns == before


def test_doctor_flags_corrupt_database(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "app.db").write_bytes(b"SQLite format 3\0" + b"lixo" * 500)
    out = _run(REPO / "scripts/jarvis-doctor", "--json", env={"ODYSSEUS_DATA_DIR": str(data), "APP_PORT": "9"})
    assert out.returncode == 1 and json.loads(out.stdout)["ok"] is False


def test_backup_cli_round_trip(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "memory.json").write_text("[]")
    key = tmp_path / "k.key"
    assert _run(REPO / "scripts/jarvis-backup", "keygen", "--out", key).returncode == 0
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert _run(REPO / "scripts/jarvis-backup", "keygen", "--out", key).returncode != 0  # never overwrites a key
    out = tmp_path / "b.jrvb"
    created = _run(REPO / "scripts/jarvis-backup", "create", "--key-file", key, "--out", out, "--data-dir", data, "--no-chroma", "--env-file", tmp_path / "none")
    assert created.returncode == 0, created.stderr
    assert json.loads(_run(REPO / "scripts/jarvis-backup", "verify", "--key-file", key, out).stdout)["ok"] is True
    target = tmp_path / "t"
    assert _run(REPO / "scripts/jarvis-backup", "restore", "--key-file", key, out, "--data-dir", target, "--no-chroma").returncode == 0
    assert (target / "memory.json").read_text() == "[]"


def test_train_dry_run_validates_dataset(tmp_path):
    good = tmp_path / "d.jsonl"
    good.write_text(json.dumps({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}) + "\n")
    out = _run(REPO / "scripts/jarvis-train", "--run", "r", "--dataset", good, "--base-model", "m", "--dry-run")
    assert out.returncode == 0 and json.loads(out.stdout)["dataset"]["examples"] == 1
    bad = tmp_path / "b.jsonl"
    bad.write_text("nao json\n")
    assert _run(REPO / "scripts/jarvis-train", "--run", "r", "--dataset", bad, "--base-model", "m", "--dry-run").returncode == 2


@pytest.mark.parametrize("script", ["jarvis-install", "jarvis-update", "jarvis-migrate-chroma"])
def test_shell_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(REPO / "scripts" / script)]).returncode == 0
