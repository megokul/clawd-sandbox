"""
OpenClaw Gateway Server — Entry Point

Starts both the WebSocket server (public, port 8765) and the
HTTP API (loopback, port 8766) in a single asyncio event loop.

Usage:
    python main.py

Environment variables (required):
    OPENCLAW_AUTH_TOKEN     Shared secret — must match the laptop agent.

Optional:
    OPENCLAW_LOG_LEVEL     DEBUG | INFO | WARNING | ERROR (default: INFO)
    OPENCLAW_TLS_CERT      Path to TLS certificate.
    OPENCLAW_TLS_KEY       Path to TLS private key.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import gateway_config as cfg
from gateway import start_ws_server
from api import start_http_api


def _configure_logging() -> None:
    level = getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _print_banner() -> None:
    print(
        r"""
  ___                    ____ _
 / _ \ _ __   ___ _ __  / ___| | __ ___      __
| | | | '_ \ / _ \ '_ \| |   | |/ _` \ \ /\ / /
| |_| | |_) |  __/ | | | |___| | (_| |\ V  V /
 \___/| .__/ \___|_| |_|\____|_|\__,_| \_/\_/
      |_|   Gateway Server v1.0.0

  WebSocket : 0.0.0.0:{ws_port}
  HTTP API  : {http_host}:{http_port}
  TLS cert  : {tls}
""".format(
            ws_port=cfg.WS_PORT,
            http_host=cfg.HTTP_HOST,
            http_port=cfg.HTTP_PORT,
            tls=cfg.TLS_CERT if cfg.TLS_CERT else "DISABLED",
        )
    )


async def _main() -> None:
    _configure_logging()
    _print_banner()

    logger = logging.getLogger("openclaw")

    if not cfg.AUTH_TOKEN:
        logger.error(
            "OPENCLAW_AUTH_TOKEN is not set.\n"
            "  export OPENCLAW_AUTH_TOKEN=$(python3 -c "
            "\"import secrets; print(secrets.token_urlsafe(48))\")"
        )
        sys.exit(1)

    # Start both servers.
    ws_server = await start_ws_server()
    http_runner = await start_http_api()

    logger.info("Gateway ready. Waiting for agent connections…")

    try:
        # Run forever.
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        await http_runner.cleanup()
        logger.info("Gateway shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
