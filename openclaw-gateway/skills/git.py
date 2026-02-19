"""SKYNET — Git Skill (git_init, git_status, git_add_all, git_commit, git_push, gh_create_repo)."""

from __future__ import annotations
from typing import Any
import bot_config as cfg
from .base import BaseSkill, SkillContext


class GitSkill(BaseSkill):
    name = "git"
    description = "Git version control and GitHub operations"
    allowed_roles = []  # All agents can use git
    requires_approval = {"git_push", "gh_create_repo"}
    plan_auto_approved = {"git_init", "git_status", "git_add_all", "git_commit"}

    def get_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "git_init",
                "description": "Initialize a new git repository with 'main' as default branch.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Directory for the repo"},
                    },
                    "required": ["working_dir"],
                },
            },
            {
                "name": "git_status",
                "description": "Show git working tree status (porcelain format).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Git repo directory"},
                    },
                    "required": ["working_dir"],
                },
            },
            {
                "name": "git_add_all",
                "description": "Stage all changes including untracked files (git add -A).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Git repo directory"},
                    },
                    "required": ["working_dir"],
                },
            },
            {
                "name": "git_commit",
                "description": "Commit staged changes with the given message.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Git repo directory"},
                        "message": {"type": "string", "description": "Commit message"},
                    },
                    "required": ["working_dir", "message"],
                },
            },
            {
                "name": "git_push",
                "description": "Push commits to the remote repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Git repo directory"},
                        "remote": {"type": "string", "description": "Remote name (default: origin)"},
                        "branch": {"type": "string", "description": "Branch name (default: main)"},
                    },
                    "required": ["working_dir"],
                },
            },
            {
                "name": "gh_create_repo",
                "description": "Create a new GitHub repository and push the initial code.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "working_dir": {"type": "string", "description": "Local git repo directory"},
                        "repo_name": {"type": "string", "description": "Repository name on GitHub"},
                        "description": {"type": "string", "description": "Repo description"},
                        "private": {"type": "boolean", "description": "Private repo (default: false)"},
                    },
                    "required": ["working_dir", "repo_name"],
                },
            },
        ]

    async def execute(self, tool_name: str, tool_input: dict[str, Any], context: SkillContext) -> str:
        needs_manual_approval = (
            tool_name in self.requires_approval and not cfg.AUTO_APPROVE_GIT_ACTIONS
        )
        if needs_manual_approval:
            if not context.request_approval:
                return f"Action '{tool_name}' requires approval, but approval channel is unavailable."
            approved = await context.request_approval(
                context.project_id, tool_name, tool_input,
            )
            if not approved:
                return f"Action '{tool_name}' was denied by the user."

        confirmed = (
            tool_name in self.plan_auto_approved
            or tool_name in self.requires_approval
        )
        return await context.send_to_agent(tool_name, tool_input, confirmed=confirmed)
