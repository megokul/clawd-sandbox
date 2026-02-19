"""
CHATHAN Worker — Action Executors

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
import json
import os
import re
import logging
import shutil
from urllib import parse, request
from typing import Any

logger = logging.getLogger("chathan.executor")

# Upper bound on how long any single subprocess may run (seconds).
_SUBPROCESS_TIMEOUT = 120

# CLI resolution for local coding agents.
_CODING_AGENT_BINARIES: dict[str, str] = {
    "codex": os.environ.get("SKYNET_CODEX_BIN") or os.environ.get("OPENCLAW_CODEX_BIN") or "codex",
    "claude": os.environ.get("SKYNET_CLAUDE_BIN") or os.environ.get("OPENCLAW_CLAUDE_BIN") or "claude",
    "cline": os.environ.get("SKYNET_CLINE_BIN") or os.environ.get("OPENCLAW_CLINE_BIN") or "cline",
}
_CODING_AGENT_PREFIX_ARGS: dict[str, list[str]] = {
    "codex": ["exec"],
    "claude": ["-p"],
    "cline": ["-p"],
}
_CODING_AGENT_TIMEOUT_SECONDS = 1800
_BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
_WEB_SEARCH_TIMEOUT_SECONDS = 15


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


async def file_read(params: dict[str, Any]) -> dict[str, Any]:
    """Read the contents of a file (path-jailed, 64 KB cap)."""
    filepath = _require_param(params, "file")
    loop = asyncio.get_running_loop()
    try:
        content = await loop.run_in_executor(None, _read_file_sync, filepath)
        return {"returncode": 0, "stdout": content, "stderr": ""}
    except OSError as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


def _read_file_sync(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if len(content) > 65536:
        return content[:65536] + "\n... (truncated at 64 KB)"
    return content


async def list_directory(params: dict[str, Any]) -> dict[str, Any]:
    """List files and subdirectories (path-jailed)."""
    directory = _require_param(params, "directory")
    recursive = params.get("recursive", False) is True
    loop = asyncio.get_running_loop()
    try:
        listing = await loop.run_in_executor(
            None, _list_dir_sync, directory, recursive, 0,
        )
        return {"returncode": 0, "stdout": listing, "stderr": ""}
    except OSError as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


async def web_search(params: dict[str, Any]) -> dict[str, Any]:
    """Search the web from the laptop worker (Brave API with DDG fallback)."""
    query = _require_param(params, "query")
    raw_num = params.get("num_results", 5)
    try:
        num_results = int(raw_num)
    except (TypeError, ValueError):
        num_results = 5
    num_results = min(max(num_results, 1), 10)

    loop = asyncio.get_running_loop()
    if _BRAVE_SEARCH_API_KEY:
        try:
            output = await loop.run_in_executor(
                None, _brave_web_search_sync, query, num_results, _BRAVE_SEARCH_API_KEY,
            )
            return {"returncode": 0, "stdout": output, "stderr": ""}
        except Exception as exc:
            logger.warning("Brave web search failed: %s; falling back to DDG", exc)

    try:
        output = await loop.run_in_executor(None, _ddg_web_search_sync, query, num_results)
        return {"returncode": 0, "stdout": output, "stderr": ""}
    except Exception as exc:
        return {"returncode": 1, "stdout": "", "stderr": f"Web search failed: {exc}"}


def _brave_web_search_sync(query: str, num_results: int, api_key: str) -> str:
    url = (
        "https://api.search.brave.com/res/v1/web/search?"
        f"q={parse.quote_plus(query)}&count={num_results}"
    )
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
            "User-Agent": "SKYNET-Worker/1.0",
        },
    )
    with request.urlopen(req, timeout=_WEB_SEARCH_TIMEOUT_SECONDS) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
    results = data.get("web", {}).get("results", []) if isinstance(data, dict) else []
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, item in enumerate(results[:num_results], 1):
        title = str(item.get("title", "No title")).strip()
        link = str(item.get("url", "")).strip()
        desc = str(item.get("description", "No description")).strip()
        lines.append(f"{i}. {title}\n   URL: {link}\n   {desc}\n")
    return "\n".join(lines)


def _ddg_web_search_sync(query: str, num_results: int) -> str:
    url = f"https://lite.duckduckgo.com/lite/?q={parse.quote_plus(query)}"
    req = request.Request(url, headers={"User-Agent": "SKYNET-Worker/1.0"})
    with request.urlopen(req, timeout=_WEB_SEARCH_TIMEOUT_SECONDS) as resp:
        page = resp.read().decode("utf-8", errors="replace")

    results: list[str] = []
    links = re.findall(
        r'class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page,
        re.DOTALL,
    )
    for link, title in links[:num_results]:
        title_text = re.sub(r"<[^>]+>", "", title).strip()
        results.append(f"- {title_text}\n  URL: {link}")

    if results:
        return "\n".join(results)

    links = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>', page)
    for link, title in links:
        if "duckduckgo.com" in link:
            continue
        results.append(f"- {title.strip()}\n  URL: {link}")
        if len(results) >= num_results:
            break

    return "\n".join(results) if results else "No results found."


def _list_dir_sync(directory: str, recursive: bool, depth: int) -> str:
    MAX_DEPTH = 3
    MAX_ENTRIES = 500
    entries: list[str] = []
    count = 0
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        if count >= MAX_ENTRIES:
            entries.append("... (truncated)")
            break
        prefix = "  " * depth
        if entry.is_dir():
            entries.append(f"{prefix}[DIR] {entry.name}/")
            if recursive and depth < MAX_DEPTH:
                entries.append(_list_dir_sync(entry.path, True, depth + 1))
        else:
            size = entry.stat().st_size
            entries.append(f"{prefix}{entry.name}  ({size} bytes)")
        count += 1
    return "\n".join(entries)


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


async def create_directory(params: dict[str, Any]) -> dict[str, Any]:
    """Create a directory (and any missing parents)."""
    directory = _require_param(params, "directory")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, os.makedirs, directory, 0o755, True)
        return {"returncode": 0, "stdout": f"Created {directory}", "stderr": ""}
    except OSError as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


async def git_init(params: dict[str, Any]) -> dict[str, Any]:
    """Initialize a new git repository and set default branch to main."""
    cwd = _require_param(params, "working_dir")
    result = await _run(["git", "init"], cwd=cwd)
    if result["returncode"] == 0:
        await _run(["git", "checkout", "-b", "main"], cwd=cwd)
    return result


async def git_add_all(params: dict[str, Any]) -> dict[str, Any]:
    """Stage all changes including untracked files."""
    cwd = _require_param(params, "working_dir")
    return await _run(["git", "add", "-A"], cwd=cwd)


async def git_push(params: dict[str, Any]) -> dict[str, Any]:
    """Push to remote repository."""
    cwd = _require_param(params, "working_dir")
    remote = params.get("remote", "origin")
    branch = params.get("branch", "main")
    return await _run(["git", "push", "-u", remote, branch], cwd=cwd)


async def gh_create_repo(params: dict[str, Any]) -> dict[str, Any]:
    """Create a GitHub repository and set it as remote origin."""
    cwd = _require_param(params, "working_dir")
    repo_name = _require_param(params, "repo_name")
    description = params.get("description", "")
    private = params.get("private", False) is True

    if not re.match(r"^[a-zA-Z0-9._-]+$", repo_name):
        return {"returncode": 1, "stdout": "", "stderr": "Invalid repo name characters."}

    visibility = "--private" if private else "--public"
    args = ["gh", "repo", "create", repo_name, visibility, "--source=.", "--push"]
    if description:
        args.extend(["--description", description])

    return await _run(args, cwd=cwd, timeout=60)


async def open_in_vscode(params: dict[str, Any]) -> dict[str, Any]:
    """Open a path in VS Code."""
    path = _require_param(params, "path")
    return await _run(["code", path])


async def check_coding_agents(params: dict[str, Any]) -> dict[str, Any]:
    """Detect available coding agent CLIs on the laptop."""
    del params
    lines = []
    for name, binary in _CODING_AGENT_BINARIES.items():
        resolved = shutil.which(binary)
        if resolved:
            lines.append(f"{name}: available ({resolved})")
        else:
            lines.append(f"{name}: unavailable (expected binary: {binary})")
    return {
        "returncode": 0,
        "stdout": "\n".join(lines),
        "stderr": "",
    }


def _resolve_coding_binary(name: str) -> tuple[str, str]:
    """Resolve configured binary path for a coding agent."""
    binary = _CODING_AGENT_BINARIES[name]
    if os.path.isabs(binary):
        if os.path.exists(binary):
            return binary, binary
        return "", binary
    resolved = shutil.which(binary)
    return (resolved or "", binary)


async def run_coding_agent(params: dict[str, Any]) -> dict[str, Any]:
    """
    Run a local coding agent CLI in non-interactive mode.

    Supported agents: codex, claude, cline.
    """
    agent = _require_param(params, "agent").strip().lower()
    prompt = _require_param(params, "prompt")
    cwd = params.get("working_dir")
    timeout = params.get("timeout_seconds", _CODING_AGENT_TIMEOUT_SECONDS)

    if agent not in _CODING_AGENT_BINARIES:
        allowed = ", ".join(sorted(_CODING_AGENT_BINARIES.keys()))
        return {"returncode": 1, "stdout": "", "stderr": f"Unknown coding agent '{agent}'. Allowed: {allowed}"}
    if cwd is not None and not isinstance(cwd, str):
        return {"returncode": 1, "stdout": "", "stderr": "working_dir must be a string path."}
    if not isinstance(timeout, int) or timeout < 30 or timeout > 3600:
        return {"returncode": 1, "stdout": "", "stderr": "timeout_seconds must be an integer between 30 and 3600."}

    resolved, configured = _resolve_coding_binary(agent)
    if not resolved:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                f"{agent} CLI not found (configured '{configured}'). "
                f"Set SKYNET_{agent.upper()}_BIN or OPENCLAW_{agent.upper()}_BIN to the executable path."
            ),
        }

    args = [resolved, *_CODING_AGENT_PREFIX_ARGS[agent], prompt]
    return await _run(args, cwd=cwd, timeout=timeout)


async def docker_build(params: dict[str, Any]) -> dict[str, Any]:
    """Build a Docker image from the project directory."""
    cwd = _require_param(params, "working_dir")
    tag = params.get("tag", "chathan-build:latest")

    if not re.match(r"^[a-zA-Z0-9._/:@-]+$", tag):
        return {"returncode": 1, "stdout": "", "stderr": "Invalid Docker tag characters."}

    return await _run(["docker", "build", "-t", tag, "."], cwd=cwd, timeout=600)


async def docker_compose_up(params: dict[str, Any]) -> dict[str, Any]:
    """Run ``docker compose up -d`` in the project directory."""
    cwd = _require_param(params, "working_dir")
    return await _run(["docker", "compose", "up", "-d"], cwd=cwd, timeout=300)


async def close_app(params: dict[str, Any]) -> dict[str, Any]:
    """
    Close an application by its friendly name.

    Only applications in config.CLOSEABLE_APPS can be terminated.
    Uses ``taskkill /F /IM <process.exe>`` with a fixed argument list.
    """
    from config import CLOSEABLE_APPS

    app_name = _require_param(params, "app").lower()

    if app_name not in CLOSEABLE_APPS:
        allowed = ", ".join(sorted(CLOSEABLE_APPS.keys()))
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"'{app_name}' is not in the allowed list. Allowed: {allowed}",
        }

    exe_name = CLOSEABLE_APPS[app_name]
    return await _run(["taskkill", "/F", "/IM", exe_name])


async def zip_project(params: dict[str, Any]) -> dict[str, Any]:
    """
    Create a zip archive of a project directory and return as base64.

    Excludes heavy/generated directories: node_modules, __pycache__,
    .git, venv, .venv, dist, build.
    Cap: 10 MB after compression.
    """
    import base64
    import io
    import zipfile

    working_dir = _require_param(params, "working_dir")

    if not os.path.isdir(working_dir):
        return {"returncode": 1, "stdout": "", "stderr": f"Not a directory: {working_dir}"}

    EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build", ".next"}
    MAX_ZIP_SIZE = 10 * 1024 * 1024  # 10 MB

    buf = io.BytesIO()
    file_count = 0

    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(working_dir):
                # Skip excluded directories.
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for fname in files:
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, working_dir)
                    try:
                        zf.write(fpath, arcname)
                        file_count += 1
                    except (PermissionError, OSError):
                        continue  # Skip unreadable files.

                    # Check size periodically.
                    if buf.tell() > MAX_ZIP_SIZE:
                        return {
                            "returncode": 1,
                            "stdout": "",
                            "stderr": f"Zip exceeds {MAX_ZIP_SIZE // (1024*1024)} MB limit.",
                        }
    except Exception as exc:
        return {"returncode": 1, "stdout": "", "stderr": f"Zip error: {exc}"}

    zip_bytes = buf.getvalue()
    encoded = base64.b64encode(zip_bytes).decode("ascii")

    return {
        "returncode": 0,
        "stdout": encoded,
        "stderr": f"Zipped {file_count} files ({len(zip_bytes)} bytes)",
    }


# ------------------------------------------------------------------
# Action registry — maps action name → executor function.
# The router uses this to dispatch; if an action is not in this dict
# it cannot be executed regardless of tier.
# ------------------------------------------------------------------

# Import Ollama handler from its own module.
from executor.ollama import ollama_chat


ACTION_REGISTRY: dict[str, Any] = {
    # AUTO
    "git_status": git_status,
    "web_search": web_search,
    "run_tests": run_tests,
    "lint_project": lint_project,
    "start_dev_server": start_dev_server,
    "build_project": build_project,
    "file_read": file_read,
    "list_directory": list_directory,
    "ollama_chat": ollama_chat,
    "check_coding_agents": check_coding_agents,
    # CONFIRM
    "git_commit": git_commit,
    "install_dependencies": install_dependencies,
    "file_write": file_write,
    "create_directory": create_directory,
    "git_init": git_init,
    "git_add_all": git_add_all,
    "git_push": git_push,
    "gh_create_repo": gh_create_repo,
    "open_in_vscode": open_in_vscode,
    "run_coding_agent": run_coding_agent,
    "docker_build": docker_build,
    "docker_compose_up": docker_compose_up,
    "close_app": close_app,
    "zip_project": zip_project,
}
