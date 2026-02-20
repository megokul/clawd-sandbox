"""
SKYNET — Project Manager

Manages the full lifecycle of a project:
  ideation → planning → approved → coding → testing → completed
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp
import aiosqlite

from agents.roles import AGENT_CONFIGS, ALL_ROLES
from agents.planner_agent import PlannerAgent
from ai.provider_router import ProviderRouter
from ai.prompts import PLANNING_PROMPT
from ai import context as ctx
import bot_config as cfg
from chathan.protocol import PlanSpec
from db import store
from search.web_search import WebSearcher
from .scheduler import Scheduler

logger = logging.getLogger("skynet.core.pm")

# ORACLE prompt for AI-based task-to-agent assignment.
_ORACLE_ASSIGNMENT_PROMPT = """You are SKYNET ORACLE — the AI task assignment engine.

Given a list of tasks and a tech stack, assign each task to the best agent role.

Available roles and their specialties:
{roles_info}

Respond with ONLY a JSON object:
{{"assignments": [{{"task_id": "<id>", "role": "<role>"}}]}}

Do not include any other text.
"""


def _slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _join_path(base: str, leaf: str) -> str:
    sep = "\\" if ("\\" in base or ":" in base) else "/"
    return base.rstrip("\\/") + sep + leaf


class ProjectManager:
    """High-level project lifecycle operations."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        router: ProviderRouter,
        searcher: WebSearcher,
        scheduler: Scheduler,
        project_base_dir: str,
    ):
        self.db = db
        self.router = router
        self.searcher = searcher
        self.scheduler = scheduler
        self.base_dir = project_base_dir
        self.planner_agent = PlannerAgent(
            router=self.router,
            run_agent_action=self._run_agent_action_for_planner,
        )

    async def create_project(self, name: str) -> dict[str, Any]:
        """Create a new project in 'ideation' status."""
        slug = _slugify(name)
        existing = await store.get_project_by_name(self.db, slug)
        if existing:
            raise ValueError(f"Project '{slug}' already exists.")

        local_path = _join_path(self.base_dir, slug)
        project = await store.create_project(
            self.db, name=slug, display_name=name, local_path=local_path,
        )
        bootstrap_summary, bootstrap_ok = await self._bootstrap_project_workspace(project)
        if not bootstrap_ok:
            # Keep creation atomic: do not retain a project row if required
            # workspace bootstrap steps failed.
            await self.db.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
            await self.db.commit()
            raise ValueError(
                f"Project bootstrap failed and creation was rolled back. {bootstrap_summary}"
            )
        await store.add_event(
            self.db, project["id"], "created",
            f"Project '{name}' created at {local_path}",
            detail=bootstrap_summary,
        )
        out = await store.get_project(self.db, project["id"])
        if out is None:
            raise ValueError("Project was created but could not be loaded.")
        out["bootstrap_summary"] = bootstrap_summary
        out["bootstrap_ok"] = bootstrap_ok
        return out

    async def add_idea(self, project_id: str, text: str) -> int:
        """Add an idea message to a project in ideation phase."""
        project = await store.get_project(self.db, project_id)
        if not project:
            raise ValueError("Project not found.")
        if project["status"] != "ideation":
            raise ValueError(f"Project is in '{project['status']}' status, not ideation.")

        idea_id = await store.add_idea(self.db, project_id, text)
        ideas = await store.get_ideas(self.db, project_id)
        return len(ideas)

    async def generate_plan(self, project_id: str) -> dict[str, Any]:
        """Use AI to synthesise ideas into a structured project plan."""
        project = await store.get_project(self.db, project_id)
        if not project:
            raise ValueError("Project not found.")

        ideas = await store.get_ideas(self.db, project_id)
        if not ideas:
            raise ValueError("No ideas to plan from. Send some ideas first.")

        # Build the prompt from all ideas.
        idea_text = "\n".join(
            f"- {idea['message_text']}" for idea in ideas
        )

        system_prompt = PLANNING_PROMPT.format(project_path=project["local_path"])

        messages = [{
            "role": "user",
            "content": (
                f"Create a detailed implementation plan for this project idea:\n\n"
                f"{idea_text}\n\n"
                f"The project name is: {project['display_name']}\n"
                f"Output ONLY the JSON plan, no other text."
            ),
        }]

        # Let the AI use planning tools (web search, file read).
        final_text, updated_messages = await self.planner_agent.run_planning_conversation(
            messages=messages,
            system_prompt=system_prompt,
        )

        # Parse the JSON plan from the response.
        plan_data = self.planner_agent.parse_plan_json(final_text)
        if not plan_data:
            raise ValueError(f"AI did not return valid plan JSON. Response: {final_text[:200]}")

        # Store the plan.
        milestones = plan_data.get("milestones", [])
        plan_id = await store.create_plan(
            self.db,
            project_id=project_id,
            summary=plan_data.get("summary", ""),
            timeline=milestones,
            milestones=milestones,
        )

        # Create tasks from milestones.
        all_tasks = []
        for milestone in milestones:
            for task in milestone.get("tasks", []):
                all_tasks.append({
                    "milestone": milestone.get("name", ""),
                    "title": task.get("title", "Untitled task"),
                    "description": task.get("description", ""),
                })
        await store.create_tasks(self.db, project_id, plan_id, all_tasks)

        # Build a formal PlanSpec from the AI output.
        plan_spec = PlanSpec.from_ai_plan(project_id, plan_id, plan_data)
        logger.info(
            "PlanSpec created: %d steps, risk=%s, agents=%s, ~%d min",
            len(plan_spec.steps),
            plan_spec.max_risk_level,
            plan_spec.agent_roles_needed,
            plan_spec.total_estimated_minutes,
        )

        # ORACLE: AI-based task-to-agent assignment.
        tech_stack = plan_data.get("tech_stack", {})
        await self._assign_agents_to_tasks(project_id, plan_id, tech_stack)

        # Update project with tech stack and description.
        await store.update_project(
            self.db, project_id,
            status="planning",
            description=plan_data.get("summary", ""),
            tech_stack=json.dumps(tech_stack),
        )

        plan = await store.get_active_plan(self.db, project_id)
        # Attach the PlanSpec dict for downstream consumers.
        plan["plan_spec"] = plan_spec.to_dict()
        return plan

    async def approve_plan(self, project_id: str) -> None:
        """Approve the plan and mark project ready for execution."""
        project = await store.get_project(self.db, project_id)
        if not project:
            raise ValueError("Project not found.")
        if project["status"] not in ("planning", "ideation"):
            raise ValueError(f"Cannot approve: project is in '{project['status']}' status.")

        await store.update_project(
            self.db, project_id,
            status="approved",
            approved_at=store._now(),
        )
        await store.add_event(self.db, project_id, "plan_approved", "Plan approved by user")

    async def start_execution(self, project_id: str) -> None:
        """Submit the project to the scheduler for autonomous coding."""
        project = await store.get_project(self.db, project_id)
        if not project:
            raise ValueError("Project not found.")
        if project["status"] != "approved":
            raise ValueError(f"Cannot start: project is in '{project['status']}' status.")

        await self.scheduler.submit(project_id)

    async def pause_project(self, project_id: str) -> None:
        if not self.scheduler.pause(project_id):
            raise ValueError("Project is not currently running.")
        await store.update_project(self.db, project_id, status="paused")

    async def resume_project(self, project_id: str) -> None:
        if not self.scheduler.resume(project_id):
            raise ValueError("Project is not currently paused.")
        await store.update_project(self.db, project_id, status="coding")

    async def cancel_project(self, project_id: str) -> None:
        self.scheduler.cancel(project_id)
        await store.update_project(self.db, project_id, status="cancelled")
        await store.add_event(self.db, project_id, "cancelled", "Project cancelled by user")

    async def list_projects(self) -> list[dict[str, Any]]:
        return await store.list_projects(self.db)

    async def get_status(self, project_id: str) -> dict[str, Any]:
        project = await store.get_project(self.db, project_id)
        if not project:
            raise ValueError("Project not found.")

        tasks = await store.get_tasks(self.db, project_id)
        completed = sum(1 for t in tasks if t["status"] == "completed")
        total = len(tasks)
        in_progress = [t for t in tasks if t["status"] == "in_progress"]

        events = await store.get_events(self.db, project_id, limit=5)

        return {
            "project": project,
            "progress": f"{completed}/{total}",
            "percent": round(completed / total * 100) if total else 0,
            "current_task": in_progress[0]["title"] if in_progress else None,
            "recent_events": events,
            "is_running": self.scheduler.is_running(project_id),
        }

    async def get_ideation_project(self) -> dict[str, Any] | None:
        """Get the current project in ideation status (if any)."""
        projects = await store.get_projects_by_status(self.db, "ideation")
        return projects[0] if projects else None

    # ------------------------------------------------------------------
    # SKYNET ORACLE — AI task-to-agent assignment
    # ------------------------------------------------------------------

    async def _assign_agents_to_tasks(
        self,
        project_id: str,
        plan_id: str,
        tech_stack: dict[str, Any],
    ) -> None:
        """Use AI to classify each task → best agent role."""
        tasks = await store.get_tasks(self.db, project_id, plan_id)
        if not tasks:
            return

        # Build role descriptions for the ORACLE prompt.
        roles_info = "\n".join(
            f"- {role}: {cfg['description']}"
            for role, cfg in AGENT_CONFIGS.items()
        )

        task_list = [
            {"id": t["id"], "title": t["title"], "desc": t.get("description", "")}
            for t in tasks
        ]

        system = _ORACLE_ASSIGNMENT_PROMPT.format(roles_info=roles_info)
        messages = [{
            "role": "user",
            "content": (
                f"Tech stack: {json.dumps(tech_stack)}\n\n"
                f"Tasks:\n{json.dumps(task_list, indent=2)}\n\n"
                f"Assign each task to the best agent role."
            ),
        }]

        try:
            response = await self.router.chat(
                messages, system=system, max_tokens=1024,
                task_type="planning",
            )
            assignments = self._parse_plan_json(response.text)
            if not assignments or "assignments" not in assignments:
                logger.warning("ORACLE returned invalid assignment JSON.")
                return

            valid_roles = ALL_ROLES
            for entry in assignments["assignments"]:
                task_id = entry.get("task_id", "")
                role = entry.get("role", "backend")
                if role not in valid_roles:
                    role = "backend"
                await store.update_task(
                    self.db, task_id, assigned_agent_role=role,
                )

            logger.info(
                "ORACLE assigned %d tasks to agent roles for project %s",
                len(assignments["assignments"]), project_id,
            )

        except Exception as exc:
            logger.warning("ORACLE assignment failed: %s — using default roles", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _planning_loop(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> tuple[str, list[dict]]:
        """Backward-compatible wrapper for planner role."""
        return await self.planner_agent.run_planning_conversation(
            messages=messages,
            system_prompt=system_prompt,
        )

    def _parse_plan_json(self, text: str) -> dict[str, Any] | None:
        """Backward-compatible wrapper for planner parser."""
        return self.planner_agent.parse_plan_json(text)

    # ------------------------------------------------------------------
    # Project bootstrap
    # ------------------------------------------------------------------

    async def _bootstrap_project_workspace(self, project: dict[str, Any]) -> tuple[str, bool]:
        """
        Initialize project workspace and repository via the connected agent.

        Returns:
            (summary, ok)
            ok=False means required bootstrap steps failed and project creation
            should be rolled back.
        """
        if not cfg.AUTO_BOOTSTRAP_PROJECT:
            return "Bootstrap skipped (AUTO_BOOTSTRAP_PROJECT disabled).", True

        path = project["local_path"]
        slug = project["name"]
        readme_path = _join_path(path, "README.md")
        bootstrap_notes: list[str] = []

        ok, msg = await self._run_agent_action(
            "create_directory",
            {"directory": path},
            confirmed=True,
        )
        bootstrap_notes.append(
            "directory: ok" if ok else f"directory: failed ({msg})"
        )
        if not ok:
            return "; ".join(bootstrap_notes), False

        readme = (
            f"# {project['display_name']}\n\n"
            "Project initialized by SKYNET/OpenClaw.\n\n"
            "## Next Steps\n"
            "1. Refine goals and milestones in Telegram.\n"
            "2. Let autonomous agents implement milestone-by-milestone.\n"
            "3. Review milestone updates and final deliverables.\n"
        )
        ok, msg = await self._run_agent_action(
            "file_write",
            {"file": readme_path, "content": readme},
            confirmed=True,
        )
        bootstrap_notes.append("readme: ok" if ok else f"readme: failed ({msg})")
        if not ok:
            return "; ".join(bootstrap_notes), False

        ok, msg = await self._run_agent_action(
            "git_init",
            {"working_dir": path},
            confirmed=True,
        )
        bootstrap_notes.append("git_init: ok" if ok else f"git_init: failed ({msg})")
        if not ok:
            return "; ".join(bootstrap_notes), False

        ok_add, msg_add = await self._run_agent_action(
            "git_add_all",
            {"working_dir": path},
            confirmed=True,
        )
        ok_commit, msg_commit = await self._run_agent_action(
            "git_commit",
            {"working_dir": path, "message": "chore: initialize project scaffold"},
            confirmed=True,
        )
        # Git can return non-zero for no-op commits; treat this as non-fatal.
        if not ok_commit and "nothing to commit" in (msg_commit or "").lower():
            ok_commit = True
            msg_commit = "nothing to commit"
        bootstrap_notes.append("git_add: ok" if ok_add else f"git_add: failed ({msg_add})")
        if ok_commit and msg_commit == "nothing to commit":
            bootstrap_notes.append("git_commit: skipped (nothing to commit)")
        else:
            bootstrap_notes.append(
                "git_commit: ok" if ok_commit else f"git_commit: failed ({msg_commit})"
            )
        if not ok_add:
            return "; ".join(bootstrap_notes), False

        if cfg.AUTO_CREATE_GITHUB_REPO:
            ok_gh, msg_gh = await self._run_agent_action(
                "gh_create_repo",
                {
                    "working_dir": path,
                    "repo_name": slug,
                    "description": project.get("display_name", slug),
                    "private": cfg.AUTO_CREATE_GITHUB_PRIVATE,
                },
                confirmed=True,
            )
            if ok_gh:
                repo_url = self._extract_github_url(msg_gh) or ""
                if not repo_url and cfg.GITHUB_USERNAME:
                    repo_url = f"https://github.com/{cfg.GITHUB_USERNAME}/{slug}"
                if repo_url:
                    await store.update_project(
                        self.db,
                        project["id"],
                        github_repo=repo_url,
                    )
                    bootstrap_notes.append(f"github: ok ({repo_url})")
                else:
                    bootstrap_notes.append("github: ok")
            else:
                # Repo creation is optional; keep bootstrap successful without it.
                bootstrap_notes.append(f"github: warning ({msg_gh})")

        return "; ".join(bootstrap_notes), True

    async def _run_agent_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        confirmed: bool,
    ) -> tuple[bool, str]:
        payload = {
            "action": action,
            "params": params,
            "confirmed": confirmed,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.scheduler.gateway_url}/action",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=130),
                ) as resp:
                    data = await resp.json()
        except Exception as exc:
            return False, str(exc)

        if data.get("error"):
            return False, str(data.get("error"))
        if data.get("status") == "error":
            return False, data.get("error", "Unknown error")

        inner = data.get("result", {})
        rc = inner.get("returncode", 0)
        if rc != 0:
            stderr = (inner.get("stderr") or "").strip()
            stdout = (inner.get("stdout") or "").strip()
            return False, stderr or stdout or f"exit code {rc}"
        return True, (inner.get("stdout") or "").strip()

    async def _run_agent_action_for_planner(
        self,
        action: str,
        params: dict[str, Any],
        confirmed: bool,
    ) -> tuple[bool, str]:
        return await self._run_agent_action(action, params, confirmed=confirmed)

    @staticmethod
    def _extract_github_url(text: str) -> str | None:
        if not text:
            return None
        match = re.search(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text)
        return match.group(0) if match else None
