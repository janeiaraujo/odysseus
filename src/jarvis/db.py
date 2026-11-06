"""Dedicated SQLAlchemy engine for Jarvis tables.

Kept apart from core.database on purpose: a bug or migration here cannot
touch data/app.db. JARVIS_DATABASE_URL overrides the default file.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from src.constants import JARVIS_DB

Base = declarative_base()

_lock = threading.RLock()
_engine = None
_Session: Optional[sessionmaker] = None
_url_override: Optional[str] = None


def utcnow() -> datetime:
    """Naive UTC, matching how SQLite round-trips DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def database_url() -> str:
    return _url_override or os.getenv("JARVIS_DATABASE_URL") or f"sqlite:///{JARVIS_DB}"


def _build_engine(url: str):
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    if url.startswith("sqlite:///") and ":memory:" not in url:
        os.makedirs(os.path.dirname(url.replace("sqlite:///", "", 1)) or ".", exist_ok=True)
    engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(conn, _record):  # pragma: no cover - trivial
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return engine


def configure(url: Optional[str] = None) -> None:
    """Point Jarvis at another database (used by tests and restore validation)."""
    global _engine, _Session, _url_override
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine, _Session = None, None
        _url_override = url


def get_engine():
    global _engine, _Session
    with _lock:
        if _engine is None:
            _engine = _build_engine(database_url())
            from src.jarvis import models  # noqa: F401  (registers tables)
            Base.metadata.create_all(_engine)
            _Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
        return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
