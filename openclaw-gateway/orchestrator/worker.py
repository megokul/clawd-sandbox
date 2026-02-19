"""
SKYNET — Project Worker

Executes a single project's plan by driving the AI conversation loop.
For each task: send instructions to AI, let it use tools, collect results.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

import aiohttp
import aiosqlite

from ai.provider_router import ProviderRouter
from ai.tool_defs import CODING_TOOLS
from ai.prompts import CODING_PROMPT, TESTING_PROMPT
from ai import context as ctx
from db import store
from search.web_search import WebSearcher

logger = logging.getLogger("skynet.core.worker")

# Actions that are pre-approved when the user approves a plan.
PLAN_AUTO_APPROVED = {
    "file_write", "file_read", "list_directory", "create_directory",
    "git_init", "git_status", "git_add_all", "git_commit",
    "run_tests", "lint_project", "build_project", "install_dependencies",
    "open_in_vscode", "check_coding_agents", "run_coding_agent",
}

# Actions that always need individual Telegram approval.
ALWAYS_CONFIRM = {"git_push", "gh_create_repo"}

MAX_TOOL_ROUNDS = 30


class Worker:
    """Drives a single project through its plan tasks."""

    def __init__(
        self,
        project_id: str,
        db: aiosqlite.Connection,
        router: ProviderRouter,
        searcher: WebSearcher,
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
        self.gateway_url = gateway_api_url
        self.pause_event = pause_event
        self.cancel_event = cancel_event
        self.on_progress = on_progress
        self.request_approval = request_approval

    async def run(self) -> None:
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

        try:
            milestone_order, milestone_totals = self._build_milestone_index(tasks)
            milestone_done: dict[str, int] = {name: 0 for name in milestone_order}
            current_milestone: str | None = None

            # Execute each task.
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

                await self._execute_task(project, task, i + 1, total)
                milestone_done[milestone_name] = milestone_done.get(milestone_name, 0) + 1

            # Final testing phase.
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

            await store.update_project(self.db, self.project_id, status="completed")
            await self._notify(
                "completed",
                f"Project {project['display_name']} is complete!"
                + (f"\nGitHub: {project.get('github_repo', '')}" if project.get("github_repo") else ""),
            )

        except Exception as exc:
            logger.exception("Worker error for project %s", self.project_id)
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

    @staticmethod
    def _classify_task(task: dict) -> str:
        """Heuristic task classification for provider routing."""
        title = (task.get("title") or "").lower()
        desc = (task.get("description") or "").lower()
        milestone = (task.get("milestone") or "").lower()

        if any(w in title for w in ("scaffold", "setup", "init", "boilerplate", "create project")):
            return "scaffold"
        if any(w in title for w in ("crud", "model", "schema", "migration")):
            return "crud"
        if any(w in title for w in ("test", "spec", "jest", "pytest")):
            return "unit_test"
        if any(w in title for w in ("readme", "docs", "documentation")):
            return "readme_polish"
        if any(w in title for w in ("debug", "fix bug", "diagnose", "troubleshoot")):
            return "hard_debug"
        if any(w in title for w in ("refactor", "redesign", "restructure")):
            return "complex_refactor"
        if any(w in milestone for w in ("plan", "design", "architecture")):
            return "planning"
        return "general"

    async def _execute_task(
        self,
        project: dict,
        task: dict,
        task_num: int,
        total_tasks: int,
    ) -> None:
        await store.update_task(
            self.db, task["id"], status="in_progress", started_at=store._now(),
        )
        task_type = self._classify_task(task)
        await self._notify(
            "task_started",
            f"[{task_num}/{total_tasks}] {task.get('milestone', '')}: {task['title']} (route: {task_type})",
        )

        system_prompt = CODING_PROMPT.format(
            project_name=project["display_name"],
            project_description=project.get("description", ""),
            tech_stack=project.get("tech_stack", "{}"),
            current_milestone=task.get("milestone", ""),
            current_task=f"{task['title']}\n{task.get('description', '')}",
            project_path=project["local_path"],
        )

        # Get context limit from the first provider in the escalation chain.
        context_limit = self._get_target_context_limit()
        messages = await ctx.build_messages_for_provider(
            self.db, self.project_id,
            context_limit=context_limit,
            summarise_fn=self._summarise_callback,
        )
        messages.append({
            "role": "user",
            "content": f"Complete this task: {task['title']}\n\n{task.get('description', '')}",
        })

        final_text, updated_messages = await self._conversation_loop(
            messages, system_prompt, CODING_TOOLS, task_type=task_type,
        )

        await ctx.save_messages(self.db, self.project_id, updated_messages)
        await store.update_task(
            self.db, task["id"],
            status="completed",
            result_summary=final_text[:500],
            completed_at=store._now(),
        )
        await self._notify("task_completed", f"Completed: {task['title']}")

    def _get_target_context_limit(self, escalation_idx: int = 0) -> int:
        """Get the context limit of the target provider in the escalation chain."""
        if escalation_idx < len(self._ESCALATION_CHAIN):
            target_name = self._ESCALATION_CHAIN[escalation_idx]
            for p in self.router.providers:
                if p.name == target_name:
                    return p.context_limit
        # Fallback: use the smallest context limit across all providers.
        if self.router.providers:
            return min(p.context_limit for p in self.router.providers)
        return 32_000  # Conservative default.

    async def _summarise_callback(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> str:
        """Summarise messages using a cheap provider (Groq or Ollama)."""
        response = await self.router.chat(
            messages,
            system=system_prompt,
            max_tokens=1024,
            task_type="general",
            preferred_provider="groq",  # Cheap + fast for summarisation.
        )
        return response.text

    async def _final_testing(self, project: dict) -> None:
        await self._notify("testing", "Running final tests and validation...")
        system_prompt = TESTING_PROMPT.format(
            project_name=project["display_name"],
            project_path=project["local_path"],
        )
        messages = [{"role": "user", "content": "Run tests and validate the project."}]
        await self._conversation_loop(messages, system_prompt, CODING_TOOLS)

    # Escalation chain: try preferred first, then escalate.
    _ESCALATION_CHAIN = ["ollama", "groq", "gemini", "claude"]
    _MAX_EMPTY_RETRIES = 3

    async def _conversation_loop(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict],
        task_type: str = "general",
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        AI conversation loop with tool execution and escalation.

        Starts with the best provider for the task_type. If a provider
        returns empty/looping output, escalates to the next tier.

        Returns (final_text, all_messages).
        """
        current_messages = list(messages)
        rounds = 0
        empty_count = 0
        recent_tool_sigs: list[str] = []
        escalation_idx = 0

        while rounds < MAX_TOOL_ROUNDS:
            if self.cancel_event.is_set():
                break

            # Determine preferred provider (escalation-aware).
            preferred = None
            if escalation_idx < len(self._ESCALATION_CHAIN):
                preferred = self._ESCALATION_CHAIN[escalation_idx]

            response = await self.router.chat(
                current_messages,
                tools=tools,
                system=system_prompt,
                max_tokens=4096,
                require_tools=True,
                task_type=task_type,
                preferred_provider=preferred,
            )

            # Check for empty response — escalate if needed.
            if not response.text.strip() and not response.tool_calls:
                empty_count += 1
                logger.warning(
                    "Empty response from %s (attempt %d/%d)",
                    response.provider_name, empty_count, self._MAX_EMPTY_RETRIES,
                )
                if empty_count >= self._MAX_EMPTY_RETRIES:
                    escalation_idx += 1
                    empty_count = 0
                    if escalation_idx >= len(self._ESCALATION_CHAIN):
                        break  # All providers exhausted.
                    logger.info("Escalating to %s", self._ESCALATION_CHAIN[escalation_idx])
                continue

            empty_count = 0  # Reset on non-empty response.

            # Build assistant message from response.
            assistant_content = self._build_assistant_content(response)
            current_messages.append({"role": "assistant", "content": assistant_content})

            if not response.tool_calls:
                return response.text, current_messages

            # Detect tool call loops (same call 3x in a row).
            for tc in response.tool_calls:
                sig = f"{tc.name}:{json.dumps(tc.input, sort_keys=True)}"
                if sig in recent_tool_sigs[-3:]:
                    logger.warning("Tool call loop detected (%s), escalating", tc.name)
                    escalation_idx += 1
                    if escalation_idx >= len(self._ESCALATION_CHAIN):
                        break
                recent_tool_sigs.append(sig)

            # Execute tool calls.
            tool_results = []
            for tc in response.tool_calls:
                result = await self._execute_tool(tc.name, tc.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })

            current_messages.append({"role": "user", "content": tool_results})
            rounds += 1

        # Exceeded max rounds — ask for summary.
        current_messages.append({
            "role": "user",
            "content": "You have reached the tool use limit. Summarize what you accomplished.",
        })
        response = await self.router.chat(
            current_messages, system=system_prompt, max_tokens=2048,
            task_type="general",
        )
        current_messages.append({"role": "assistant", "content": response.text})
        return response.text, current_messages

    def _build_assistant_content(self, response) -> Any:
        """Build assistant message content including tool_use blocks."""
        from ai.providers.base import ProviderResponse
        parts = []
        if response.text:
            parts.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            parts.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        return parts if parts else response.text

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a single tool call."""
        # Web search is handled locally on EC2.
        if tool_name == "web_search":
            return await self.searcher.search(
                tool_input.get("query", ""),
                tool_input.get("num_results", 5),
            )

        # Determine if this action needs individual approval.
        if tool_name in ALWAYS_CONFIRM:
            approved = await self.request_approval(
                self.project_id, tool_name, tool_input,
            )
            if not approved:
                return f"Action '{tool_name}' was denied by the user."
            confirmed = True
        else:
            # Plan-approved actions send confirmed=True.
            confirmed = tool_name in PLAN_AUTO_APPROVED

        return await self._send_to_agent(tool_name, tool_input, confirmed)

    async def _send_to_agent(
        self,
        action: str,
        params: dict,
        confirmed: bool = True,
    ) -> str:
        """Send an action to the laptop agent via the gateway HTTP API."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.gateway_url}/action",
                    json={"action": action, "params": params, "confirmed": confirmed},
                    timeout=aiohttp.ClientTimeout(total=130),
                ) as resp:
                    result = await resp.json()
        except Exception as exc:
            return f"ERROR: Failed to reach agent: {exc}"

        if result.get("status") == "error":
            return f"ERROR: {result.get('error', 'Unknown error')}"

        inner = result.get("result", {})
        parts = []
        if inner.get("stdout"):
            parts.append(inner["stdout"])
        if inner.get("stderr"):
            parts.append(f"STDERR: {inner['stderr']}")
        rc = inner.get("returncode", "?")
        parts.append(f"[exit code: {rc}]")
        return "\n".join(parts) if parts else "OK"

    async def _notify(self, event_type: str, summary: str) -> None:
        await store.add_event(self.db, self.project_id, event_type, summary)
        try:
            await self.on_progress(self.project_id, event_type, summary)
        except Exception:
            logger.exception("Progress callback failed")
