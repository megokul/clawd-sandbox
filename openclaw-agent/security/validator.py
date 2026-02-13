"""
OpenClaw Local Execution Agent — Security Validator

Centralises every security gate:
  1. Emergency-stop check.
  2. Action allow-list lookup + tier resolution.
  3. Path-jail enforcement for any parameter that references a filesystem path.
  4. Parameter sanitisation (no embedded shell metacharacters).

All public functions raise ``SecurityViolation`` on failure so the caller
can catch a single exception type and respond with a structured rejection.
"""

from __future__ import annotations

import os
import re
from typing import Any

import config
from config import (
    ALLOWED_ROOTS,
    AUTO_ACTIONS,
    BLOCKED_ACTIONS,
    CONFIRM_ACTIONS,
    Tier,
)


class SecurityViolation(Exception):
    """Raised when a request fails any security gate."""

    def __init__(self, reason: str, *, action: str = "", tier: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.action = action
        self.tier = tier


# Pre-compile the canonical forms of allowed roots once at import time.
_CANONICAL_ROOTS: list[str] = [
    os.path.realpath(os.path.normcase(r)) for r in ALLOWED_ROOTS
]

# Shell meta-characters that must never appear in a parameter value.
_SHELL_META = re.compile(r"[;&|`$(){}!<>\"\']")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def check_emergency_stop() -> None:
    """Reject immediately if the global kill switch is active."""
    if config.EMERGENCY_STOP:
        raise SecurityViolation(
            "Emergency stop is active — all execution suspended.",
            tier="BLOCKED",
        )


def resolve_tier(action: str) -> Tier:
    """Return the risk tier for *action*, or BLOCKED if unknown."""
    if action in AUTO_ACTIONS:
        return Tier.AUTO
    if action in CONFIRM_ACTIONS:
        return Tier.CONFIRM
    # Anything not explicitly allowed is blocked, whether it appears in
    # BLOCKED_ACTIONS or is completely unknown.
    return Tier.BLOCKED


def validate_action(action: str) -> Tier:
    """
    Full validation pipeline for an action name.

    Returns the resolved ``Tier`` on success.
    Raises ``SecurityViolation`` if the action is blocked.
    """
    check_emergency_stop()

    tier = resolve_tier(action)
    if tier is Tier.BLOCKED:
        known = action in BLOCKED_ACTIONS
        raise SecurityViolation(
            f"Action '{action}' is {'explicitly' if known else 'implicitly'} blocked.",
            action=action,
            tier="BLOCKED",
        )
    return tier


def validate_path(path: str) -> str:
    """
    Ensure *path* resolves to a location inside one of the ``ALLOWED_ROOTS``.

    Returns the resolved canonical path on success.
    Raises ``SecurityViolation`` if the path escapes the jail.
    """
    if not path:
        raise SecurityViolation("Empty path supplied.")

    canonical = os.path.realpath(os.path.normcase(path))

    for root in _CANONICAL_ROOTS:
        # os.path.commonpath raises ValueError if paths are on different
        # drives on Windows, which is itself a rejection signal.
        try:
            if os.path.commonpath([canonical, root]) == root:
                return canonical
        except ValueError:
            continue

    raise SecurityViolation(
        f"Path '{path}' (resolved: '{canonical}') is outside allowed roots.",
    )


def validate_params(params: dict[str, Any] | None) -> None:
    """
    Shallow sanitisation of parameter values.

    * Rejects shell metacharacters in string values.
    * Rejects excessively long values (> 4 096 chars).
    """
    if not params:
        return

    for key, value in params.items():
        if not isinstance(value, str):
            continue
        if len(value) > 4096:
            raise SecurityViolation(
                f"Parameter '{key}' exceeds maximum length (4096 chars).",
            )
        if _SHELL_META.search(value):
            raise SecurityViolation(
                f"Parameter '{key}' contains disallowed shell metacharacters.",
            )


def validate_path_params(params: dict[str, Any] | None) -> None:
    """
    If *params* contains keys that look like filesystem paths
    (``path``, ``directory``, ``project_dir``, ``file``), validate
    each one against the path jail.
    """
    if not params:
        return

    path_keys = {"path", "directory", "project_dir", "file", "working_dir"}
    for key in path_keys:
        value = params.get(key)
        if isinstance(value, str) and value:
            params[key] = validate_path(value)
