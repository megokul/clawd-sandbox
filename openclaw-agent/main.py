"""
OpenClaw Local Execution Agent — Entry Point

Usage:
    python main.py

Environment variables (required):
    OPENCLAW_AUTH_TOKEN     Pre-shared bearer token.
    OPENCLAW_GATEWAY_URL   WebSocket URL of the AWS OpenClaw gateway.

Optional:
    OPENCLAW_LOG_LEVEL     DEBUG | INFO | WARNING | ERROR (default: INFO)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

# Ensure project root is on sys.path so bare imports like ``import config``
# resolve correctly regardless of the caller's working directory.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402 — must come after path fixup
from connection.websocket_client import run_agent  # noqa: E402


def _configure_logging() -> None:
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
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
      |_|   Local Execution Agent v1.0.0

  Gateway : {gateway}
  Roots   : {roots}
  Rate    : {rate}/min
  Log dir : {logdir}
""".format(
            gateway=config.GATEWAY_URL,
            roots=", ".join(config.ALLOWED_ROOTS),
            rate=config.RATE_LIMIT_PER_MINUTE,
            logdir=config.AUDIT_LOG_DIR,
        )
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Graceful shutdown on Ctrl+C / SIGTERM."""
    def _shutdown(sig: signal.Signals) -> None:
        logging.getLogger("openclaw").info("Received %s — shutting down.", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    # Windows does not support add_signal_handler on the default event loop,
    # so fall back to signal.signal for SIGINT (Ctrl+C).
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda s, f: _shutdown(signal.Signals(s)))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown, sig)


async def _main() -> None:
    _configure_logging()
    _print_banner()

    logger = logging.getLogger("openclaw")

    # Pre-flight checks.
    if not config.AUTH_TOKEN:
        logger.error(
            "OPENCLAW_AUTH_TOKEN is not set. "
            "Export it before starting:\n"
            "  set OPENCLAW_AUTH_TOKEN=<your-token>"
        )
        sys.exit(1)

    logger.info("Agent starting.  Press Ctrl+C to stop.")

    try:
        await run_agent()
    except asyncio.CancelledError:
        logger.info("Agent stopped.")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(_main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
