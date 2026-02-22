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
        self._always_on_prompt_skill_names: list[str] = []
        self._always_on_snippet_chars: int = 1200

    @staticmethod
    def _norm_skill_name(value: str) -> str:
        return re.sub(r"[\s_]+", "-", (value or "").strip().lower())

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
        name_norm = self._norm_skill_name(name)
        self._prompt_skills.append({
            "name": name.strip(),
            "name_norm": name_norm,
            "description": description.strip(),
            "content": content.strip(),
            "source": source.strip(),
            "search_blob": f"{name}\n{description}\n{content}".lower(),
        })
        logger.debug("Registered external prompt skill: %s", name)

    def set_always_on_prompt_skills(
        self,
        names: list[str] | None,
        *,
        snippet_chars: int = 1200,
    ) -> None:
        """
        Set prompt skills that should always be injected into context.

        Names are normalized (case-insensitive, spaces/underscores treated as hyphens).
        """
        normalized: list[str] = []
        for raw in names or []:
            norm = self._norm_skill_name(raw)
            if norm and norm not in normalized:
                normalized.append(norm)
        self._always_on_prompt_skill_names = normalized
        self._always_on_snippet_chars = max(300, int(snippet_chars or 1200))

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
                "always_on": s["name_norm"] in self._always_on_prompt_skill_names,
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
        Return always-on + top-matching external prompt-skill snippets.

        `max_skills` applies to query-matched skills in addition to always-on skills.
        This augments system prompts without changing tool schema.
        """
        del role  # Reserved for future role-specific filtering.
        if not self._prompt_skills:
            return ""

        text = (query or "").strip().lower()
        if not text and not self._always_on_prompt_skill_names:
            return ""

        tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text) if len(t) >= 4]

        always_on_items: list[dict[str, str]] = []
        if self._always_on_prompt_skill_names:
            by_norm: dict[str, dict[str, str]] = {}
            for item in self._prompt_skills:
                by_norm.setdefault(item["name_norm"], item)
            for norm in self._always_on_prompt_skill_names:
                item = by_norm.get(norm)
                if item and item not in always_on_items:
                    always_on_items.append(item)

        always_on_set = {item["name_norm"] for item in always_on_items}
        scored: list[tuple[int, dict[str, str]]] = []
        for item in self._prompt_skills:
            if item["name_norm"] in always_on_set:
                continue
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

        if not scored and not always_on_items:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [*always_on_items, *[item for _, item in scored[:max_skills]]]
        always_on_cap = self._always_on_snippet_chars
        if always_on_items:
            # Reserve space using actual fixed overhead so all always-on skills fit.
            fixed_overhead = 0
            for item in always_on_items:
                fixed_overhead += len(f"[Skill: {item['name']}]\n{item['description']}\n\n")
                fixed_overhead += len("\n... (always-on snippet truncated)")
                fixed_overhead += 2  # separator/newline slack
            content_budget = max(max_chars - fixed_overhead, len(always_on_items) * 180)
            always_on_cap = max(180, min(self._always_on_snippet_chars, content_budget // len(always_on_items)))

        parts: list[str] = []
        char_count = 0
        for item in selected:
            header = f"[Skill: {item['name']}]"
            desc = item["description"]
            content = item["content"]
            if item["name_norm"] in always_on_set and len(content) > always_on_cap:
                content = content[:always_on_cap].rstrip()
                content += "\n... (always-on snippet truncated)"
            block = f"{header}\n{desc}\n\n{content}"
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
    always_on_prompt_skills: list[str] | None = None,
    always_on_prompt_snippet_chars: int = 1200,
) -> SkillRegistry:
    """Build the default skill registry with built-in + external prompt skills."""
    from .filesystem import FilesystemSkill
    from .git import GitSkill
    from .build import BuildSkill
    from .search import SearchSkill
    from .ide import IDESkill
    from .docker import DockerSkill
    from .skynet_delegate import SkynetDelegateSkill
    from .project_skill import ProjectManagementSkill
    from .external_prompt_loader import load_external_prompt_skills

    registry = SkillRegistry()
    registry.register(FilesystemSkill())
    registry.register(GitSkill())
    registry.register(BuildSkill())
    registry.register(SearchSkill())
    registry.register(IDESkill())
    registry.register(DockerSkill())
    registry.register(SkynetDelegateSkill())
    registry.register(ProjectManagementSkill())

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
    external_skill_cache_dir = os.environ.get(
        "SKYNET_EXTERNAL_SKILLS_CACHE_DIR",
        os.environ.get("OPENCLAW_EXTERNAL_SKILLS_CACHE_DIR", ""),
    ).strip() or None

    registry.set_always_on_prompt_skills(
        always_on_prompt_skills,
        snippet_chars=always_on_prompt_snippet_chars,
    )

    try:
        external_items = load_external_prompt_skills(
            external_skills_dir,
            skill_urls=external_skill_urls,
            cache_root=external_skill_cache_dir,
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
    if registry._always_on_prompt_skill_names:
        logger.info(
            "Always-on prompt skills: %s (snippet_chars=%d)",
            ", ".join(registry._always_on_prompt_skill_names),
            registry._always_on_snippet_chars,
        )
    return registry
