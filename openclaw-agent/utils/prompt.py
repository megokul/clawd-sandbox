"""
OpenClaw Local Execution Agent — Terminal Confirmation Prompt

Asks the operator (the person sitting at the laptop) to approve a
CONFIRM-tier action before it executes.  Runs in a background thread
so the async event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("openclaw.prompt")


async def ask_confirmation(
    action: str,
    params: dict | None,
    request_id: str,
) -> bool:
    """
    Print the pending action to the terminal and wait for y/n input.

    Returns ``True`` if the operator approves, ``False`` otherwise.
    """
    border = "=" * 60
    summary = json.dumps(params, indent=2, default=str) if params else "{}"

    prompt_text = (
        f"\n{border}\n"
        f"  CONFIRM-TIER ACTION REQUESTED\n"
        f"{border}\n"
        f"  Request ID : {request_id}\n"
        f"  Action     : {action}\n"
        f"  Parameters :\n{_indent(summary, 4)}\n"
        f"{border}\n"
        f"  Approve execution? [y/N]: "
    )

    loop = asyncio.get_running_loop()
    # Run blocking input() in a thread executor.
    answer: str = await loop.run_in_executor(None, _blocking_input, prompt_text)

    approved = answer.strip().lower() in ("y", "yes")
    if approved:
        logger.info("Operator APPROVED action '%s' (req=%s)", action, request_id)
    else:
        logger.info("Operator DENIED action '%s' (req=%s)", action, request_id)
    return approved


def _blocking_input(prompt_text: str) -> str:
    """Wrapper around built-in ``input`` for use in an executor."""
    try:
        return input(prompt_text)
    except EOFError:
        return "n"


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())
