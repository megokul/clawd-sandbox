"""SKYNET — Search Skill (web_search — executed by the laptop worker agent)."""

from __future__ import annotations
from typing import Any
from .base import BaseSkill, SkillContext


class SearchSkill(BaseSkill):
    name = "search"
    description = "Web search for programming resources and documentation"
    allowed_roles = []

    def get_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "web_search",
                "description": (
                    "Search the web for programming resources, library documentation, "
                    "API references, or implementation examples."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "num_results": {
                            "type": "integer",
                            "description": "Number of results (default: 5, max: 10)",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, tool_name: str, tool_input: dict[str, Any], context: SkillContext) -> str:
        if tool_name != "web_search":
            return f"Unknown search tool: {tool_name}"
        return await context.send_to_agent("web_search", {
            "query": tool_input.get("query", ""),
            "num_results": tool_input.get("num_results", 5),
        }, confirmed=True)
