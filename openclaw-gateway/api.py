"""
OpenClaw Gateway Server — HTTP API

Lightweight HTTP server on loopback (127.0.0.1:8766) that lets you
submit action requests to the connected laptop agent.

Endpoints:

    POST /action        Submit an action request, wait for result.
    POST /emergency-stop   Trigger emergency stop on agent.
    POST /resume           Resume agent after emergency stop.
    GET  /status           Check if an agent is connected.

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

logger = logging.getLogger("openclaw.api")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_status(request: web.Request) -> web.Response:
    return web.json_response({
        "agent_connected": is_agent_connected(),
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

    if not is_agent_connected():
        return web.json_response(
            {"error": "No agent connected."}, status=503,
        )

    try:
        result = await send_action(action, params)
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
    try:
        await send_emergency_stop()
        return web.json_response({"status": "emergency_stop_sent"})
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)


async def handle_resume(request: web.Request) -> web.Response:
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
