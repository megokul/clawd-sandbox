"""
OpenClaw Gateway Server — WebSocket Core

Accepts exactly one agent connection at a time (your laptop).
Provides an internal API for other components (HTTP API, CLI) to
enqueue action requests and await responses.

Authentication: The agent must send ``Authorization: Bearer <token>``
in the WebSocket upgrade headers.  Connections without a valid token
are rejected immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import os
import uuid
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

import gateway_config as cfg

logger = logging.getLogger("openclaw.gateway")

# ---------------------------------------------------------------------------
# Agent connection state
# ---------------------------------------------------------------------------

# The single connected agent (or None).
_agent_ws: ServerConnection | None = None
_agent_lock = asyncio.Lock()

# Maps request_id → Future that resolves with the agent's response.
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
_pending_lock = asyncio.Lock()

# Event that signals at least one agent is connected.
agent_connected = asyncio.Event()


# ---------------------------------------------------------------------------
# Public interface — used by HTTP API and CLI
# ---------------------------------------------------------------------------

async def send_action(
    action: str,
    params: dict[str, Any] | None = None,
    timeout: int = cfg.ACTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Send an action request to the connected agent and wait for the response.

    Raises ``RuntimeError`` if no agent is connected.
    Raises ``asyncio.TimeoutError`` if the agent doesn't reply in time.
    """
    if _agent_ws is None:
        raise RuntimeError("No agent connected.")

    request_id = str(uuid.uuid4())
    message = {
        "type": "action_request",
        "request_id": request_id,
        "action": action,
        "params": params or {},
    }

    # Create a future for the response.
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()

    async with _pending_lock:
        _pending[request_id] = future

    try:
        await _agent_ws.send(json.dumps(message))
        logger.info("Sent action '%s' (req=%s) to agent.", action, request_id)

        result = await asyncio.wait_for(future, timeout=timeout)
        return result

    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for response to req=%s", request_id)
        raise

    finally:
        async with _pending_lock:
            _pending.pop(request_id, None)


async def send_emergency_stop() -> None:
    """Send the emergency stop control message to the agent."""
    if _agent_ws is None:
        raise RuntimeError("No agent connected.")
    await _agent_ws.send(json.dumps({"type": "emergency_stop"}))
    logger.critical("Emergency stop sent to agent.")


async def send_resume() -> None:
    """Send the resume control message to the agent."""
    if _agent_ws is None:
        raise RuntimeError("No agent connected.")
    await _agent_ws.send(json.dumps({"type": "resume"}))
    logger.info("Resume sent to agent.")


def is_agent_connected() -> bool:
    return _agent_ws is not None


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handler(ws: ServerConnection) -> None:
    """Handle one agent WebSocket connection."""
    global _agent_ws

    # ---- Authenticate ----
    # websockets >= 14 exposes request headers via ws.request
    token = _extract_token(ws)
    if token != cfg.AUTH_TOKEN:
        logger.warning("Rejected connection: invalid token.")
        await ws.close(4001, "Unauthorized")
        return

    # ---- Accept (single agent at a time) ----
    async with _agent_lock:
        if _agent_ws is not None:
            logger.warning("Rejected connection: another agent already connected.")
            await ws.close(4002, "Another agent is already connected")
            return
        _agent_ws = ws
        agent_connected.set()

    remote = ws.remote_address
    logger.info("Agent connected from %s", remote)

    try:
        async for raw in ws:
            await _on_message(raw)
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Agent disconnected (%s).", exc)
    finally:
        async with _agent_lock:
            _agent_ws = None
            agent_connected.clear()
        # Cancel any pending futures so callers don't hang.
        async with _pending_lock:
            for rid, fut in _pending.items():
                if not fut.done():
                    fut.set_exception(RuntimeError("Agent disconnected."))
            _pending.clear()
        logger.info("Agent connection cleaned up.")


def _extract_token(ws: ServerConnection) -> str:
    """Pull the Bearer token from the upgrade request headers."""
    try:
        auth = ws.request.headers.get("Authorization", "")
    except AttributeError:
        return ""
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


async def _on_message(raw: str | bytes) -> None:
    """Route an inbound message from the agent."""
    try:
        msg: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Non-JSON frame from agent — ignoring.")
        return

    msg_type = msg.get("type", "")

    if msg_type == "agent_hello":
        caps = msg.get("capabilities", [])
        logger.info("Agent hello received. Capabilities: %s", caps)
        return

    if msg_type == "action_response":
        request_id = msg.get("request_id", "")
        async with _pending_lock:
            future = _pending.get(request_id)
        if future and not future.done():
            future.set_result(msg)
        else:
            logger.warning("Response for unknown/expired request_id=%s", request_id)
        return

    if msg_type in ("emergency_stop_ack", "resume_ack"):
        logger.info("Agent acknowledged: %s", msg_type)
        return

    if msg_type == "pong":
        return

    logger.debug("Unhandled agent message type: %s", msg_type)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def _build_ssl_context() -> ssl.SSLContext | None:
    """Load TLS cert/key if they exist, otherwise run without TLS."""
    if os.path.isfile(cfg.TLS_CERT) and os.path.isfile(cfg.TLS_KEY):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.TLS_CERT, cfg.TLS_KEY)
        logger.info("TLS enabled (cert=%s).", cfg.TLS_CERT)
        return ctx
    logger.warning(
        "TLS cert/key not found (%s, %s). Running WITHOUT TLS — "
        "use setup_tls.sh to generate certificates.",
        cfg.TLS_CERT,
        cfg.TLS_KEY,
    )
    return None


async def start_ws_server() -> websockets.asyncio.server.Server:
    """Start the WebSocket server and return the server object."""
    ssl_ctx = _build_ssl_context()

    server = await websockets.serve(
        _handler,
        cfg.WS_HOST,
        cfg.WS_PORT,
        ssl=ssl_ctx,
        ping_interval=cfg.WS_PING_INTERVAL,
        ping_timeout=cfg.WS_PING_TIMEOUT,
        max_size=2**20,
    )

    proto = "wss" if ssl_ctx else "ws"
    logger.info("WebSocket server listening on %s://0.0.0.0:%d", proto, cfg.WS_PORT)
    return server
