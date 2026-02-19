"""
SKYNET — Skill Registry

Central registry for all available skills. Provides role-filtered
tool discovery and tool-to-skill routing.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("skynet.skills.registry")


class SkillRegistry:
    """Central registry for all available skills."""

    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}
        self._prompt_skills: list[dict[str, str]] = []

    def register(self, skill: BaseSkill) -> None:
        """Register a skill."""
        self._skills[skill.name] = skill
        logger.debug("Registered skill: %s (%d tools)", skill.name, len(skill.get_tool_names()))

    def register_prompt_skill(
        self,
        *,
        name: str,
        description: str,
        content: str,
        source: str,
    ) -> None:
        """Register a prompt-only external skill loaded from SKILL.md."""
        if not name.strip() or not content.strip():
            return
        self._prompt_skills.append({
            "name": name.strip(),
            "description": description.strip(),
            "content": content.strip(),
            "source": source.strip(),
            "search_blob": f"{name}\n{description}\n{content}".lower(),
        })
        logger.debug("Registered external prompt skill: %s", name)

    def get_tools_for_role(self, role: str) -> list[dict[str, Any]]:
        """Return combined tool definitions for an agent role."""
        tools = []
        for skill in self._skills.values():
            if not skill.allowed_roles or role in skill.allowed_roles:
                tools.extend(skill.get_tools())
        return tools

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Return all tool definitions (for backward compatibility)."""
        tools = []
        for skill in self._skills.values():
            tools.extend(skill.get_tools())
        return tools

    def get_skill_for_tool(self, tool_name: str) -> BaseSkill | None:
        """Find which skill handles a given tool name."""
        for skill in self._skills.values():
            if tool_name in skill.get_tool_names():
                return skill
        return None

    def is_plan_auto_approved(self, tool_name: str) -> bool:
        """Check if a tool is auto-approved when plan is approved."""
        skill = self.get_skill_for_tool(tool_name)
        return skill is not None and tool_name in skill.plan_auto_approved

    def requires_approval(self, tool_name: str) -> bool:
        """Check if a tool always requires Telegram approval."""
        skill = self.get_skill_for_tool(tool_name)
        return skill is not None and tool_name in skill.requires_approval

    def list_skills(self) -> list[dict[str, Any]]:
        """Return summary of all registered skills (for /skills command)."""
        tool_skills = [
            {
                "name": s.name,
                "description": s.description,
                "tools": sorted(s.get_tool_names()),
                "allowed_roles": s.allowed_roles or ["all"],
                "kind": "tool",
            }
            for s in self._skills.values()
        ]
        prompt_skills = [
            {
                "name": s["name"],
                "description": s["description"],
                "tools": [],
                "allowed_roles": ["all"],
                "kind": "prompt",
                "source": s["source"],
            }
            for s in self._prompt_skills
        ]
        return [*tool_skills, *prompt_skills]

    def get_prompt_skill_context(
        self,
        query: str,
        *,
        role: str = "general",
        max_skills: int = 3,
        max_chars: int = 6000,
    ) -> str:
        """
        Return top-matching external prompt-skill snippets for a query.

        This augments system prompts without changing tool schema.
        """
        del role  # Reserved for future role-specific filtering.
        if not self._prompt_skills:
            return ""

        text = (query or "").strip().lower()
        if not text:
            return ""

        tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text) if len(t) >= 4]

        scored: list[tuple[int, dict[str, str]]] = []
        for item in self._prompt_skills:
            score = 0
            blob = item["search_blob"]
            name_lower = item["name"].lower()
            if name_lower in text:
                score += 10
            for tok in tokens:
                if tok in blob:
                    score += 1
            if score > 0:
                scored.append((score, item))

        if not scored:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [item for _, item in scored[:max_skills]]

        parts: list[str] = []
        char_count = 0
        for item in selected:
            header = f"[Skill: {item['name']}]"
            desc = item["description"]
            block = f"{header}\n{desc}\n\n{item['content']}"
            if char_count + len(block) > max_chars:
                remaining = max_chars - char_count
                if remaining <= 120:
                    break
                block = block[:remaining] + "\n... (truncated)"
            parts.append(block)
            char_count += len(block)
            if char_count >= max_chars:
                break

        return "\n\n".join(parts)

    @property
    def skill_count(self) -> int:
        return len(self._skills) + len(self._prompt_skills)

    @property
    def prompt_skill_count(self) -> int:
        return len(self._prompt_skills)


def build_default_registry(
    *,
    external_skills_dir: str | None = None,
    external_skill_urls: list[str] | None = None,
) -> SkillRegistry:
    """Build the default skill registry with built-in + external prompt skills."""
    from .filesystem import FilesystemSkill
    from .git import GitSkill
    from .build import BuildSkill
    from .search import SearchSkill
    from .ide import IDESkill
    from .docker import DockerSkill
    from .skynet_delegate import SkynetDelegateSkill
    from .external_prompt_loader import load_external_prompt_skills

    registry = SkillRegistry()
    registry.register(FilesystemSkill())
    registry.register(GitSkill())
    registry.register(BuildSkill())
    registry.register(SearchSkill())
    registry.register(IDESkill())
    registry.register(DockerSkill())
    registry.register(SkynetDelegateSkill())

    if external_skills_dir is None:
        external_skills_dir = os.environ.get(
            "SKYNET_EXTERNAL_SKILLS_DIR",
            os.environ.get(
                "OPENCLAW_EXTERNAL_SKILLS_DIR",
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "external-skills"),
            ),
        )
    if external_skill_urls is None:
        raw_urls = os.environ.get(
            "SKYNET_EXTERNAL_SKILL_URLS",
            os.environ.get("OPENCLAW_EXTERNAL_SKILL_URLS", ""),
        )
        external_skill_urls = [u.strip() for u in raw_urls.replace("\n", ",").split(",") if u.strip()]

    try:
        external_items = load_external_prompt_skills(
            external_skills_dir,
            skill_urls=external_skill_urls,
        )
        for item in external_items:
            registry.register_prompt_skill(
                name=item.name,
                description=item.description,
                content=item.content,
                source=item.source,
            )
    except Exception:
        logger.exception("Failed loading external prompt skills from %s", external_skills_dir)

    logger.info(
        "Skill registry ready: %d total skills (%d prompt-only), %d total tools",
        registry.skill_count,
        registry.prompt_skill_count,
        sum(len(s.get_tool_names()) for s in registry._skills.values()),
    )
    return registry
