"""
SKYNET — Skill Base Classes

Every skill inherits from BaseSkill and provides tool definitions
plus execution logic. Skills are modular capabilities that agents
load based on their role.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

import aiohttp


class SkillContext:
    """Runtime context passed to skill execution — provides gateway services."""

    def __init__(
        self,
        project_id: str,
        project_path: str,
        gateway_api_url: str,
        searcher: Any = None,
        request_approval: Callable[..., Awaitable[bool]] | None = None,
    ):
        self.project_id = project_id
        self.project_path = project_path
        self.gateway_api_url = gateway_api_url
        self.searcher = searcher
        self.request_approval = request_approval

    async def send_to_agent(
        self,
        action: str,
        params: dict[str, Any],
        confirmed: bool = True,
        include_exit_code: bool = True,
    ) -> str:
        """Send an action to the laptop agent via the gateway HTTP API."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.gateway_api_url}/action",
                    json={"action": action, "params": params, "confirmed": confirmed},
                    timeout=aiohttp.ClientTimeout(total=130),
                ) as resp:
                    result = await resp.json()
        except Exception as exc:
            return f"ERROR: Failed to reach agent: {exc}"

        if result.get("status") == "error":
            return f"ERROR: {result.get('error', 'Unknown error')}"

        inner = result.get("result", {})
        rc = inner.get("returncode", "?")
        stdout = (inner.get("stdout") or "").strip()
        stderr = (inner.get("stderr") or "").strip()

        if include_exit_code:
            parts = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"STDERR: {stderr}")
            parts.append(f"[exit code: {rc}]")
            return "\n".join(parts) if parts else "OK"

        if str(rc) in {"0", "0.0"}:
            return stdout or "OK"
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stderr or stdout or "Action failed."


class BaseSkill(ABC):
    """Abstract base for all SKYNET skills."""

    name: str = "base"
    description: str = ""
    version: str = "1.0.0"

    # Which agent roles can use this skill. Empty = all roles.
    allowed_roles: list[str] = []

    # Actions that always need individual Telegram approval.
    requires_approval: set[str] = set()

    # Actions auto-approved when a project plan is approved.
    plan_auto_approved: set[str] = set()

    @abstractmethod
    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic tool schema format."""
        ...

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: SkillContext,
    ) -> str:
        """Execute a tool call. Returns result string for the AI."""
        ...

    def get_tool_names(self) -> set[str]:
        """Return all tool names this skill provides."""
        return {t["name"] for t in self.get_tools()}
