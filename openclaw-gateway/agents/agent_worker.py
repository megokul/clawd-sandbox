"""
SKYNET — Agent Worker

Top-level project executor that delegates tasks to specialized agents.
Replaces Worker.run() with agent-aware task dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

import aiosqlite

from ai.provider_router import ProviderRouter
from ai.prompts import get_agent_prompt
from db import store
from search.web_search import WebSearcher
from skills.registry import SkillRegistry
from .roles import AGENT_CONFIGS, DEFAULT_ROLE
from .specialized import SpecializedAgent

logger = logging.getLogger("skynet.agents.worker")


class AgentWorker:
    """
    Drives a project through its plan by dispatching tasks
    to specialized agents based on their assigned roles.
    """

    def __init__(
        self,
        project_id: str,
        db: aiosqlite.Connection,
        router: ProviderRouter,
        searcher: WebSearcher,
        skill_registry: SkillRegistry,
        memory_manager: Any | None,
        gateway_api_url: str,
        pause_event: asyncio.Event,
        cancel_event: asyncio.Event,
        on_progress: Callable[[str, str, str], Awaitable[None]],
        request_approval: Callable[[str, str, dict], Awaitable[bool]],
    ):
        self.project_id = project_id
        self.db = db
        self.router = router
        self.searcher = searcher
        self.skill_registry = skill_registry
        self.memory_manager = memory_manager
        self.gateway_url = gateway_api_url
        self.pause_event = pause_event
        self.cancel_event = cancel_event
        self.on_progress = on_progress
        self.request_approval = request_approval

    async def run(self) -> None:
        """Main execution loop — fetch tasks, dispatch to agents, test."""
        project = await store.get_project(self.db, self.project_id)
        if not project:
            logger.error("Project %s not found", self.project_id)
            return

        plan = await store.get_active_plan(self.db, self.project_id)
        if not plan:
            logger.error("No active plan for project %s", self.project_id)
            return

        tasks = await store.get_tasks(self.db, self.project_id, plan["id"])

        await store.update_project(self.db, self.project_id, status="coding")
        await self._notify("started", f"Coding started for {project['display_name']}")

        # Initialize memory for all agent roles used in this project.
        roles_needed = set(t.get("assigned_agent_role", DEFAULT_ROLE) for t in tasks)
        for role in roles_needed:
            agent_id = await self._get_or_create_agent(role)
            if self.memory_manager:
                config = AGENT_CONFIGS.get(role, AGENT_CONFIGS[DEFAULT_ROLE])
                await self.memory_manager.initialize_agent_memory(
                    agent_id, role, project, config,
                )

        try:
            milestone_order, milestone_totals = self._build_milestone_index(tasks)
            milestone_done: dict[str, int] = {name: 0 for name in milestone_order}
            current_milestone: str | None = None

            total = len(tasks)
            for i, task in enumerate(tasks):
                if self.cancel_event.is_set():
                    await self._notify("cancelled", "Project cancelled by user.")
                    await store.update_project(self.db, self.project_id, status="cancelled")
                    return

                # Wait if paused.
                if not self.pause_event.is_set():
                    await self._notify("paused", "Project paused. Send /resume_project to continue.")
                    await self.pause_event.wait()
                    await self._notify("resumed", "Project resumed.")

                milestone_name = self._task_milestone(task)
                if milestone_name != current_milestone:
                    if current_milestone is not None:
                        await self._notify(
                            "milestone_review",
                            self._milestone_summary(
                                current_milestone,
                                milestone_done.get(current_milestone, 0),
                                milestone_totals.get(current_milestone, 0),
                                i,
                                total,
                            ),
                        )
                    current_milestone = milestone_name
                    await self._notify(
                        "milestone_started",
                        self._milestone_start_summary(
                            milestone_name,
                            milestone_order,
                            milestone_totals,
                        ),
                    )

                role = task.get("assigned_agent_role", DEFAULT_ROLE)
                await self._execute_task_with_agent(project, task, role, i + 1, total)
                milestone_done[milestone_name] = milestone_done.get(milestone_name, 0) + 1

            # Final testing phase using the testing agent.
            await self._final_testing(project)

            if current_milestone is not None:
                await self._notify(
                    "milestone_review",
                    self._milestone_summary(
                        current_milestone,
                        milestone_done.get(current_milestone, 0),
                        milestone_totals.get(current_milestone, 0),
                        total,
                        total,
                    ),
                )

            # Sync memory to S3.
            if self.memory_manager:
                await self.memory_manager.sync_to_s3(self.project_id)

            await store.update_project(self.db, self.project_id, status="completed")
            await self._notify(
                "completed",
                f"Project {project['display_name']} is complete!"
                + (f"\nGitHub: {project.get('github_repo', '')}" if project.get("github_repo") else ""),
            )

        except Exception as exc:
            logger.exception("AgentWorker error for project %s", self.project_id)
            await store.update_project(self.db, self.project_id, status="failed")
            await self._notify("error", f"Project failed: {exc}")

    @staticmethod
    def _task_milestone(task: dict[str, Any]) -> str:
        name = (task.get("milestone") or "").strip()
        return name or "General"

    def _build_milestone_index(
        self,
        tasks: list[dict[str, Any]],
    ) -> tuple[list[str], dict[str, int]]:
        order: list[str] = []
        totals: dict[str, int] = {}
        for task in tasks:
            name = self._task_milestone(task)
            if name not in totals:
                order.append(name)
                totals[name] = 0
            totals[name] += 1
        return order, totals

    @staticmethod
    def _milestone_summary(
        milestone: str,
        done_in_milestone: int,
        total_in_milestone: int,
        done_all: int,
        total_all: int,
    ) -> str:
        return (
            f"Milestone review: {milestone}\n"
            f"Milestone progress: {done_in_milestone}/{total_in_milestone} tasks\n"
            f"Overall progress: {done_all}/{total_all} tasks"
        )

    @staticmethod
    def _milestone_start_summary(
        milestone: str,
        milestone_order: list[str],
        milestone_totals: dict[str, int],
    ) -> str:
        idx = milestone_order.index(milestone) + 1 if milestone in milestone_order else 1
        total = len(milestone_order) if milestone_order else 1
        return (
            f"Starting milestone {idx}/{total}: {milestone} "
            f"({milestone_totals.get(milestone, 0)} tasks)"
        )

    async def _execute_task_with_agent(
        self,
        project: dict,
        task: dict,
        role: str,
        task_num: int,
        total_tasks: int,
    ) -> None:
        """Create a specialized agent and execute one task."""
        agent_id = await self._get_or_create_agent(role)
        config = AGENT_CONFIGS.get(role, AGENT_CONFIGS[DEFAULT_ROLE])

        await store.update_task(
            self.db, task["id"],
            status="in_progress", started_at=store._now(),
        )
        await self._notify(
            "task_started",
            f"[{task_num}/{total_tasks}] {config['display_name']}: {task['title']}",
        )

        agent = SpecializedAgent(
            agent_id=agent_id,
            role=role,
            project_id=self.project_id,
            db=self.db,
            router=self.router,
            searcher=self.searcher,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_manager,
            gateway_api_url=self.gateway_url,
            pause_event=self.pause_event,
            cancel_event=self.cancel_event,
            on_progress=self.on_progress,
            request_approval=self.request_approval,
        )

        final_text = await agent.execute_task(project, task)

        # Update task and agent records.
        await store.update_task(
            self.db, task["id"],
            status="completed",
            result_summary=final_text[:500],
            completed_at=store._now(),
        )
        await store.update_agent(
            self.db, agent_id,
            status="idle",
            tasks_completed_delta=1,
            last_active_at=store._now(),
        )
        await self._notify("task_completed", f"Completed: {task['title']}")

    async def _final_testing(self, project: dict) -> None:
        """Run final validation using the testing agent."""
        await self._notify("testing", "Running final tests with Testing Agent...")

        agent_id = await self._get_or_create_agent("testing")
        agent = SpecializedAgent(
            agent_id=agent_id,
            role="testing",
            project_id=self.project_id,
            db=self.db,
            router=self.router,
            searcher=self.searcher,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_manager,
            gateway_api_url=self.gateway_url,
            pause_event=self.pause_event,
            cancel_event=self.cancel_event,
            on_progress=self.on_progress,
            request_approval=self.request_approval,
        )

        test_task = {
            "title": "Final Testing & Validation",
            "description": "Run all tests, lint the project, and validate everything works.",
            "milestone": "Quality Assurance",
        }
        await agent.execute_task(project, test_task)

    async def _get_or_create_agent(self, role: str) -> str:
        """Get or create an agent record for this project + role."""
        existing = await store.get_agent_by_project_role(self.db, self.project_id, role)
        if existing:
            return existing["id"]

        agent_id = await store.create_agent(self.db, self.project_id, role)
        return agent_id

    async def _notify(self, event_type: str, summary: str) -> None:
        await store.add_event(self.db, self.project_id, event_type, summary)
        try:
            await self.on_progress(self.project_id, event_type, summary)
        except Exception:
            logger.exception("Progress callback failed")
