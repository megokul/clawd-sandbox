"""
SKYNET Gateway - SSH Tunnel Action Executor

Fallback execution path when no OpenClaw worker is connected.
Runs allowlisted actions directly on a remote laptop over SSH.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import base64
import time
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import paramiko

import bot_config as bot_cfg
from search.web_search import WebSearcher


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_roots(raw: str, remote_os: str) -> list[str]:
    parts = [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]
    if parts:
        return parts
    if remote_os == "windows":
        return [r"E:\MyProjects"]
    return ["/home", "/tmp"]


def _parse_provider_priority(raw: str) -> list[str]:
    allowed = {"gemini", "deepseek", "groq", "openrouter", "openai", "anthropic"}
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
    ordered: list[str] = []
    for part in parts:
        if part in allowed and part not in ordered:
            ordered.append(part)
    if ordered:
        return ordered
    return ["gemini", "deepseek", "groq", "openrouter"]


def _norm_remote_path(path: str, remote_os: str) -> str:
    if remote_os == "windows":
        return str(PureWindowsPath(path))
    return str(PurePosixPath(path))


def _is_allowed_path(path: str, allowed_roots: list[str], remote_os: str) -> bool:
    candidate = _norm_remote_path(path, remote_os)
    if remote_os == "windows":
        cand = candidate.replace("/", "\\").rstrip("\\").lower()
        for root in allowed_roots:
            r = _norm_remote_path(root, remote_os).replace("/", "\\").rstrip("\\").lower()
            if cand == r or cand.startswith(r + "\\"):
                return True
        return False

    cand = candidate.rstrip("/")
    for root in allowed_roots:
        r = _norm_remote_path(root, remote_os).rstrip("/")
        if cand == r or cand.startswith(r + "/"):
            return True
    return False


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_windows_command(args: list[str], cwd: str | None = None) -> str:
    cmd = " ".join(_ps_quote(str(a)) for a in args)
    script_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
    ]
    if cwd:
        script_lines.append(f"Set-Location -LiteralPath {_ps_quote(cwd)}")
    script_lines.append(f"& {cmd}")
    script_lines.append("$code = $LASTEXITCODE")
    script_lines.append("if ($null -eq $code) { $code = 0 }")
    script_lines.append("exit $code")
    script = "\n".join(script_lines)
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded}"
    )


def _sanitize_powershell_output(text: str) -> str:
    if not text:
        return text
    cleaned = text.replace("_x000D__x000A_", "\n").replace("_x000D_", "\r").replace("_x000A_", "\n")
    if "<Objs Version=" in cleaned and "</Objs>" in cleaned:
        # Keep only message payloads from CLIXML blocks.
        parts = re.findall(r"<S S=\"(?:Error|Warning|Verbose)\">(.*?)</S>", cleaned, flags=re.DOTALL)
        if parts:
            cleaned = "\n".join(parts)
        else:
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return cleaned.strip()


def _build_linux_command(args: list[str], cwd: str | None = None) -> str:
    import shlex

    run = " ".join(shlex.quote(str(a)) for a in args)
    if cwd:
        return f"cd {shlex.quote(cwd)} && {run}"
    return run


class SSHTunnelExecutor:
    """Remote action executor using SSH."""

    _PATH_KEYS = {"working_dir", "directory", "file", "path", "project_dir"}

    def __init__(self) -> None:
        mode = os.environ.get("OPENCLAW_EXECUTION_MODE", "").strip().lower()
        self.enabled = mode in {"ssh", "ssh_tunnel", "tunnel", "ssh-only"} or _env_bool("OPENCLAW_SSH_FALLBACK_ENABLED", False)

        self.host = os.environ.get("OPENCLAW_SSH_HOST", "127.0.0.1").strip()
        self.port = _env_int("OPENCLAW_SSH_PORT", 2222)
        self.username = os.environ.get("OPENCLAW_SSH_USER", "").strip()
        self.password = os.environ.get("OPENCLAW_SSH_PASSWORD", "")
        self.key_path = os.environ.get("OPENCLAW_SSH_KEY_PATH", "").strip()
        self.connect_timeout = _env_int("OPENCLAW_SSH_CONNECT_TIMEOUT", 4)
        self.command_timeout = _env_int("OPENCLAW_SSH_COMMAND_TIMEOUT", 180)
        self.remote_os = os.environ.get("OPENCLAW_SSH_REMOTE_OS", "windows").strip().lower()
        self.strict_host_key = _env_bool("OPENCLAW_SSH_STRICT_HOST_KEY", False)
        roots_raw = os.environ.get("OPENCLAW_SSH_ALLOWED_ROOTS", "")
        self.allowed_roots = _parse_roots(roots_raw, self.remote_os)

        self._searcher = WebSearcher(bot_cfg.BRAVE_SEARCH_API_KEY)
        self._coding_bins = {
            "codex": os.environ.get("OPENCLAW_SSH_CODEX_BIN", "codex"),
            "claude": os.environ.get("OPENCLAW_SSH_CLAUDE_BIN", "claude"),
            "cline": os.environ.get("OPENCLAW_SSH_CLINE_BIN", "cline"),
        }
        self._coding_prefix = {
            "codex": ["exec"],
            "claude": ["-p"],
            "cline": ["-p"],
        }
        self._closeable_apps = {
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
        self._health_cache_seconds = _env_int("OPENCLAW_SSH_HEALTH_CACHE_SECONDS", 15)
        self._last_health_at = 0.0
        self._last_health: tuple[bool, str] = (False, "SSH health not checked yet")
        self._cline_auto_switch = _env_bool("OPENCLAW_CLINE_AUTO_SWITCH", True)
        self._cline_provider_priority = _parse_provider_priority(
            os.environ.get("OPENCLAW_CLINE_PROVIDER_PRIORITY", ""),
        )
        self._cline_provider_base_urls = {
            "gemini": os.environ.get("OPENCLAW_CLINE_GEMINI_BASE_URL", "").strip(),
            "deepseek": os.environ.get("OPENCLAW_CLINE_DEEPSEEK_BASE_URL", "").strip(),
            "groq": os.environ.get("OPENCLAW_CLINE_GROQ_BASE_URL", "").strip(),
            "openrouter": os.environ.get("OPENCLAW_CLINE_OPENROUTER_BASE_URL", "").strip(),
            "openai": os.environ.get("OPENCLAW_CLINE_OPENAI_BASE_URL", "").strip(),
            "anthropic": os.environ.get("OPENCLAW_CLINE_ANTHROPIC_BASE_URL", "").strip(),
        }

    def is_configured(self) -> bool:
        return self.enabled and bool(self.username and self.host)

    async def health_check(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SSH executor not configured"
        now = time.time()
        if now - self._last_health_at < max(self._health_cache_seconds, 1):
            return self._last_health
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._probe_sync)
            self._last_health = (True, f"{self.username}@{self.host}:{self.port}")
        except Exception as exc:
            self._last_health = (False, str(exc))
        self._last_health_at = now
        return self._last_health

    async def execute_action(
        self,
        action: str,
        params: dict[str, Any],
        confirmed: bool = True,
    ) -> dict[str, Any]:
        del confirmed
        if not self.is_configured():
            return {"status": "error", "action": action, "error": "SSH fallback is not configured."}

        params = dict(params or {})
        for key in self._PATH_KEYS:
            if isinstance(params.get(key), str):
                val = _norm_remote_path(params[key], self.remote_os)
                if not _is_allowed_path(val, self.allowed_roots, self.remote_os):
                    return {
                        "status": "error",
                        "action": action,
                        "error": f"Path '{params[key]}' is outside OPENCLAW_SSH_ALLOWED_ROOTS.",
                    }
                params[key] = val

        if action == "web_search":
            try:
                query = str(params.get("query") or "").strip()
                if not query:
                    raise ValueError("Missing required parameter: 'query'")
                raw_num = params.get("num_results", 5)
                num = int(raw_num) if isinstance(raw_num, (int, str)) else 5
                num = min(max(num, 1), 10)
                output = await self._searcher.search(query, num)
                return {
                    "status": "ok",
                    "action": action,
                    "result": {"returncode": 0, "stdout": output, "stderr": ""},
                }
            except Exception as exc:
                return {
                    "status": "ok",
                    "action": action,
                    "result": {"returncode": 1, "stdout": "", "stderr": f"Web search failed: {exc}"},
                }

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._execute_sync, action, params)
            return {"status": "ok", "action": action, "result": result}
        except Exception as exc:
            return {"status": "error", "action": action, "error": f"SSH action failed: {exc}"}

    def _probe_sync(self) -> None:
        client = self._connect()
        try:
            probe_args = ["cmd", "/c", "echo", "ok"] if self.remote_os == "windows" else ["sh", "-lc", "echo ok"]
            command = self._build_command(probe_args, cwd=None)
            _, stdout, stderr = client.exec_command(command, timeout=self.connect_timeout)
            _ = stdout.read()
            _ = stderr.read()
        finally:
            client.close()

    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        if self.strict_host_key:
            client.load_system_host_keys()
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "look_for_keys": True,
            "allow_agent": True,
        }
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        if self.password:
            kwargs["password"] = self.password
        client.connect(**kwargs)
        return client

    def _execute_sync(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._connect()
        try:
            if action == "file_read":
                return self._file_read(client, params)
            if action == "file_write":
                return self._file_write(client, params)
            if action == "create_directory":
                return self._create_directory(client, params)
            if action == "list_directory":
                return self._list_directory(client, params)
            return self._run_command_action(client, action, params)
        finally:
            client.close()

    def _build_command(self, args: list[str], cwd: str | None) -> str:
        if self.remote_os == "windows":
            return _build_windows_command(args, cwd=cwd)
        return _build_linux_command(args, cwd=cwd)

    def _require_str(self, params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not value or not isinstance(value, str):
            raise ValueError(f"Missing required parameter: '{key}'")
        return value

    def _run_command(
        self,
        client: paramiko.SSHClient,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        command = self._build_command(args, cwd=cwd)
        _, stdout, stderr = client.exec_command(
            command,
            timeout=timeout or self.command_timeout,
        )
        out = stdout.read().decode("utf-8", errors="replace")[:8192]
        err = stderr.read().decode("utf-8", errors="replace")[:4096]
        if self.remote_os == "windows":
            out = _sanitize_powershell_output(out)
            err = _sanitize_powershell_output(err)
        rc = stdout.channel.recv_exit_status()
        return {"returncode": int(rc), "stdout": out, "stderr": err}

    def _run_command_action(self, client: paramiko.SSHClient, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "git_status":
            cwd = self._require_str(params, "working_dir")
            return self._run_command(client, ["git", "status", "--porcelain"], cwd=cwd)

        if action == "run_tests":
            cwd = self._require_str(params, "working_dir")
            runner = params.get("runner", "pytest")
            if runner == "pytest":
                return self._run_command(client, ["python", "-m", "pytest", "--tb=short", "-q"], cwd=cwd)
            if runner == "npm":
                return self._run_command(client, ["npm", "test"], cwd=cwd)
            return {"returncode": 1, "stdout": "", "stderr": f"Unknown runner: {runner}"}

        if action == "lint_project":
            cwd = self._require_str(params, "working_dir")
            linter = params.get("linter", "ruff")
            if linter == "ruff":
                return self._run_command(client, ["python", "-m", "ruff", "check", "."], cwd=cwd)
            if linter == "eslint":
                return self._run_command(client, ["npx", "eslint", "."], cwd=cwd)
            return {"returncode": 1, "stdout": "", "stderr": f"Unknown linter: {linter}"}

        if action == "build_project":
            cwd = self._require_str(params, "working_dir")
            tool = params.get("build_tool", "npm")
            if tool == "npm":
                return self._run_command(client, ["npm", "run", "build"], cwd=cwd)
            if tool == "python":
                return self._run_command(client, ["python", "-m", "build"], cwd=cwd)
            return {"returncode": 1, "stdout": "", "stderr": f"Unknown build tool: {tool}"}

        if action == "install_dependencies":
            cwd = self._require_str(params, "working_dir")
            manager = params.get("manager", "pip")
            if manager == "pip":
                req_file = self._norm_join(cwd, "requirements.txt")
                return self._run_command(
                    client, ["python", "-m", "pip", "install", "-r", req_file], cwd=cwd, timeout=300,
                )
            if manager == "npm":
                return self._run_command(client, ["npm", "install"], cwd=cwd, timeout=300)
            return {"returncode": 1, "stdout": "", "stderr": f"Unknown manager: {manager}"}

        if action == "git_init":
            cwd = self._require_str(params, "working_dir")
            result = self._run_command(client, ["git", "init"], cwd=cwd)
            if result["returncode"] == 0:
                _ = self._run_command(client, ["git", "checkout", "-b", "main"], cwd=cwd)
            return result

        if action == "git_add_all":
            cwd = self._require_str(params, "working_dir")
            return self._run_command(client, ["git", "add", "-A"], cwd=cwd)

        if action == "git_commit":
            cwd = self._require_str(params, "working_dir")
            message = self._require_str(params, "message")
            stage = self._run_command(client, ["git", "add", "-u"], cwd=cwd)
            if stage["returncode"] != 0:
                return stage
            return self._run_command(client, ["git", "commit", "-m", message], cwd=cwd)

        if action == "git_push":
            cwd = self._require_str(params, "working_dir")
            remote = str(params.get("remote", "origin"))
            branch = str(params.get("branch", "main"))
            return self._run_command(client, ["git", "push", "-u", remote, branch], cwd=cwd)

        if action == "gh_create_repo":
            cwd = self._require_str(params, "working_dir")
            repo_name = self._require_str(params, "repo_name")
            description = str(params.get("description") or "")
            private = params.get("private", False) is True
            if not re.match(r"^[a-zA-Z0-9._-]+$", repo_name):
                return {"returncode": 1, "stdout": "", "stderr": "Invalid repo name characters."}
            if self.remote_os == "windows":
                exists = self._run_command(client, ["where", "gh"], cwd=None)
                if exists.get("returncode", 1) != 0:
                    return {
                        "returncode": 127,
                        "stdout": "",
                        "stderr": "GitHub CLI (gh) is not installed on the worker laptop.",
                    }
            visibility = "--private" if private else "--public"
            args = ["gh", "repo", "create", repo_name, visibility, "--source=.", "--push"]
            if description:
                args.extend(["--description", description])
            return self._run_command(client, args, cwd=cwd, timeout=120)

        if action == "open_in_vscode":
            path = self._require_str(params, "path")
            return self._run_command(client, ["code", path], cwd=None)

        if action == "check_coding_agents":
            if self.remote_os == "windows":
                lines = []
                for name, binary in self._coding_bins.items():
                    resolved_bin, available = self._resolve_windows_binary(client, binary)
                    if available:
                        lines.append(f"{name}: available ({resolved_bin})")
                    else:
                        lines.append(f"{name}: unavailable (expected binary: {binary})")
                out = "\n".join(lines)
                err = ""
                rc = 0
                return {"returncode": int(rc), "stdout": out, "stderr": err}
            # Linux fallback
            lines = []
            for name, binary in self._coding_bins.items():
                r = self._run_command(client, ["bash", "-lc", f"command -v {binary} || true"], cwd=None)
                if r["stdout"].strip():
                    lines.append(f"{name}: available ({r['stdout'].strip()})")
                else:
                    lines.append(f"{name}: unavailable (expected binary: {binary})")
            return {"returncode": 0, "stdout": "\n".join(lines), "stderr": ""}

        if action == "configure_coding_agent":
            agent = self._require_str(params, "agent").strip().lower()
            provider = self._require_str(params, "provider").strip().lower()
            model = str(params.get("model") or "").strip()
            base_url = str(params.get("base_url") or "").strip()

            if agent != "cline":
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Only 'cline' is supported for provider switching right now.",
                }

            api_key = str(params.get("api_key") or "").strip()
            if not api_key:
                api_key = self._default_api_key_for_provider(provider)
            if not api_key:
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": (
                        f"No API key available for provider '{provider}'. "
                        "Pass api_key explicitly or configure matching environment key."
                    ),
                }

            if not model:
                model = self._default_model_for_provider(provider)

            binary = self._coding_bins.get("cline", "cline")
            if self.remote_os == "windows":
                binary, available = self._resolve_windows_binary(client, binary)
                if not available:
                    return {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "Cline CLI is not installed or not on PATH.",
                    }

            configured = self._configure_cline_provider(
                client=client,
                binary=binary,
                provider=provider,
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
            if configured["returncode"] != 0:
                return configured

            verify = self._run_command(client, [binary, "--version"], cwd=None, timeout=30)
            if verify["returncode"] == 0:
                configured["stdout"] = (
                    f"Cline configured to provider '{provider}'"
                    + (f" (model: {model})" if model else "")
                )
            else:
                configured["stdout"] = (
                    f"Cline auth command succeeded for provider '{provider}'"
                    + (f" (model: {model})" if model else "")
                )
            configured["stderr"] = ""
            return configured

        if action == "run_coding_agent":
            agent = self._require_str(params, "agent").strip().lower()
            prompt = self._require_str(params, "prompt")
            cwd = params.get("working_dir")
            timeout = params.get("timeout_seconds", 1800)
            if agent not in self._coding_bins:
                allowed = ", ".join(sorted(self._coding_bins.keys()))
                return {"returncode": 1, "stdout": "", "stderr": f"Unknown coding agent '{agent}'. Allowed: {allowed}"}
            if cwd is not None and not isinstance(cwd, str):
                return {"returncode": 1, "stdout": "", "stderr": "working_dir must be a string path."}
            if not isinstance(timeout, int) or timeout < 30 or timeout > 3600:
                return {"returncode": 1, "stdout": "", "stderr": "timeout_seconds must be an integer between 30 and 3600."}
            binary = self._coding_bins[agent]
            if self.remote_os == "windows":
                binary, available = self._resolve_windows_binary(client, binary)
                if not available:
                    return {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": (
                            f"'{agent}' CLI is not installed or not on PATH. "
                            f"Expected binary: {self._coding_bins[agent]}"
                        ),
                    }
            args = [binary, *self._coding_prefix[agent], prompt]
            initial = self._run_command(client, args, cwd=cwd, timeout=timeout)
            if agent != "cline":
                return initial
            if initial["returncode"] == 0:
                return initial
            if not self._cline_auto_switch:
                return initial
            if not self._is_retryable_cline_failure(initial):
                return initial
            return self._run_cline_with_auto_switch(
                client=client,
                binary=binary,
                prompt=prompt,
                cwd=cwd if isinstance(cwd, str) else None,
                timeout=timeout,
                initial_result=initial,
            )

        if action == "docker_build":
            cwd = self._require_str(params, "working_dir")
            tag = str(params.get("tag", "chathan-build:latest"))
            if not re.match(r"^[a-zA-Z0-9._/:@-]+$", tag):
                return {"returncode": 1, "stdout": "", "stderr": "Invalid Docker tag characters."}
            return self._run_command(client, ["docker", "build", "-t", tag, "."], cwd=cwd, timeout=600)

        if action == "docker_compose_up":
            cwd = self._require_str(params, "working_dir")
            return self._run_command(client, ["docker", "compose", "up", "-d"], cwd=cwd, timeout=300)

        if action == "close_app":
            app_name = self._require_str(params, "app").lower()
            exe = self._closeable_apps.get(app_name)
            if not exe:
                allowed = ", ".join(sorted(self._closeable_apps.keys()))
                return {"returncode": 1, "stdout": "", "stderr": f"'{app_name}' is not in the allowed list. Allowed: {allowed}"}
            if self.remote_os == "windows":
                return self._run_command(client, ["taskkill", "/F", "/IM", exe], cwd=None)
            return {"returncode": 1, "stdout": "", "stderr": "close_app currently supports Windows remote hosts only."}

        return {"returncode": 1, "stdout": "", "stderr": f"Action '{action}' is not supported in SSH tunnel mode."}

    @staticmethod
    def _default_api_key_for_provider(provider: str) -> str:
        keys = {
            "gemini": bot_cfg.GOOGLE_AI_API_KEY,
            "deepseek": bot_cfg.DEEPSEEK_API_KEY,
            "groq": bot_cfg.GROQ_API_KEY,
            "openrouter": bot_cfg.OPENROUTER_API_KEY,
            "openai": bot_cfg.OPENAI_API_KEY,
            "anthropic": bot_cfg.ANTHROPIC_API_KEY,
        }
        return str(keys.get(provider, "") or "").strip()

    @staticmethod
    def _default_model_for_provider(provider: str) -> str:
        defaults = {
            "gemini": os.environ.get("OPENCLAW_CLINE_GEMINI_MODEL", bot_cfg.GEMINI_MODEL or "gemini-2.0-flash"),
            "deepseek": os.environ.get("OPENCLAW_CLINE_DEEPSEEK_MODEL", "deepseek-chat"),
            "groq": os.environ.get("OPENCLAW_CLINE_GROQ_MODEL", "llama-3.3-70b-versatile"),
            "openrouter": os.environ.get(
                "OPENCLAW_CLINE_OPENROUTER_MODEL",
                bot_cfg.OPENROUTER_MODEL or "qwen/qwen3-next-80b-a3b-instruct:free",
            ),
            "openai": os.environ.get("OPENCLAW_CLINE_OPENAI_MODEL", "gpt-4o-mini"),
            "anthropic": os.environ.get("OPENCLAW_CLINE_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        }
        return str(defaults.get(provider, "") or "").strip()

    def _configure_cline_provider(
        self,
        *,
        client: paramiko.SSHClient,
        binary: str,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
    ) -> dict[str, Any]:
        args = [binary, "auth", "-p", provider, "-k", api_key]
        if model:
            args.extend(["-m", model])
        if base_url:
            args.extend(["-b", base_url])
        return self._run_command(client, args, cwd=None, timeout=120)

    @staticmethod
    def _is_retryable_cline_failure(result: dict[str, Any]) -> bool:
        text = (
            f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
        ).lower()
        markers = (
            "not authenticated",
            "api request failed",
            "provider returned error",
            "rate limit",
            "rate_limit_exceeded",
            "resource_exhausted",
            "quota",
            "quota exceeded",
            "insufficient_quota",
            "status\":402",
            "status\":429",
            "status':402",
            "status':429",
            "spend limit",
            "billing",
            "too many requests",
        )
        return any(marker in text for marker in markers)

    def _run_cline_with_auto_switch(
        self,
        *,
        client: paramiko.SSHClient,
        binary: str,
        prompt: str,
        cwd: str | None,
        timeout: int,
        initial_result: dict[str, Any],
    ) -> dict[str, Any]:
        attempted: list[str] = []
        last = dict(initial_result)
        run_args = [binary, *self._coding_prefix["cline"], prompt]

        for provider in self._cline_provider_priority:
            api_key = self._default_api_key_for_provider(provider)
            if not api_key:
                continue
            model = self._default_model_for_provider(provider)
            base_url = self._cline_provider_base_urls.get(provider, "")
            attempted.append(provider)

            configured = self._configure_cline_provider(
                client=client,
                binary=binary,
                provider=provider,
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
            if configured.get("returncode", 1) != 0:
                last = configured
                continue

            run = self._run_command(client, run_args, cwd=cwd, timeout=timeout)
            notice = (
                f"Notice: auto-switched Cline provider to {provider}"
                + (f" (model: {model})." if model else ".")
            )
            run["stdout"] = (f"{notice}\n{run.get('stdout', '').strip()}").strip()
            if run.get("returncode", 1) == 0:
                run["stderr"] = (run.get("stderr") or "").strip()
                return run
            last = run
            if not self._is_retryable_cline_failure(run):
                return run

        if attempted:
            suffix = f"Auto-switch attempted providers: {', '.join(attempted)}."
            err = (last.get("stderr") or "").strip()
            out = (last.get("stdout") or "").strip()
            if err:
                last["stderr"] = f"{err}\n{suffix}"
            elif out:
                last["stderr"] = suffix
            else:
                last["stderr"] = f"Cline failed and {suffix}"
        return last

    def _resolve_windows_binary(self, client: paramiko.SSHClient, binary: str) -> tuple[str, bool]:
        b = (binary or "").strip()
        if not b:
            return binary, False

        # Explicit path already provided.
        if any(ch in b for ch in ("\\", "/", ":")):
            return b, self._remote_path_exists(client, b)

        # PATH lookup first.
        where_result = self._run_command(client, ["where", b], cwd=None)
        if where_result.get("returncode", 1) == 0:
            lines = [ln.strip() for ln in (where_result.get("stdout") or "").splitlines() if ln.strip()]
            if lines:
                return lines[0], True

        # Fallback: npm global bin for current user.
        npm_bin = rf"C:\Users\{self.username}\AppData\Roaming\npm"
        candidates = [
            rf"{npm_bin}\{b}.cmd",
            rf"{npm_bin}\{b}.exe",
            rf"{npm_bin}\{b}",
        ]
        for cand in candidates:
            if self._remote_path_exists(client, cand):
                return cand, True

        return b, False

    @staticmethod
    def _remote_path_exists(client: paramiko.SSHClient, path: str) -> bool:
        sftp = client.open_sftp()
        try:
            sftp.stat(path)
            return True
        except OSError:
            return False
        finally:
            sftp.close()

    def _norm_join(self, parent: str | None, child: str) -> str:
        if not parent:
            return child
        if self.remote_os == "windows":
            return str(PureWindowsPath(parent) / child)
        return str(PurePosixPath(parent) / child)

    def _file_read(self, client: paramiko.SSHClient, params: dict[str, Any]) -> dict[str, Any]:
        filepath = self._require_str(params, "file")
        sftp = client.open_sftp()
        try:
            with sftp.open(filepath, "r") as fh:
                content = fh.read().decode("utf-8", errors="replace")
            if len(content) > 65536:
                content = content[:65536] + "\n... (truncated at 64 KB)"
            return {"returncode": 0, "stdout": content, "stderr": ""}
        except OSError as exc:
            if self.remote_os == "windows":
                ps = (
                    f"$p={_ps_quote(filepath)}; "
                    "$c=Get-Content -LiteralPath $p -Raw -Encoding UTF8; "
                    "if ($c.Length -gt 65536) { $c.Substring(0,65536) + \"`n... (truncated at 64 KB)\" } else { $c }"
                )
                return self._run_command(client, ["powershell", "-NoProfile", "-Command", ps], cwd=None)
            return {"returncode": 1, "stdout": "", "stderr": str(exc)}
        finally:
            sftp.close()

    def _file_write(self, client: paramiko.SSHClient, params: dict[str, Any]) -> dict[str, Any]:
        filepath = self._require_str(params, "file")
        content = params.get("content", "")
        if not isinstance(content, str):
            return {"returncode": 1, "stdout": "", "stderr": "content must be a string."}
        if len(content.encode("utf-8")) > 1_048_576:
            return {"returncode": 1, "stdout": "", "stderr": "Content exceeds 1 MB limit."}

        sftp = client.open_sftp()
        try:
            parent = str(PureWindowsPath(filepath).parent) if self.remote_os == "windows" else str(PurePosixPath(filepath).parent)
            self._sftp_makedirs(sftp, parent)
            with sftp.open(filepath, "w") as fh:
                fh.write(content)
            return {"returncode": 0, "stdout": f"Wrote {len(content)} bytes to {filepath}.", "stderr": ""}
        except OSError as exc:
            if self.remote_os == "windows":
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                ps = (
                    f"$p={_ps_quote(filepath)}; "
                    "$d=Split-Path -Parent $p; if ($d) { New-Item -ItemType Directory -Path $d -Force | Out-Null }; "
                    f"$bytes=[System.Convert]::FromBase64String('{encoded}'); "
                    "[System.IO.File]::WriteAllBytes($p,$bytes);"
                )
                return self._run_command(client, ["powershell", "-NoProfile", "-Command", ps], cwd=None)
            return {"returncode": 1, "stdout": "", "stderr": str(exc)}
        finally:
            sftp.close()

    def _create_directory(self, client: paramiko.SSHClient, params: dict[str, Any]) -> dict[str, Any]:
        directory = self._require_str(params, "directory")
        sftp = client.open_sftp()
        try:
            self._sftp_makedirs(sftp, directory)
            return {"returncode": 0, "stdout": f"Created {directory}", "stderr": ""}
        except OSError as exc:
            if self.remote_os == "windows":
                ps = f"$d={_ps_quote(directory)}; New-Item -ItemType Directory -Path $d -Force | Out-Null; Write-Output \"Created $d\""
                return self._run_command(client, ["powershell", "-NoProfile", "-Command", ps], cwd=None)
            return {"returncode": 1, "stdout": "", "stderr": str(exc)}
        finally:
            sftp.close()

    def _sftp_makedirs(self, sftp: paramiko.SFTPClient, path: str) -> None:
        if not path:
            return
        if self.remote_os == "windows":
            parts = list(PureWindowsPath(path).parts)
            if len(parts) == 1 and parts[0].endswith("\\"):
                return
            current = parts[0]
            for p in parts[1:]:
                current = str(PureWindowsPath(current) / p)
                try:
                    sftp.stat(current)
                except OSError:
                    sftp.mkdir(current)
            return

        parts = list(PurePosixPath(path).parts)
        current = ""
        for p in parts:
            current = str(PurePosixPath(current) / p)
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)

    def _list_directory(self, client: paramiko.SSHClient, params: dict[str, Any]) -> dict[str, Any]:
        directory = self._require_str(params, "directory")
        recursive = params.get("recursive", False) is True
        sftp = client.open_sftp()
        try:
            lines: list[str] = []
            self._walk_sftp(sftp, directory, recursive, 0, lines, {"count": 0})
            return {"returncode": 0, "stdout": "\n".join(lines), "stderr": ""}
        except OSError as exc:
            if self.remote_os == "windows":
                if recursive:
                    ps = (
                        f"$d={_ps_quote(directory)}; "
                        "Get-ChildItem -LiteralPath $d -Recurse -Force | "
                        "Select-Object FullName,Length,PSIsContainer | "
                        "ForEach-Object { if ($_.PSIsContainer) { \"[DIR] $($_.FullName)\" } else { \"$($_.FullName)  ($($_.Length) bytes)\" } }"
                    )
                else:
                    ps = (
                        f"$d={_ps_quote(directory)}; "
                        "Get-ChildItem -LiteralPath $d -Force | "
                        "ForEach-Object { if ($_.PSIsContainer) { \"[DIR] $($_.Name)/\" } else { \"$($_.Name)  ($($_.Length) bytes)\" } }"
                    )
                return self._run_command(client, ["powershell", "-NoProfile", "-Command", ps], cwd=None)
            return {"returncode": 1, "stdout": "", "stderr": str(exc)}
        finally:
            sftp.close()

    def _walk_sftp(
        self,
        sftp: paramiko.SFTPClient,
        directory: str,
        recursive: bool,
        depth: int,
        out: list[str],
        state: dict[str, int],
    ) -> None:
        max_depth = 3
        max_entries = 500

        entries = sorted(sftp.listdir_attr(directory), key=lambda e: e.filename.lower())
        for e in entries:
            if state["count"] >= max_entries:
                out.append("... (truncated)")
                return
            name = e.filename
            path = self._norm_join(directory, name)
            prefix = "  " * depth
            if stat.S_ISDIR(e.st_mode):
                out.append(f"{prefix}[DIR] {name}/")
                if recursive and depth < max_depth:
                    self._walk_sftp(sftp, path, True, depth + 1, out, state)
            else:
                out.append(f"{prefix}{name}  ({int(e.st_size)} bytes)")
            state["count"] += 1


_SSH_EXECUTOR: SSHTunnelExecutor | None = None


def get_ssh_executor() -> SSHTunnelExecutor:
    global _SSH_EXECUTOR
    if _SSH_EXECUTOR is None:
        _SSH_EXECUTOR = SSHTunnelExecutor()
    return _SSH_EXECUTOR
