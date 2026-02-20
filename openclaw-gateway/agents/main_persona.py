"""
SKYNET - Main Persona Agent

Defines high-level interaction and delegation policy for Telegram chat.
"""

from __future__ import annotations

import re


class MainPersonaAgent:
    """Policy helper for main agent behavior."""

    DELEGATE_PATTERNS = (
        r"\bimplement\b",
        r"\bbuild\b",
        r"\bdeploy\b",
        r"\brefactor\b",
        r"\bintegrat(?:e|ion)\b",
        r"\bwrite tests?\b",
        r"\bcreate (?:an?|the)? ?(?:project|repo|service|pipeline)\b",
    )

    def compose_system_prompt(self, base_prompt: str, *, profile_context: str = "") -> str:
        policy = (
            "You are the main persona agent. "
            "Provide concise final responses and prefer delegating long-running work "
            "to planner/worker flows instead of attempting full execution inline."
        )
        if profile_context.strip():
            return f"{base_prompt}\n\n{policy}\n\n[User Profile]\n{profile_context.strip()}"
        return f"{base_prompt}\n\n{policy}"

    def should_delegate(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if len(lowered) >= 240:
            return True
        for pattern in self.DELEGATE_PATTERNS:
            if re.search(pattern, lowered):
                return True
        return False

    def compose_final_response(self, answer: str, *, task_report_summary: str = "") -> str:
        answer = (answer or "").strip()
        if not task_report_summary.strip():
            return answer
        return f"{answer}\n\nTask report summary: {task_report_summary.strip()}"
