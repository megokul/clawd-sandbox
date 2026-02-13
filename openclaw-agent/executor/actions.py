"""
OpenClaw Local Execution Agent — Action Executors

Each public function in this module corresponds to exactly one permitted
action.  Functions receive validated, sanitised parameters and return a
plain dict that is serialised to JSON and sent back to the gateway.

SECURITY INVARIANTS
  - No function calls ``os.system``, ``eval``, ``exec``, or
    ``subprocess.Popen`` with user-controlled command strings.
  - Every ``subprocess`` invocation uses a **fixed argument list**.
  - Path parameters have already passed the jail check in the validator;
    they are used only as working-directory or target-file arguments.
"""

from __future__ import annotations

import asyncio
import os
import logging
from typing import Any

logger = logging.getLogger("openclaw.executor")

# Upper bound on how long any single subprocess may run (seconds).
_SUBPROCESS_TIMEOUT = 120


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _run(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _SUBPROCESS_TIMEOUT,
) -> dict[str, Any]:
    """
    Run a fixed argument list as an async subprocess.

    Returns a dict with ``returncode``, ``stdout``, and ``stderr``.
    """
    logger.debug("exec: %s  (cwd=%s)", args, cwd)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"Process timed out after {timeout}s and was killed.",
        }

    return {
        "returncode": proc.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace")[:8192],
        "stderr": stderr_bytes.decode("utf-8", errors="replace")[:4096],
    }


def _require_param(params: dict[str, Any], key: str) -> str:
    """Extract a required string parameter or raise."""
    value = params.get(key)
    if not value or not isinstance(value, str):
        raise ValueError(f"Missing required parameter: '{key}'")
    return value


# ------------------------------------------------------------------
# AUTO-tier actions
# ------------------------------------------------------------------

async def git_status(params: dict[str, Any]) -> dict[str, Any]:
    """Run ``git status`` in the given project directory."""
    cwd = _require_param(params, "working_dir")
    return await _run(["git", "status", "--porcelain"], cwd=cwd)


async def run_tests(params: dict[str, Any]) -> dict[str, Any]:
    """
    Run the project test suite.

    Supports ``runner`` = "pytest" | "npm" (default: pytest).
    """
    cwd = _require_param(params, "working_dir")
    runner = params.get("runner", "pytest")

    if runner == "pytest":
        return await _run(["python", "-m", "pytest", "--tb=short", "-q"], cwd=cwd)
    elif runner == "npm":
        return await _run(["npm", "test"], cwd=cwd)
    else:
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown runner: {runner}"}


async def lint_project(params: dict[str, Any]) -> dict[str, Any]:
    """
    Lint the project.

    Supports ``linter`` = "ruff" | "eslint" (default: ruff).
    """
    cwd = _require_param(params, "working_dir")
    linter = params.get("linter", "ruff")

    if linter == "ruff":
        return await _run(["python", "-m", "ruff", "check", "."], cwd=cwd)
    elif linter == "eslint":
        return await _run(["npx", "eslint", "."], cwd=cwd)
    else:
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown linter: {linter}"}


async def start_dev_server(params: dict[str, Any]) -> dict[str, Any]:
    """
    Start a dev server (non-blocking — returns immediately).

    Supports ``framework`` = "npm" | "uvicorn" (default: npm).
    """
    cwd = _require_param(params, "working_dir")
    framework = params.get("framework", "npm")

    if framework == "npm":
        # Fire-and-forget — just confirm it launched.
        proc = await asyncio.create_subprocess_exec(
            "npm", "run", "dev",
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return {"returncode": 0, "stdout": f"Dev server started (pid={proc.pid}).", "stderr": ""}
    elif framework == "uvicorn":
        app_module = params.get("app_module", "main:app")
        proc = await asyncio.create_subprocess_exec(
            "python", "-m", "uvicorn", app_module, "--reload",
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return {"returncode": 0, "stdout": f"Uvicorn started (pid={proc.pid}).", "stderr": ""}
    else:
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown framework: {framework}"}


async def build_project(params: dict[str, Any]) -> dict[str, Any]:
    """
    Build the project.

    Supports ``build_tool`` = "npm" | "python" (default: npm).
    """
    cwd = _require_param(params, "working_dir")
    tool = params.get("build_tool", "npm")

    if tool == "npm":
        return await _run(["npm", "run", "build"], cwd=cwd)
    elif tool == "python":
        return await _run(["python", "-m", "build"], cwd=cwd)
    else:
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown build tool: {tool}"}


# ------------------------------------------------------------------
# CONFIRM-tier actions
# ------------------------------------------------------------------

async def git_commit(params: dict[str, Any]) -> dict[str, Any]:
    """Stage all changes and commit with the supplied message."""
    cwd = _require_param(params, "working_dir")
    message = _require_param(params, "message")

    # Stage tracked changes only (no untracked files).
    stage_result = await _run(["git", "add", "-u"], cwd=cwd)
    if stage_result["returncode"] != 0:
        return stage_result

    return await _run(["git", "commit", "-m", message], cwd=cwd)


async def install_dependencies(params: dict[str, Any]) -> dict[str, Any]:
    """
    Install project dependencies.

    Supports ``manager`` = "pip" | "npm" (default: pip).
    """
    cwd = _require_param(params, "working_dir")
    manager = params.get("manager", "pip")

    if manager == "pip":
        req_file = os.path.join(cwd, "requirements.txt")
        return await _run(
            ["python", "-m", "pip", "install", "-r", req_file],
            cwd=cwd,
            timeout=300,
        )
    elif manager == "npm":
        return await _run(["npm", "install"], cwd=cwd, timeout=300)
    else:
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown manager: {manager}"}


async def file_write(params: dict[str, Any]) -> dict[str, Any]:
    """
    Write content to a file inside the allowed roots.

    The ``file`` parameter must already have passed the path-jail check.
    """
    filepath = _require_param(params, "file")
    content = params.get("content", "")

    if not isinstance(content, str):
        return {"returncode": 1, "stdout": "", "stderr": "content must be a string."}

    # Limit file size to 1 MB to prevent abuse.
    if len(content.encode("utf-8")) > 1_048_576:
        return {"returncode": 1, "stdout": "", "stderr": "Content exceeds 1 MB limit."}

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _write_file_sync, filepath, content)
    except OSError as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}

    return {"returncode": 0, "stdout": f"Wrote {len(content)} bytes to {filepath}.", "stderr": ""}


def _write_file_sync(filepath: str, content: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)


async def docker_build(params: dict[str, Any]) -> dict[str, Any]:
    """Build a Docker image from the project directory."""
    cwd = _require_param(params, "working_dir")
    tag = params.get("tag", "openclaw-build:latest")

    # Restrict tag to alphanumeric, dashes, underscores, dots, colons, slashes.
    import re
    if not re.match(r"^[a-zA-Z0-9._/:@-]+$", tag):
        return {"returncode": 1, "stdout": "", "stderr": "Invalid Docker tag characters."}

    return await _run(["docker", "build", "-t", tag, "."], cwd=cwd, timeout=600)


async def docker_compose_up(params: dict[str, Any]) -> dict[str, Any]:
    """Run ``docker compose up -d`` in the project directory."""
    cwd = _require_param(params, "working_dir")
    return await _run(["docker", "compose", "up", "-d"], cwd=cwd, timeout=300)


# ------------------------------------------------------------------
# Action registry — maps action name → executor function.
# The router uses this to dispatch; if an action is not in this dict
# it cannot be executed regardless of tier.
# ------------------------------------------------------------------

ACTION_REGISTRY: dict[str, Any] = {
    # AUTO
    "git_status": git_status,
    "run_tests": run_tests,
    "lint_project": lint_project,
    "start_dev_server": start_dev_server,
    "build_project": build_project,
    # CONFIRM
    "git_commit": git_commit,
    "install_dependencies": install_dependencies,
    "file_write": file_write,
    "docker_build": docker_build,
    "docker_compose_up": docker_compose_up,
}
