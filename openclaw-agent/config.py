"""
CHATHAN Worker — Configuration

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
    "SKYNET_GATEWAY_URL",
    os.environ.get("OPENCLAW_GATEWAY_URL", "wss://100.50.2.232:8765/agent/ws"),
)

# Pre-shared bearer token for WebSocket authentication.
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
AUTH_TOKEN: str = os.environ.get(
    "SKYNET_AUTH_TOKEN", os.environ.get("OPENCLAW_AUTH_TOKEN", ""),
)

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
    "web_search",
    "run_tests",
    "lint_project",
    "start_dev_server",
    "build_project",
    "file_read",
    "list_directory",
    "ollama_chat",
    "check_coding_agents",
}

CONFIRM_ACTIONS: set[str] = {
    "git_commit",
    "install_dependencies",
    "file_write",
    "create_directory",
    "git_init",
    "git_add_all",
    "git_push",
    "gh_create_repo",
    "open_in_vscode",
    "run_coding_agent",
    "docker_build",
    "docker_compose_up",
    "close_app",
    "zip_project",
}

# Hardcoded allowlist of process names that close_app can terminate.
# Only these executables can be closed — anything else is rejected.
CLOSEABLE_APPS: dict[str, str] = {
    "chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "edge": "msedge.exe",
    "notepad": "notepad.exe",
    "code": "Code.exe",
    "explorer": "explorer.exe",
    "slack": "slack.exe",
    "discord": "Discord.exe",
    "spotify": "Spotify.exe",
    "teams": "Teams.exe",
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
def _parse_allowed_roots() -> list[str]:
    """
    Resolve path-jail roots from environment or platform defaults.

    Environment:
      SKYNET_ALLOWED_ROOTS / OPENCLAW_ALLOWED_ROOTS
      Delimiters: ';' or ',' (portable across Windows/Linux).
    """
    raw = os.environ.get("SKYNET_ALLOWED_ROOTS") or os.environ.get("OPENCLAW_ALLOWED_ROOTS")
    if raw:
        roots = [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]
        if roots:
            return roots

    if os.name == "nt":
        return [
            r"E:\MyProjects",
            r"E:\OpenClaw\projects",
        ]

    # Linux default for EC2-hosted workers.
    return ["/home/ubuntu", "/tmp"]


ALLOWED_ROOTS: list[str] = _parse_allowed_roots()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE: int = 120


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
LOG_LEVEL: str = os.environ.get(
    "SKYNET_LOG_LEVEL", os.environ.get("OPENCLAW_LOG_LEVEL", "INFO"),
)
