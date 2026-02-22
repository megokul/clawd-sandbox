"""
SKYNET — Project Management Skill

Exposes project lifecycle tools to the LLM so it can create projects,
capture ideas, generate plans, write docs, and manage project state
through natural conversation instead of regex-driven menus.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import BaseSkill, SkillContext

logger = logging.getLogger("skynet.skills.project")


def _pm():
    """Lazy import to avoid circular imports at module load time."""
    from bot import state as _state
    return _state._project_manager


def _state():
    from bot import state as _s
    return _s


class ProjectManagementSkill(BaseSkill):
    name = "project_management"
    description = "Create and manage SKYNET projects, capture ideas, generate plans and docs"
    plan_auto_approved = {
        "project_add_idea",
        "project_list",
        "project_status",
    }
    requires_approval = {
        "project_remove",
    }

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "project_create",
                "description": (
                    "Create a new SKYNET project with the given name. "
                    "Call this when the user wants to start a new project. "
                    "Returns the created project details including local path."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Project name (short, filesystem-safe)",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "project_add_idea",
                "description": (
                    "Add an idea or requirement to the active project. "
                    "Call this whenever the user describes features, requirements, "
                    "constraints, or anything they want the project to do. "
                    "If project_id is omitted, adds to the last active project."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "idea": {
                            "type": "string",
                            "description": "The idea, requirement, or feature description",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                    "required": ["idea"],
                },
            },
            {
                "name": "project_list",
                "description": "List all projects with their status and idea counts.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "project_status",
                "description": (
                    "Get the current status of the active project: status, idea count, "
                    "local path, and recent ideas."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_generate_plan",
                "description": (
                    "Generate the task plan for the active project. "
                    "Call this when the user asks to generate the plan, "
                    "start building, or when enough ideas/requirements are captured."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_approve_start",
                "description": (
                    "Approve the plan and start execution for the active project. "
                    "Call this when the user approves the plan and wants coding to begin."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_pause",
                "description": "Pause the active project's execution.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_resume",
                "description": "Resume a paused project.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_cancel",
                "description": "Cancel the active project.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_remove",
                "description": (
                    "Permanently remove a project record (workspace files are kept). "
                    "Requires explicit user confirmation — only call when the user "
                    "clearly asks to delete or remove a project."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID to remove (optional, defaults to last active)",
                        },
                    },
                },
            },
            {
                "name": "project_generate_docs",
                "description": (
                    "Write project documentation (PRD, architecture overview, feature list, "
                    "runbooks, ADR, task plan) based on what you know about the project. "
                    "Fill in the fields you have gathered from the conversation. "
                    "Call this when the user asks to generate docs, write the PRD, "
                    "or when you have enough context (problem + requirements + tech stack)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "problem": {
                            "type": "string",
                            "description": "The problem this project solves",
                        },
                        "users": {
                            "type": "string",
                            "description": "Who the primary users are",
                        },
                        "requirements": {
                            "type": "string",
                            "description": "Key requirements and features (can be comma-separated or bullet list)",
                        },
                        "tech_stack": {
                            "type": "string",
                            "description": "Preferred tech stack, language, or framework",
                        },
                        "non_goals": {
                            "type": "string",
                            "description": "What is explicitly out of scope",
                        },
                        "success_metrics": {
                            "type": "string",
                            "description": "How success will be measured",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to last active)",
                        },
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: SkillContext,
    ) -> str:
        pm = _pm()
        st = _state()

        if pm is None:
            return "ERROR: Project manager is not available."

        try:
            if tool_name == "project_create":
                return await self._create(pm, st, tool_input)
            if tool_name == "project_add_idea":
                return await self._add_idea(pm, st, tool_input)
            if tool_name == "project_list":
                return await self._list(pm)
            if tool_name == "project_status":
                return await self._status(pm, st, tool_input)
            if tool_name == "project_generate_plan":
                return await self._generate_plan(pm, st, tool_input)
            if tool_name == "project_approve_start":
                return await self._approve_start(pm, st, tool_input)
            if tool_name == "project_pause":
                return await self._pause(pm, st, tool_input)
            if tool_name == "project_resume":
                return await self._resume(pm, st, tool_input)
            if tool_name == "project_cancel":
                return await self._cancel(pm, st, tool_input)
            if tool_name == "project_remove":
                return await self._remove(pm, st, tool_input)
            if tool_name == "project_generate_docs":
                return await self._generate_docs(pm, st, tool_input)
            return f"Unknown tool: {tool_name}"
        except Exception as exc:
            logger.exception("ProjectManagementSkill.%s failed", tool_name)
            return f"ERROR: {exc}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_project_id(self, st, tool_input: dict) -> str | None:
        return tool_input.get("project_id") or st._last_project_id

    async def _get_project(self, pm, project_id: str) -> dict | None:
        try:
            from db import store
            return await store.get_project(pm.db, project_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _create(self, pm, st, inp: dict) -> str:
        name = (inp.get("name") or "").strip()
        if not name:
            return "ERROR: Project name is required."
        project = await pm.create_project(name)
        st._last_project_id = project["id"]
        from bot.helpers import _project_bootstrap_note, _project_display
        path = project.get("local_path", "")
        bootstrap_note = _project_bootstrap_note(project)
        parts = [f"Created project '{_project_display(project)}' at {path}."]
        if bootstrap_note:
            parts.append(bootstrap_note)
        return "\n".join(parts)

    async def _add_idea(self, pm, st, inp: dict) -> str:
        idea = (inp.get("idea") or "").strip()
        if not idea:
            return "ERROR: idea text is required."
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project. Create one first."
        count = await pm.add_idea(project_id, idea)
        st._last_project_id = project_id
        project = await self._get_project(pm, project_id)
        name = project.get("name", project_id) if project else project_id
        return f"Added idea #{count} to '{name}'."

    async def _list(self, pm) -> str:
        projects = await pm.list_projects()
        if not projects:
            return "No projects found."
        lines = ["Projects:"]
        for p in projects:
            status = p.get("status", "?")
            name = p.get("name") or p.get("id", "?")
            pid = p.get("id", "")
            ideas = p.get("idea_count", "?")
            lines.append(f"  • {name} [{status}] — {ideas} ideas (id: {pid})")
        return "\n".join(lines)

    async def _status(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "No active project."
        project = await self._get_project(pm, project_id)
        if not project:
            return f"Project '{project_id}' not found."
        st._last_project_id = project_id
        name = project.get("name", project_id)
        status = project.get("status", "?")
        path = project.get("local_path", "?")
        ideas = project.get("ideas") or []
        idea_count = len(ideas) if isinstance(ideas, list) else "?"
        recent = []
        if isinstance(ideas, list):
            recent = ideas[-3:]
        lines = [
            f"Project: {name}",
            f"Status: {status}",
            f"Path: {path}",
            f"Ideas: {idea_count}",
        ]
        if recent:
            lines.append("Recent ideas:")
            for idea in recent:
                text = (idea.get("text") or str(idea))[:120]
                lines.append(f"  - {text}")
        return "\n".join(lines)

    async def _generate_plan(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        await pm.generate_plan(project_id)
        st._last_project_id = project_id
        return f"Plan generation started for project '{project_id}'. I will notify you when it's ready for review."

    async def _approve_start(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        await pm.approve_plan(project_id)
        await pm.start_execution(project_id)
        st._last_project_id = project_id
        return f"Plan approved and execution started for project '{project_id}'."

    async def _pause(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        await pm.pause_project(project_id)
        return f"Project '{project_id}' paused."

    async def _resume(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        await pm.resume_project(project_id)
        st._last_project_id = project_id
        return f"Project '{project_id}' resumed."

    async def _cancel(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        await pm.cancel_project(project_id)
        return f"Project '{project_id}' cancelled."

    async def _remove(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project to remove."
        project = await self._get_project(pm, project_id)
        if not project:
            return f"Project '{project_id}' not found."
        from bot.helpers import _project_display, _send_remove_project_confirmation
        await _send_remove_project_confirmation(project)
        return f"Confirmation sent for removing '{_project_display(project)}'. Waiting for your approval."

    async def _generate_docs(self, pm, st, inp: dict) -> str:
        project_id = self._resolve_project_id(st, inp)
        if not project_id:
            return "ERROR: No active project."
        project = await self._get_project(pm, project_id)
        if not project:
            return f"Project '{project_id}' not found."

        answers = {
            k: str(v).strip()
            for k, v in inp.items()
            if k in ("problem", "users", "requirements", "tech_stack", "non_goals", "success_metrics")
            and v
        }

        from bot.doc_intake import _run_project_docs_generation_async
        from bot.helpers import _spawn_background_task

        _spawn_background_task(
            _run_project_docs_generation_async(project, answers, reason="doc_request"),
            tag=f"doc-gen-{project_id}",
        )
        return (
            f"Documentation generation started for '{project.get('name', project_id)}'. "
            "I'll send a notification when the docs are written."
        )
