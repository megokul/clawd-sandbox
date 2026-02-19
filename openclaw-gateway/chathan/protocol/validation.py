"""
CHATHAN Protocol — Specification Validation

Validates ExecutionSpecs before they are dispatched to workers.
Catches configuration errors, missing parameters, and policy
violations before any execution begins.
"""

from __future__ import annotations

from typing import Any

from .execution_spec import ExecutionSpec, ExecutionStep


# Actions recognized by the CHATHAN Worker.
KNOWN_ACTIONS: set[str] = {
    # READ_ONLY
    "file_read", "list_directory", "git_status", "ollama_chat", "web_search",
    # WRITE
    "file_write", "create_directory", "git_commit", "git_init",
    "git_add_all", "install_dependencies", "run_tests", "lint_project",
    "start_dev_server", "build_project", "docker_build",
    # ADMIN
    "git_push", "gh_create_repo", "docker_compose_up",
    "close_app", "zip_project", "open_in_vscode",
}

# Action → minimum risk level required.
ACTION_RISK: dict[str, str] = {
    "file_read": "READ_ONLY",
    "list_directory": "READ_ONLY",
    "git_status": "READ_ONLY",
    "ollama_chat": "READ_ONLY",
    "web_search": "READ_ONLY",
    "run_tests": "READ_ONLY",
    "lint_project": "READ_ONLY",
    "start_dev_server": "READ_ONLY",
    "build_project": "WRITE",
    "file_write": "WRITE",
    "create_directory": "WRITE",
    "git_commit": "WRITE",
    "git_init": "WRITE",
    "git_add_all": "WRITE",
    "install_dependencies": "WRITE",
    "docker_build": "WRITE",
    "git_push": "ADMIN",
    "gh_create_repo": "ADMIN",
    "docker_compose_up": "ADMIN",
    "close_app": "ADMIN",
    "zip_project": "WRITE",
    "open_in_vscode": "WRITE",
}

# Required parameters per action.
ACTION_REQUIRED_PARAMS: dict[str, list[str]] = {
    "file_read": ["file"],
    "file_write": ["file", "content"],
    "list_directory": ["directory"],
    "create_directory": ["directory"],
    "git_status": ["working_dir"],
    "git_commit": ["working_dir", "message"],
    "git_init": ["working_dir"],
    "git_add_all": ["working_dir"],
    "git_push": ["working_dir"],
    "gh_create_repo": ["working_dir", "repo_name"],
    "run_tests": ["working_dir"],
    "lint_project": ["working_dir"],
    "start_dev_server": ["working_dir"],
    "build_project": ["working_dir"],
    "install_dependencies": ["working_dir"],
    "docker_build": ["working_dir"],
    "docker_compose_up": ["working_dir"],
    "close_app": ["app"],
    "zip_project": ["working_dir"],
    "open_in_vscode": ["path"],
    "ollama_chat": ["messages"],
    "web_search": ["query"],
}

_RISK_ORDER = {"READ_ONLY": 0, "WRITE": 1, "ADMIN": 2}


def validate_spec(spec: ExecutionSpec) -> list[str]:
    """
    Validate an ExecutionSpec and return a list of errors.

    An empty list means the spec is valid.
    """
    errors: list[str] = []

    if not spec.job_id:
        errors.append("Missing job_id.")

    if not spec.project_id:
        errors.append("Missing project_id.")

    if spec.risk_level not in _RISK_ORDER:
        errors.append(f"Invalid risk_level: '{spec.risk_level}'. Must be READ_ONLY, WRITE, or ADMIN.")

    if not spec.steps:
        errors.append("ExecutionSpec has no steps.")

    # Check that the sandbox_root is set for actions that need a working directory.
    needs_sandbox = any(
        "working_dir" in ACTION_REQUIRED_PARAMS.get(s.action, [])
        for s in spec.steps
    )
    if needs_sandbox and not spec.sandbox_root:
        errors.append("sandbox_root is required when steps reference working_dir.")

    # Validate each step.
    for i, step in enumerate(spec.steps):
        step_errors = validate_step_params(step)
        for err in step_errors:
            errors.append(f"Step {i} ({step.action}): {err}")

        # Check step risk doesn't exceed spec risk.
        step_risk = ACTION_RISK.get(step.action, "WRITE")
        if _RISK_ORDER.get(step_risk, 1) > _RISK_ORDER.get(spec.risk_level, 1):
            errors.append(
                f"Step {i} ({step.action}) requires {step_risk} but spec risk is {spec.risk_level}."
            )

    return errors


def validate_step_params(step: ExecutionStep) -> list[str]:
    """
    Validate that a step's action is known and has required parameters.

    Returns a list of errors (empty = valid).
    """
    errors: list[str] = []

    if not step.action:
        errors.append("Missing action.")
        return errors

    if step.action not in KNOWN_ACTIONS:
        errors.append(f"Unknown action: '{step.action}'.")
        return errors

    required = ACTION_REQUIRED_PARAMS.get(step.action, [])
    for param in required:
        if param not in step.params:
            errors.append(f"Missing required parameter: '{param}'.")

    if step.timeout_sec <= 0:
        errors.append(f"Invalid timeout: {step.timeout_sec}s (must be > 0).")

    return errors
