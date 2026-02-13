"""
OpenClaw Local Execution Agent — Configuration

Central configuration for security policy, connection settings,
allowed actions, path restrictions, and rate limiting.

SECURITY NOTE: In production, load TOKEN and GATEWAY_URL from
environment variables or a secrets manager — never hardcode them.
"""

import os
from enum import Enum


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
GATEWAY_URL: str = os.environ.get(
    "OPENCLAW_GATEWAY_URL",
    "wss://100.50.2.232:8765/agent/ws",
)

# Pre-shared bearer token for WebSocket authentication.
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
AUTH_TOKEN: str = os.environ.get("OPENCLAW_AUTH_TOKEN", "")

# Seconds between reconnection attempts after a drop.
RECONNECT_DELAY_SECONDS: int = 5
MAX_RECONNECT_DELAY_SECONDS: int = 120

# WebSocket ping interval to keep NAT/firewall mappings alive.
WS_PING_INTERVAL_SECONDS: int = 30
WS_PING_TIMEOUT_SECONDS: int = 10


# ---------------------------------------------------------------------------
# Risk Tiers
# ---------------------------------------------------------------------------
class Tier(str, Enum):
    AUTO = "AUTO"         # Execute immediately, no confirmation.
    CONFIRM = "CONFIRM"   # Prompt operator in terminal before executing.
    BLOCKED = "BLOCKED"   # Never execute — reject instantly.


# ---------------------------------------------------------------------------
# Action → Tier mapping
# Only actions listed here are permitted. Everything else is BLOCKED.
# ---------------------------------------------------------------------------
AUTO_ACTIONS: set[str] = {
    "git_status",
    "run_tests",
    "lint_project",
    "start_dev_server",
    "build_project",
}

CONFIRM_ACTIONS: set[str] = {
    "git_commit",
    "install_dependencies",
    "file_write",
    "docker_build",
    "docker_compose_up",
}

# Explicitly listed so the validator can log attempts against known-bad ops.
BLOCKED_ACTIONS: set[str] = {
    "shell_exec",
    "format_disk",
    "modify_registry",
    "manage_users",
    "firewall_change",
    "download_exec",
    "eval_code",
}


# ---------------------------------------------------------------------------
# Path restrictions
# ---------------------------------------------------------------------------
ALLOWED_ROOTS: list[str] = [
    r"C:\Users\Gokul\Projects",
]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE: int = 30


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------
# When True, ALL actions (including AUTO) are rejected.
# Toggle at runtime via the /emergency-stop control message.
EMERGENCY_STOP: bool = False


# ---------------------------------------------------------------------------
# Logging / Audit
# ---------------------------------------------------------------------------
AUDIT_LOG_DIR: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "logs",
)
AUDIT_LOG_FILE: str = "audit.jsonl"
LOG_LEVEL: str = os.environ.get("OPENCLAW_LOG_LEVEL", "INFO")
