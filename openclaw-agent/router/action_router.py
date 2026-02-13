"""
OpenClaw Local Execution Agent — Action Router

Orchestrates the full lifecycle of an inbound action request:

  1. Rate-limit gate
  2. Security validation (emergency stop, tier resolution, path jail, params)
  3. Tier-based dispatch:
     - AUTO  → execute immediately
     - CONFIRM → prompt operator → execute on approval
     - BLOCKED → reject
  4. Audit logging (success or failure)
  5. Structured JSON response back to caller
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from audit.logger import log_event
from config import Tier
from executor.actions import ACTION_REGISTRY
from security.rate_limiter import RateLimitExceeded, SlidingWindowRateLimiter
from security.validator import (
    SecurityViolation,
    validate_action,
    validate_params,
    validate_path_params,
)
from utils.prompt import ask_confirmation

logger = logging.getLogger("openclaw.router")

# Module-level rate limiter — shared across the agent lifetime.
_rate_limiter = SlidingWindowRateLimiter()


# ------------------------------------------------------------------
# Response builders
# ------------------------------------------------------------------

def _success_response(
    request_id: str,
    action: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": "success",
        "action": action,
        "result": result,
    }


def _error_response(
    request_id: str,
    action: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": "error",
        "action": action,
        "error": reason,
    }


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

async def route(message: dict[str, Any]) -> dict[str, Any]:
    """
    Process one inbound action message and return a structured response.

    Expected message schema::

        {
            "request_id": "optional-uuid",
            "action": "git_status",
            "params": { "working_dir": "C:\\Users\\Gokul\\Projects\\myapp" }
        }
    """
    request_id: str = message.get("request_id") or str(uuid.uuid4())
    action: str = message.get("action", "")
    params: dict[str, Any] | None = message.get("params")

    tier_label = "UNKNOWN"
    start = time.monotonic()

    try:
        # ---- Gate 1: Rate limit ----
        await _rate_limiter.acquire()

        # ---- Gate 2: Security validation ----
        tier: Tier = validate_action(action)
        tier_label = tier.value

        validate_params(params)
        validate_path_params(params)

        # ---- Gate 3: Ensure executor exists ----
        executor_fn = ACTION_REGISTRY.get(action)
        if executor_fn is None:
            raise SecurityViolation(
                f"No executor registered for action '{action}'.",
                action=action,
                tier=tier_label,
            )

        # ---- Gate 4: Tier dispatch ----
        if tier is Tier.CONFIRM:
            approved = await ask_confirmation(action, params, request_id)
            if not approved:
                await log_event(
                    request_id=request_id,
                    action=action,
                    tier=tier_label,
                    params=params,
                    outcome="DENIED_BY_OPERATOR",
                    duration_ms=_elapsed_ms(start),
                )
                return _error_response(
                    request_id, action, "Operator denied the action."
                )

        # ---- Execute ----
        logger.info("Executing %s (tier=%s, req=%s)", action, tier_label, request_id)
        result = await executor_fn(params or {})

        await log_event(
            request_id=request_id,
            action=action,
            tier=tier_label,
            params=params,
            outcome="EXECUTED",
            detail=result,
            duration_ms=_elapsed_ms(start),
        )
        return _success_response(request_id, action, result)

    except RateLimitExceeded as exc:
        logger.warning("Rate limit hit for req=%s: %s", request_id, exc)
        await log_event(
            request_id=request_id,
            action=action,
            tier=tier_label,
            params=params,
            outcome="RATE_LIMITED",
            detail=str(exc),
            duration_ms=_elapsed_ms(start),
        )
        return _error_response(request_id, action, str(exc))

    except SecurityViolation as exc:
        logger.warning("Security violation for req=%s: %s", request_id, exc.reason)
        await log_event(
            request_id=request_id,
            action=action,
            tier=exc.tier or tier_label,
            params=params,
            outcome="BLOCKED",
            detail=exc.reason,
            duration_ms=_elapsed_ms(start),
        )
        return _error_response(request_id, action, exc.reason)

    except Exception as exc:
        logger.exception("Unexpected error processing req=%s", request_id)
        await log_event(
            request_id=request_id,
            action=action,
            tier=tier_label,
            params=params,
            outcome="INTERNAL_ERROR",
            detail=str(exc),
            duration_ms=_elapsed_ms(start),
        )
        return _error_response(request_id, action, "Internal agent error.")


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 2)
