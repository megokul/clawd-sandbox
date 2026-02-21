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
# Stores pending natural-language follow-up for project-name capture by user id.
_pending_project_name_requests: _TTLDict = _TTLDict(ttl_seconds=1800)
# Stores pending routing choices when user says "start/make project" without clear target.
_pending_project_route_requests: _TTLDict = _TTLDict(ttl_seconds=1800)
# Stores pending destructive remove-project confirmations.
_pending_project_removals: _TTLDict = _TTLDict(ttl_seconds=300)
# Stores pending project documentation intake by user id.
_pending_project_doc_intake: _TTLDict = _TTLDict(ttl_seconds=3600)
_background_tasks: set[asyncio.Task] = set()

_PROJECT_DOC_INTAKE_STEPS: list[tuple[str, str]] = [
    ("problem", "What problem are we solving with this project?"),
    ("users", "Who are the primary users?"),
    ("requirements", "List the top requirements or features (comma-separated or bullets)."),
    ("non_goals", "What is explicitly out of scope?"),
    ("success_metrics", "How will we measure success?"),
    ("tech_stack", "Any preferred tech stack or constraints (language/framework/runtime)?"),
]
_DOC_INTAKE_FIELDS: tuple[str, ...] = tuple(field for field, _ in _PROJECT_DOC_INTAKE_STEPS)

_DOC_INTAKE_FIELD_LIMITS: dict[str, int] = {
    "problem": 1500,
    "users": 800,
    "requirements": 2000,
    "non_goals": 1200,
    "success_metrics": 1200,
    "tech_stack": 1200,
}

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
_CHAT_SYSTEM_PROMPT = (
    "You are OpenClaw running through Telegram. "
    "Converse naturally in plain language and extract key details from user text. "
    "Never be dismissive or sarcastic. "
    "For greetings (for example 'hi'), reply briefly and naturally without canned scripts. "
    "Use available tools/skills whenever execution, inspection, git, build, docker, or web research is needed. "
    "When asked to use coding agents, use check_coding_agents and run_coding_agent tools (codex/claude/cline CLIs). "
    "Ask concise clarifying questions only when required details are missing. "
    "Never ask the user to switch to slash commands; infer intent from natural language and run the matching action. "
    "Do not return numbered option menus unless the user explicitly asks for options. "
    "If a tool fails, explain it in one short sentence and continue with the best possible answer. "
    "Do not output JSON unless the user explicitly asks for JSON."
)
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
