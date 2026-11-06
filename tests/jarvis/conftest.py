"""Isolate every Jarvis test: temp SQLite file, temp config, temp app key."""
import pytest


@pytest.fixture(autouse=True)
def jarvis_env(tmp_path, monkeypatch):
    from src.jarvis import config as jconfig, db as jdb
    import src.secret_storage as secrets_mod

    monkeypatch.setattr(jconfig, "JARVIS_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_mod, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets_mod, "_fernet", None)
    jdb.configure(f"sqlite:///{tmp_path / 'jarvis.db'}")
    yield tmp_path
    jdb.configure(None)
