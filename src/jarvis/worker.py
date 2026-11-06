"""Background loop for missions, mail queue and replication.

Runs inside the web process, honoring the same ODYSSEUS_INPROCESS_TASKS switch as
the task scheduler. All work is claimed atomically, so a duplicate worker is safe.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from src.jarvis import config, mailqueue, missions, replication
from src.jarvis.redact import redact_text

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 30
_task: Optional[asyncio.Task] = None


async def run_once() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, step in (("missions", missions.tick), ("mail", mailqueue.tick)):
        try:
            out[name] = await step()
        except Exception as exc:
            logger.warning("jarvis worker: %s failed: %s", name, redact_text(exc))
            out[name] = {"error": redact_text(exc)[:200]}
    if config.get("firebase.enabled", False):
        try:
            out["replication"] = await replication.flush()
        except Exception as exc:  # outage or missing credentials: the outbox just waits
            logger.info("jarvis worker: replication pending: %s", redact_text(exc))
    return out


async def _loop() -> None:
    while True:
        await run_once()
        await asyncio.sleep(INTERVAL_SECONDS)


def enabled() -> bool:
    return os.environ.get("ODYSSEUS_INPROCESS_TASKS", "1").strip().lower() not in ("0", "false", "no", "off") and config.get("enabled", True)


def start() -> bool:
    global _task
    if _task is None and enabled():
        _task = asyncio.create_task(_loop(), name="jarvis-worker")
        logger.info("Jarvis worker started")
        return True
    return False


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
