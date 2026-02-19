"""
SKYNET — Specialized Agent

Evolved from orchestrator.worker.Worker. A SpecializedAgent has a role,
persona, preferred AI providers, and a role-filtered skill set.
It drives a single task through the AI conversation loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

import aiosqlite

from ai.provider_router import ProviderRouter
from ai import context as ctx
from ai.prompts import get_agent_prompt
from db import store
from search.web_search import WebSearcher
from skills.base import SkillContext
from skills.registry import SkillRegistry
from .roles import AGENT_CONFIGS, DEFAULT_ROLE

logger = logging.getLogger("skynet.agents.specialized")


class SpecializedAgent:
    """
    A specialized agent that executes tasks for a specific role.

    Keeps the proven _conversation_loop and tool execution logic
    from Worker, adding role identity, per-agent memory, and
    role-specific model preferences.
    """

    _MAX_EMPTY_RETRIES = 3

    def __init__(
        self,
        agent_id: str,
        role: str,
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
        self.agent_id = agent_id
        self.role = role
        self.config = AGENT_CONFIGS.get(role, AGENT_CONFIGS[DEFAULT_ROLE])
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
        self._project_path: str = ""

    async def execute_task(self, project: dict, task: dict) -> str:
        """Execute a single task with role-specific behavior."""
        self._project_path = project["local_path"]

        # Build role-specific system prompt.
        system_prompt = get_agent_prompt(
            role=self.role,
            project_name=project["display_name"],
            project_description=project.get("description", ""),
            tech_stack=project.get("tech_stack", "{}"),
            current_milestone=task.get("milestone", ""),
            current_task=f"{task['title']}\n{task.get('description', '')}",
            project_path=project["local_path"],
        )
        try:
            prompt_context = self.skill_registry.get_prompt_skill_context(
                f"{task.get('title', '')}\n{task.get('description', '')}",
                role=self.role,
            )
            if prompt_context:
                system_prompt += (
                    "\n\n[External Skill Guidance]\n"
                    "Use this community skill guidance if relevant:\n\n"
                    f"{prompt_context}"
                )
        except Exception:
            logger.exception("Failed to inject external skill guidance for role=%s", self.role)

        # Get tools filtered for this agent's role.
        tools = self.skill_registry.get_tools_for_role(self.role)

        # Load conversation history with context-aware summarization.
        context_limit = self._get_context_limit()
        messages = await ctx.build_messages_for_provider(
            self.db, self.project_id,
            context_limit=context_limit,
            summarise_fn=self._summarise_callback,
        )

        # Inject agent memory context.
        if self.memory_manager:
            memory_context = await self.memory_manager.get_context_for_agent(
                self.agent_id, self.project_id,
            )
            if memory_context:
                messages.insert(0, {
                    "role": "user",
                    "content": f"[AGENT MEMORY]\n{memory_context}\n[END AGENT MEMORY]",
                })

        messages.append({
            "role": "user",
            "content": f"Complete this task: {task['title']}\n\n{task.get('description', '')}",
        })

        # Run conversation loop.
        task_type = self.config.get("default_task_type", "general")
        final_text, updated_messages = await self._conversation_loop(
            messages, system_prompt, tools, task_type=task_type,
        )

        # Persist and update memory.
        await ctx.save_messages(self.db, self.project_id, updated_messages)
        if self.memory_manager:
            await self.memory_manager.update_from_task(
                self.agent_id, self.project_id, task, final_text,
            )

        return final_text

    # ------------------------------------------------------------------
    # Conversation loop (from Worker, with role-aware escalation)
    # ------------------------------------------------------------------

    async def _conversation_loop(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict],
        task_type: str = "general",
    ) -> tuple[str, list[dict[str, Any]]]:
        """AI conversation loop with tool execution and escalation."""
        max_rounds = self.config.get("max_tool_rounds", 30)
        escalation_chain = self.config.get("preferred_providers", ["ollama", "groq", "gemini", "claude"])
        current_messages = list(messages)
        rounds = 0
        empty_count = 0
        recent_tool_sigs: list[str] = []
        escalation_idx = 0

        while rounds < max_rounds:
            if self.cancel_event.is_set():
                break

            # Determine preferred provider.
            preferred = None
            if escalation_idx < len(escalation_chain):
                preferred = escalation_chain[escalation_idx]

            response = await self.router.chat(
                current_messages,
                tools=tools,
                system=system_prompt,
                max_tokens=4096,
                require_tools=True,
                task_type=task_type,
                preferred_provider=preferred,
            )

            # Empty response detection.
            if not response.text.strip() and not response.tool_calls:
                empty_count += 1
                logger.warning(
                    "[%s/%s] Empty response from %s (%d/%d)",
                    self.role, self.agent_id[:8],
                    response.provider_name, empty_count, self._MAX_EMPTY_RETRIES,
                )
                if empty_count >= self._MAX_EMPTY_RETRIES:
                    escalation_idx += 1
                    empty_count = 0
                    if escalation_idx >= len(escalation_chain):
                        break
                    logger.info("[%s] Escalating to %s", self.role, escalation_chain[escalation_idx])
                continue

            empty_count = 0

            # Build assistant message.
            assistant_content = self._build_assistant_content(response)
            current_messages.append({"role": "assistant", "content": assistant_content})

            if not response.tool_calls:
                return response.text, current_messages

            # Tool call loop detection.
            for tc in response.tool_calls:
                sig = f"{tc.name}:{json.dumps(tc.input, sort_keys=True)}"
                if sig in recent_tool_sigs[-3:]:
                    logger.warning("[%s] Tool loop detected (%s), escalating", self.role, tc.name)
                    escalation_idx += 1
                    if escalation_idx >= len(escalation_chain):
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

        # Exceeded max rounds.
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

    # ------------------------------------------------------------------
    # Tool execution (skill-registry-based)
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call via the skill registry."""
        skill = self.skill_registry.get_skill_for_tool(tool_name)
        if skill is None:
            return f"Unknown tool: {tool_name}"

        context = SkillContext(
            project_id=self.project_id,
            project_path=self._project_path,
            gateway_api_url=self.gateway_url,
            searcher=self.searcher,
            request_approval=self.request_approval,
        )
        return await skill.execute(tool_name, tool_input, context)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_assistant_content(self, response) -> Any:
        """Build assistant message content including tool_use blocks."""
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

    def _get_context_limit(self) -> int:
        """Get context limit from the first preferred provider."""
        providers = self.config.get("preferred_providers", [])
        for prov_name in providers:
            for p in self.router.providers:
                if p.name == prov_name:
                    return p.context_limit
        if self.router.providers:
            return min(p.context_limit for p in self.router.providers)
        return 32_000

    async def _summarise_callback(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> str:
        """Summarise messages using a cheap provider."""
        response = await self.router.chat(
            messages,
            system=system_prompt,
            max_tokens=1024,
            task_type="general",
            preferred_provider="groq",
        )
        return response.text
