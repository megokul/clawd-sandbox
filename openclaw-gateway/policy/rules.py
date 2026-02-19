"""
SKYNET Policy Engine — Risk Classification Rules

Maps every known action to a risk level and defines permanently
blocked actions.  Used by the PolicyEngine to make approval decisions.

Risk Levels:
  READ_ONLY — No side effects.  Auto-approved.
  WRITE     — Modifies local state (files, git, packages).  Requires plan approval.
  ADMIN     — External-facing or destructive (push, deploy, docker).  Requires explicit approval.
"""

from __future__ import annotations


# Action → minimum risk level.
ACTION_RISK: dict[str, str] = {
    # READ_ONLY — safe, no side effects
    "file_read": "READ_ONLY",
    "list_directory": "READ_ONLY",
    "git_status": "READ_ONLY",
    "ollama_chat": "READ_ONLY",
    "run_tests": "READ_ONLY",
    "lint_project": "READ_ONLY",
    "start_dev_server": "READ_ONLY",

    # WRITE — modifies local state
    "file_write": "WRITE",
    "create_directory": "WRITE",
    "git_commit": "WRITE",
    "git_init": "WRITE",
    "git_add_all": "WRITE",
    "install_dependencies": "WRITE",
    "build_project": "WRITE",
    "docker_build": "WRITE",
    "zip_project": "WRITE",
    "open_in_vscode": "WRITE",
    "check_coding_agents": "READ_ONLY",
    "run_coding_agent": "WRITE",

    # ADMIN — external-facing or destructive
    "git_push": "ADMIN",
    "gh_create_repo": "ADMIN",
    "docker_compose_up": "ADMIN",
    "close_app": "ADMIN",
    "configure_coding_agent": "ADMIN",
}

# Actions that are always blocked, regardless of approval.
BLOCKED_ACTIONS: set[str] = {
    "shell_exec",
    "format_disk",
    "modify_registry",
    "manage_users",
    "firewall_change",
    "download_exec",
    "eval_code",
}

_RISK_ORDER = {"READ_ONLY": 0, "WRITE": 1, "ADMIN": 2}


def classify_action_risk(action: str) -> str:
    """Return the risk level for an action. Unknown actions default to ADMIN."""
    if action in BLOCKED_ACTIONS:
        return "BLOCKED"
    return ACTION_RISK.get(action, "ADMIN")


def risk_exceeds(action_risk: str, max_allowed: str) -> bool:
    """Return True if action_risk exceeds max_allowed level."""
    return _RISK_ORDER.get(action_risk, 2) > _RISK_ORDER.get(max_allowed, 1)
