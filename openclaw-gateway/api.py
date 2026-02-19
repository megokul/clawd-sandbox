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

from aiohttp import web

import gateway_config as cfg
from gateway import (
    is_agent_connected,
    send_action,
    send_emergency_stop,
    send_resume,
)
from ssh_tunnel_executor import get_ssh_executor

logger = logging.getLogger("skynet.api")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_status(request: web.Request) -> web.Response:
    ssh_exec = get_ssh_executor()
    ssh_ok, ssh_detail = await ssh_exec.health_check()
    return web.json_response({
        "agent_connected": is_agent_connected(),
        "ssh_fallback_enabled": ssh_exec.is_configured(),
        "ssh_fallback_healthy": ssh_ok,
        "ssh_fallback_target": ssh_detail,
    })


async def handle_action(request: web.Request) -> web.Response:
    """
    POST /action
    Body: { "action": "git_status", "params": { "working_dir": "..." } }
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

    if not is_agent_connected():
        ssh_exec = get_ssh_executor()
        if ssh_exec.is_configured():
            result = await ssh_exec.execute_action(action, params, confirmed=confirmed)
            if result.get("status") == "error":
                return web.json_response(result, status=503)
            return web.json_response(result)
        return web.json_response(
            {"error": "No agent connected and SSH fallback is not configured."}, status=503,
        )

    try:
        result = await send_action(action, params, confirmed=confirmed)
        return web.json_response(result)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "Agent did not respond in time."}, status=504,
        )
    except RuntimeError as exc:
        return web.json_response(
            {"error": str(exc)}, status=503,
        )


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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_post("/action", handle_action)
    app.router.add_post("/emergency-stop", handle_emergency_stop)
    app.router.add_post("/resume", handle_resume)
    return app


async def start_http_api() -> web.AppRunner:
    """Start the HTTP API server and return the runner."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.HTTP_HOST, cfg.HTTP_PORT)
    await site.start()
    logger.info("HTTP API listening on http://%s:%d", cfg.HTTP_HOST, cfg.HTTP_PORT)
    return runner
