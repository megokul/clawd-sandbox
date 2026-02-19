"""SKYNET — IDE Skill (open_in_vscode)."""

from __future__ import annotations
from typing import Any
from .base import BaseSkill, SkillContext


class IDESkill(BaseSkill):
    name = "ide"
    description = "IDE integration (VS Code + local coding agent CLIs)"
    allowed_roles = ["frontend", "backend", "devops"]
    plan_auto_approved = {"open_in_vscode", "check_coding_agents", "run_coding_agent"}

    def get_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "open_in_vscode",
                "description": "Open a project directory or file in VS Code on the laptop.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to open in VS Code"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "check_coding_agents",
                "description": (
                    "Check if local coding agent CLIs are installed on the laptop. "
                    "Reports Codex, Claude, and Cline availability."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "run_coding_agent",
                "description": (
                    "Run a local coding agent CLI in non-interactive mode on the laptop "
                    "(Codex, Claude, or Cline)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "agent": {
                            "type": "string",
                            "enum": ["codex", "claude", "cline"],
                            "description": "Which coding agent to run.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Task prompt to send to the coding agent.",
                        },
                        "working_dir": {
                            "type": "string",
                            "description": "Optional project directory on the laptop.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Optional timeout (30-3600, default 1800).",
                        },
                    },
                    "required": ["agent", "prompt"],
                },
            },
        ]

    async def execute(self, tool_name: str, tool_input: dict[str, Any], context: SkillContext) -> str:
        return await context.send_to_agent(tool_name, tool_input)
