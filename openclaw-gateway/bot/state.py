"""
bot/state.py -- All module-level mutable globals for the SKYNET Telegram bot.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import bot_config as cfg
from agents.main_persona import MainPersonaAgent


class _TTLDict(dict):
    """dict that evicts entries older than ttl_seconds on every write.

    asyncio.Future values are cancelled before eviction so callers that are
    waiting on them get an immediate CancelledError rather than hanging forever.
    """

    def __init__(self, ttl_seconds: int) -> None:
        super().__init__()
        self._ttl = ttl_seconds
        self._timestamps: dict = {}

    def __setitem__(self, key, value):
        self._evict()
        self._timestamps[key] = time.monotonic()
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._timestamps.pop(key, None)
        super().__delitem__(key)

    def pop(self, key, *args):
        self._timestamps.pop(key, None)
        return super().pop(key, *args)

    def _evict(self):
        now = time.monotonic()
        stale = [k for k, ts in self._timestamps.items() if now - ts >= self._ttl]
        for k in stale:
            value = self.get(k)
            if isinstance(value, asyncio.Future) and not value.done():
                value.cancel()
            super().pop(k, None)
            self._timestamps.pop(k, None)

# ---------------------------------------------------------------------------
# Injected at startup by main.py.
# ---------------------------------------------------------------------------
_project_manager = None
_provider_router = None
_heartbeat = None
_sentinel = None
_searcher = None
_skill_registry = None

# Stores pending CONFIRM actions keyed by a short ID.
_pending_confirms: _TTLDict = _TTLDict(ttl_seconds=1800)
_confirm_counter: int = 0

# Stores pending approval futures from the orchestrator worker.
# { "key": asyncio.Future }
_pending_approvals: _TTLDict = _TTLDict(ttl_seconds=600)
_approval_counter: int = 0
# Stores pending destructive remove-project confirmations.
_pending_project_removals: _TTLDict = _TTLDict(ttl_seconds=300)
_background_tasks: set[asyncio.Task] = set()

_DOC_LLM_TARGET_PATHS: tuple[str, ...] = (
    "docs/product/PRD.md",
    "docs/product/overview.md",
    "docs/product/features.md",
    "docs/architecture/overview.md",
    "docs/architecture/system-design.md",
    "docs/architecture/data-flow.md",
    "docs/runbooks/local-dev.md",
    "docs/runbooks/deploy.md",
    "docs/runbooks/recovery.md",
    "docs/guides/getting-started.md",
    "docs/guides/configuration.md",
    "docs/decisions/ADR-001-tech-stack.md",
    "planning/task_plan.md",
    "planning/progress.md",
    "planning/findings.md",
)

_FINALIZED_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "skynet-project-documentation"
    / "templates"
)

# Reference to the Telegram app for sending proactive messages.
_bot_app = None  # Application | None -- assigned in build_app()

# Short rolling chat history for natural Telegram conversation.
_chat_history: list[dict] = []
_CHAT_HISTORY_MAX: int = 12
_CHAT_SYSTEM_PROMPT = """\
You are OpenClaw, an AI engineering collaborator running in Telegram.

## Conversation style
- Talk like a capable engineer working with the user, not a form or menu.
- Never be dismissive or sarcastic. For greetings reply briefly and naturally.
- Never show numbered option menus. Never tell the user to use slash commands.
- Ask one focused follow-up question when something important is unclear.
- If a tool fails, say so in one sentence and continue.
- Do not output JSON unless explicitly asked.

## Project work
- When the user describes what they want to build, immediately call project_add_idea.
- When creating a project, call project_create with the name.
- Gather requirements naturally through conversation — no rigid question forms.
- Once you have the problem, requirements, and tech stack, offer to write docs or generate the plan.
- When the user says they are ready, call project_generate_plan.
- Use project_generate_docs when asked to write the PRD or when enough context is captured.

## Other tools
- Use filesystem, git, build, docker, search, and IDE tools whenever execution is needed.
- When asked to use coding agents (codex/claude/cline), use check_coding_agents and run_coding_agent.
- Prefer delegated execution through tools for long-running work.\
"""
_last_project_id: str | None = None
_last_model_signature: str | None = None
_CHAT_PROVIDER_ALLOWLIST = (
    ["gemini"]
    if cfg.GEMINI_ONLY_MODE
    else ["gemini", "groq", "openrouter", "deepseek", "openai", "claude"]
)
_main_persona_agent = MainPersonaAgent()
_NO_STORE_ONCE_MARKERS = {
    "don't store this",
    "do not store this",
    "dont store this",
}
_NO_STORE_CHAT_MARKERS = {
    "don't store anything from this chat",
    "do not store anything from this chat",
    "dont store anything from this chat",
}


def set_dependencies(
    project_manager,
    provider_router,
    heartbeat=None,
    sentinel=None,
    searcher=None,
    skill_registry=None,
):
    """Called by main.py to inject dependencies."""
    global _project_manager, _provider_router, _heartbeat, _sentinel, _searcher, _skill_registry
    _project_manager = project_manager
    _provider_router = provider_router
    _heartbeat = heartbeat
    _sentinel = sentinel
    _searcher = searcher
    _skill_registry = skill_registry


# ------------------------------------------------------------------
# Helpers
