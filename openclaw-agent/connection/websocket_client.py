"""
OpenClaw Local Execution Agent — WebSocket Connection Layer

Maintains a persistent, outbound-only WebSocket connection to the
AWS OpenClaw gateway.  Handles:

  - Token-based authentication via the ``Authorization`` header on connect.
  - Automatic reconnection with exponential back-off.
  - Ping/pong keep-alive to survive NAT timeouts.
  - Emergency-stop control messages.
  - Dispatching inbound action requests to the router.
  - Sending structured JSON responses back to the gateway.

The connection is always initiated *from* the laptop *to* AWS.
AWS never connects inbound.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

import config
from router.action_router import route

logger = logging.getLogger("openclaw.connection")


async def run_agent() -> None:
    """
    Top-level coroutine.  Connects to the gateway and enters the
    receive-dispatch loop.  Reconnects automatically on failure.
    """
    delay = config.RECONNECT_DELAY_SECONDS

    while True:
        try:
            await _connect_and_listen()
            # If the connection closes cleanly, reset the back-off.
            delay = config.RECONNECT_DELAY_SECONDS

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidStatusCode,
            OSError,
        ) as exc:
            logger.warning("Connection lost (%s). Reconnecting in %ds…", exc, delay)

        except Exception:
            logger.exception("Unexpected error in connection loop. Reconnecting in %ds…", delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, config.MAX_RECONNECT_DELAY_SECONDS)


async def _connect_and_listen() -> None:
    """Establish one WebSocket session and process messages until it closes."""
    if not config.AUTH_TOKEN:
        raise RuntimeError(
            "OPENCLAW_AUTH_TOKEN is not set. "
            "Export it as an environment variable before starting the agent."
        )

    headers = {"Authorization": f"Bearer {config.AUTH_TOKEN}"}

    logger.info("Connecting to %s …", config.GATEWAY_URL)

    # Accept self-signed certs from the gateway.  In production, replace
    # with a proper CA-signed cert and remove this override.
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect(
        config.GATEWAY_URL,
        additional_headers=headers,
        ssl=ssl_ctx,
        ping_interval=config.WS_PING_INTERVAL_SECONDS,
        ping_timeout=config.WS_PING_TIMEOUT_SECONDS,
        max_size=2**20,  # 1 MB max frame
    ) as ws:
        logger.info("Connected to gateway.")
        delay = config.RECONNECT_DELAY_SECONDS  # reset on successful connect

        # Send a hello handshake so the server knows who we are.
        await _send_hello(ws)

        # Enter receive loop.
        async for raw in ws:
            await _handle_message(ws, raw)


async def _send_hello(ws: ClientConnection) -> None:
    """Send an agent-hello message after connecting."""
    hello = {
        "type": "agent_hello",
        "agent_version": "1.0.0",
        "capabilities": list(
            config.AUTO_ACTIONS | config.CONFIRM_ACTIONS
        ),
    }
    await ws.send(json.dumps(hello))
    logger.debug("Sent hello: %s", hello)


async def _handle_message(ws: ClientConnection, raw: str | bytes) -> None:
    """Parse one inbound frame and dispatch it."""
    try:
        message: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Received non-JSON frame — ignoring.")
        return

    msg_type = message.get("type", "action_request")

    # ----- Control messages -----
    if msg_type == "emergency_stop":
        logger.critical("EMERGENCY STOP received from gateway.")
        config.EMERGENCY_STOP = True
        await ws.send(json.dumps({
            "type": "emergency_stop_ack",
            "status": "stopped",
        }))
        return

    if msg_type == "resume":
        logger.info("RESUME received from gateway.")
        config.EMERGENCY_STOP = False
        await ws.send(json.dumps({
            "type": "resume_ack",
            "status": "resumed",
        }))
        return

    if msg_type == "ping":
        await ws.send(json.dumps({"type": "pong"}))
        return

    # ----- Action requests -----
    if msg_type in ("action_request", "action"):
        response = await route(message)
        response["type"] = "action_response"
        await ws.send(json.dumps(response, default=str))
        return

    logger.warning("Unknown message type '%s' — ignoring.", msg_type)
