"""
SKYNET Gateway — HTTP API

Lightweight HTTP server on loopback (127.0.0.1:8766) that dispatches
action requests to the connected CHATHAN worker.

Endpoints:

    POST /action           Submit an action request, wait for result.
    POST /emergency-stop   Trigger emergency stop on CHATHAN worker.
    POST /resume           Resume worker after emergency stop.
    GET  /status           Check if a CHATHAN worker is connected.

This API is **only** bound to localhost so it is not reachable from
the internet.  If you need external access, put it behind an
authenticated reverse proxy (nginx + basic-auth, or an ALB).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiosqlite
from aiohttp import web

import bot_config
import gateway_config as cfg
from gateway import (
    is_agent_connected,
    send_action,
    send_emergency_stop,
    send_resume,
)
from ssh_tunnel_executor import get_ssh_executor

logger = logging.getLogger("skynet.api")


_SSH_ONLY_MODES = {"ssh", "ssh_tunnel", "tunnel", "ssh-only"}
_idempotency_inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
_idempotency_lock = asyncio.Lock()


def _force_ssh_mode(ssh_configured: bool) -> bool:
    mode = os.environ.get("OPENCLAW_EXECUTION_MODE", "").strip().lower()
    return ssh_configured and mode in _SSH_ONLY_MODES


def _action_key(task_id: str | None, idempotency_key: str | None) -> str | None:
    tid = str(task_id or "").strip()
    key = str(idempotency_key or "").strip()
    if not tid or not key:
        return None
    return f"{tid}:{key}"


def _idempotency_db(request: web.Request) -> aiosqlite.Connection | None:
    return request.app.get("idempotency_db")


async def _load_cached_result(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    async with db.execute(
        """
        SELECT response_json
        FROM action_idempotency
        WHERE task_id = ? AND idempotency_key = ?
        """,
        (task_id, idempotency_key),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _store_cached_result(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    idempotency_key: str,
    result: dict[str, Any],
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO action_idempotency (
            task_id, idempotency_key, response_json, created_at
        ) VALUES (?, ?, ?, datetime('now'))
        """,
        (task_id, idempotency_key, json.dumps(result, default=str)),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_status(request: web.Request) -> web.Response:
    ssh_exec = get_ssh_executor()
    ssh_ok, ssh_detail = await ssh_exec.health_check()
    ssh_configured = ssh_exec.is_configured()
    force_ssh = _force_ssh_mode(ssh_configured)
    return web.json_response({
        "agent_connected": is_agent_connected(),
        "ssh_fallback_enabled": ssh_configured,
        "ssh_fallback_healthy": ssh_ok,
        "ssh_fallback_target": ssh_detail,
        "execution_mode": "ssh_tunnel" if force_ssh else "agent_preferred",
    })


async def handle_action(request: web.Request) -> web.Response:
    """
    POST /action
    Body: {
      "action": "git_status",
      "params": { "working_dir": "..." },
      "task_id": "task-123",                # optional
      "idempotency_key": "claim-token-xyz"  # optional, requires task_id
    }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"error": "Invalid JSON body."}, status=400,
        )

    action = body.get("action")
    if not action or not isinstance(action, str):
        return web.json_response(
            {"error": "Missing 'action' field."}, status=400,
        )

    params = body.get("params", {})
    confirmed = body.get("confirmed", False) is True
    task_id = body.get("task_id")
    idempotency_key = body.get("idempotency_key")
    action_key = _action_key(task_id, idempotency_key)
    db = _idempotency_db(request)

    if idempotency_key and not task_id:
        return web.json_response(
            {"error": "idempotency_key requires task_id."}, status=400,
        )

    if action_key and db is not None:
        cached = await _load_cached_result(
            db,
            task_id=str(task_id),
            idempotency_key=str(idempotency_key),
        )
        if cached is not None:
            replay = dict(cached)
            replay["idempotent_replay"] = True
            return web.json_response(replay)

    is_owner = False
    inflight_future: asyncio.Future[dict[str, Any]] | None = None
    if action_key:
        async with _idempotency_lock:
            existing = _idempotency_inflight.get(action_key)
            if existing is not None:
                inflight_future = existing
            else:
                loop = asyncio.get_running_loop()
                inflight_future = loop.create_future()
                _idempotency_inflight[action_key] = inflight_future
                is_owner = True

        if not is_owner and inflight_future is not None:
            try:
                result = await inflight_future
            except asyncio.TimeoutError:
                return web.json_response({"error": "Agent did not respond in time."}, status=504)
            except Exception as exc:  # pragma: no cover - defensive fallback
                return web.json_response({"error": str(exc)}, status=503)
            replay = dict(result)
            replay["idempotent_replay"] = True
            return web.json_response(replay)

    try:
        ssh_exec = get_ssh_executor()
        ssh_configured = ssh_exec.is_configured()
        force_ssh = _force_ssh_mode(ssh_configured)
        if force_ssh or not is_agent_connected():
            if ssh_configured:
                result = await ssh_exec.execute_action(action, params, confirmed=confirmed)
                if action_key and db is not None:
                    await _store_cached_result(
                        db,
                        task_id=str(task_id),
                        idempotency_key=str(idempotency_key),
                        result=result,
                    )
                if is_owner and inflight_future is not None and not inflight_future.done():
                    inflight_future.set_result(result)
                if result.get("status") == "error":
                    return web.json_response(result, status=503)
                return web.json_response(result)
            if force_ssh:
                if is_owner and inflight_future is not None and not inflight_future.done():
                    inflight_future.set_exception(
                        RuntimeError("SSH mode enabled without configured executor")
                    )
                return web.json_response(
                    {"error": "SSH tunnel mode is enabled but SSH executor is not configured."},
                    status=503,
                )
            if is_owner and inflight_future is not None and not inflight_future.done():
                inflight_future.set_exception(RuntimeError("No connected agent and no SSH fallback"))
            return web.json_response(
                {"error": "No agent connected and SSH fallback is not configured."}, status=503,
            )

        result = await send_action(
            action,
            params,
            confirmed=confirmed,
            task_id=str(task_id) if task_id else None,
            idempotency_key=str(idempotency_key) if idempotency_key else None,
        )
        if action_key and db is not None:
            await _store_cached_result(
                db,
                task_id=str(task_id),
                idempotency_key=str(idempotency_key),
                result=result,
            )
        if is_owner and inflight_future is not None and not inflight_future.done():
            inflight_future.set_result(result)
        return web.json_response(result)
    except asyncio.TimeoutError:
        if is_owner and inflight_future is not None and not inflight_future.done():
            inflight_future.set_exception(asyncio.TimeoutError())
        return web.json_response(
            {"error": "Agent did not respond in time."}, status=504,
        )
    except RuntimeError as exc:
        if is_owner and inflight_future is not None and not inflight_future.done():
            inflight_future.set_exception(exc)
        return web.json_response(
            {"error": str(exc)}, status=503,
        )
    finally:
        if action_key and is_owner:
            async with _idempotency_lock:
                _idempotency_inflight.pop(action_key, None)


async def handle_emergency_stop(request: web.Request) -> web.Response:
    if not is_agent_connected() and get_ssh_executor().is_configured():
        return web.json_response({"status": "not_applicable_in_ssh_mode"})
    try:
        await send_emergency_stop()
        return web.json_response({"status": "emergency_stop_sent"})
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)


async def handle_resume(request: web.Request) -> web.Response:
    if not is_agent_connected() and get_ssh_executor().is_configured():
        return web.json_response({"status": "not_applicable_in_ssh_mode"})
    try:
        await send_resume()
        return web.json_response({"status": "resume_sent"})
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)


async def handle_get_profile(request: web.Request) -> web.Response:
    telegram_user_id = request.match_info.get("telegram_user_id", "").strip()
    if not telegram_user_id.isdigit():
        return web.json_response({"error": "telegram_user_id must be numeric."}, status=400)

    db = request.app.get("idempotency_db")
    if db is None:
        return web.json_response({"error": "Database not initialized."}, status=503)

    try:
        from db import store

        user = await store.get_user_by_telegram_id(db, int(telegram_user_id))
        if not user:
            return web.json_response({"error": "User not found."}, status=404)
        user_id = int(user["id"])
        facts = await store.list_profile_facts(db, user_id=user_id, active_only=True)
        prefs = await store.get_user_preferences(db, user_id=user_id)
        return web.json_response({
            "user": user,
            "facts": facts,
            "preferences": prefs,
        })
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_forget_profile(request: web.Request) -> web.Response:
    telegram_user_id = request.match_info.get("telegram_user_id", "").strip()
    if not telegram_user_id.isdigit():
        return web.json_response({"error": "telegram_user_id must be numeric."}, status=400)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    key = str(payload.get("key_or_text", "")).strip()
    if not key:
        return web.json_response({"error": "Missing key_or_text."}, status=400)

    db = request.app.get("idempotency_db")
    if db is None:
        return web.json_response({"error": "Database not initialized."}, status=503)

    try:
        from db import store

        user = await store.get_user_by_telegram_id(db, int(telegram_user_id))
        if not user:
            return web.json_response({"error": "User not found."}, status=404)
        removed = await store.forget_profile_facts(
            db,
            user_id=int(user["id"]),
            key_or_text=key,
        )
        await store.add_memory_audit_log(
            db,
            user_id=int(user["id"]),
            action="forget_api",
            target_type="fact",
            target_key=key,
            detail=f"Removed facts: {removed}",
        )
        return web.json_response({"ok": True, "removed": removed})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def handle_set_memory_policy(request: web.Request) -> web.Response:
    telegram_user_id = request.match_info.get("telegram_user_id", "").strip()
    if not telegram_user_id.isdigit():
        return web.json_response({"error": "telegram_user_id must be numeric."}, status=400)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be boolean."}, status=400)

    db = request.app.get("idempotency_db")
    if db is None:
        return web.json_response({"error": "Database not initialized."}, status=503)

    try:
        from db import store

        user = await store.get_user_by_telegram_id(db, int(telegram_user_id))
        if not user:
            return web.json_response({"error": "User not found."}, status=404)
        user_id = int(user["id"])
        await store.set_user_memory_enabled(db, user_id=user_id, enabled=enabled)
        await store.add_memory_audit_log(
            db,
            user_id=user_id,
            action="memory_policy_api",
            target_type="policy",
            target_key="memory_enabled",
            detail=f"enabled={enabled}",
        )
        return web.json_response({"ok": True, "enabled": enabled})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

async def _ensure_idempotency_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS action_idempotency (
            task_id          TEXT NOT NULL,
            idempotency_key  TEXT NOT NULL,
            response_json    TEXT NOT NULL DEFAULT '{}',
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, idempotency_key)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_idempotency_created ON action_idempotency(created_at)"
    )
    await db.commit()


async def _cleanup_app(app: web.Application) -> None:
    db = app.get("idempotency_db")
    if db is not None:
        await db.close()


def create_app(*, idempotency_db: aiosqlite.Connection | None = None) -> web.Application:
    app = web.Application()
    app["idempotency_db"] = idempotency_db
    app.on_cleanup.append(_cleanup_app)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/action", handle_action)
    app.router.add_post("/emergency-stop", handle_emergency_stop)
    app.router.add_post("/resume", handle_resume)
    app.router.add_get("/profile/{telegram_user_id}", handle_get_profile)
    app.router.add_post("/profile/{telegram_user_id}/forget", handle_forget_profile)
    app.router.add_post("/profile/{telegram_user_id}/memory-policy", handle_set_memory_policy)
    return app


async def start_http_api() -> web.AppRunner:
    """Start the HTTP API server and return the runner."""
    idempotency_db = await aiosqlite.connect(bot_config.DB_PATH)
    idempotency_db.row_factory = aiosqlite.Row
    await _ensure_idempotency_schema(idempotency_db)

    app = create_app(idempotency_db=idempotency_db)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.HTTP_HOST, cfg.HTTP_PORT)
    await site.start()
    logger.info("HTTP API listening on http://%s:%d", cfg.HTTP_HOST, cfg.HTTP_PORT)
    return runner
