"""
SKYNET - Planner Agent

Dedicated planner role for decomposing idea context into executable plans.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Awaitable

from ai.provider_router import ProviderRouter
from ai.tool_defs import PLANNING_TOOLS


class PlannerAgent:
    """Planner/decomposer wrapper around planning conversation flow."""

    def __init__(
        self,
        *,
        router: ProviderRouter,
        run_agent_action: Callable[[str, dict[str, Any], bool], Awaitable[tuple[bool, str]]],
    ) -> None:
        self.router = router
        self._run_agent_action = run_agent_action

    async def run_planning_conversation(
        self,
        *,
        messages: list[dict[str, Any]],
        system_prompt: str,
        max_rounds: int = 5,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run planning loop with limited tool use."""
        current = list(messages)
        last_response_text = ""

        for _ in range(max_rounds):
            response = await self.router.chat(
                current,
                tools=PLANNING_TOOLS,
                system=system_prompt,
                max_tokens=4096,
            )
            last_response_text = response.text or ""

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
            current.append({"role": "assistant", "content": parts or response.text})

            if not response.tool_calls:
                return last_response_text, current

            tool_results = []
            for tc in response.tool_calls:
                if tc.name == "web_search":
                    ok, output = await self._run_agent_action(
                        "web_search",
                        {
                            "query": tc.input.get("query", ""),
                            "num_results": tc.input.get("num_results", 5),
                        },
                        True,
                    )
                    result = output if ok else f"Web search unavailable: {output}"
                else:
                    result = f"Tool '{tc.name}' not available during planning."
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })
            current.append({"role": "user", "content": tool_results})

        return last_response_text, current

    @staticmethod
    def parse_plan_json(text: str) -> dict[str, Any] | None:
        """Extract plan JSON from model output."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None
