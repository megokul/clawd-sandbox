"""
OpenClaw Local Execution Agent — Audit Logger

Append-only JSONL audit trail.  Every inbound request and its outcome
are recorded with a UTC timestamp, the resolved tier, and the result
or rejection reason.

The log file lives under config.AUDIT_LOG_DIR and is created on first write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from config import AUDIT_LOG_DIR, AUDIT_LOG_FILE

logger = logging.getLogger("openclaw.audit")

_log_path: str | None = None
_write_lock = asyncio.Lock()


def _ensure_log_dir() -> str:
    global _log_path
    if _log_path is None:
        os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
        _log_path = os.path.join(AUDIT_LOG_DIR, AUDIT_LOG_FILE)
    return _log_path


def _build_entry(
    *,
    request_id: str,
    action: str,
    tier: str,
    params: dict[str, Any] | None,
    outcome: str,
    detail: Any = None,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        "request_id": request_id,
        "action": action,
        "tier": tier,
        "params": params,
        "outcome": outcome,
        "detail": detail,
        "duration_ms": duration_ms,
    }


async def log_event(
    *,
    request_id: str,
    action: str,
    tier: str,
    params: dict[str, Any] | None = None,
    outcome: str,
    detail: Any = None,
    duration_ms: float | None = None,
) -> None:
    """Append one audit record to the JSONL log file."""
    entry = _build_entry(
        request_id=request_id,
        action=action,
        tier=tier,
        params=params,
        outcome=outcome,
        detail=detail,
        duration_ms=duration_ms,
    )
    line = json.dumps(entry, default=str) + "\n"
    path = _ensure_log_dir()

    async with _write_lock:
        # Run blocking I/O in a thread so we never stall the event loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _append_line, path, line)

    logger.info(
        "audit | %s | %s | %s | %s",
        request_id,
        action,
        tier,
        outcome,
    )


def _append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
