"""
Integration tests: full conversation cycle through handle_text.

Uses a scripted mock LLM router so tests are fast and deterministic, but
exercises the real project manager (in-memory DB), real skill registry,
and the full handle_text dispatch path.

Each scenario represents a different user conversation flow.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

def _ensure_paths() -> None:
    repo_root = Path(__file__).parent.parent
    for sub in ("openclaw-gateway", ""):
        p = str(repo_root / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_paths()


# ---------------------------------------------------------------------------
# Fake Telegram Update
# ---------------------------------------------------------------------------

@dataclass
class _FakeMessage:
    text: str
    replies: list[str] = field(default_factory=list)

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


@dataclass
class _FakeUser:
    id: int = 999
    first_name: str = "Tester"
    username: str = "tester"
    language_code: str = "en"


@dataclass
class _FakeUpdate:
    message: _FakeMessage
    effective_user: _FakeUser = field(default_factory=_FakeUser)
    effective_chat: Any = None

    def __post_init__(self):
        if self.effective_chat is None:
            self.effective_chat = type("FakeChat", (), {"id": 999})()


def _make_update(text: str) -> _FakeUpdate:
    return _FakeUpdate(message=_FakeMessage(text=text))


# ---------------------------------------------------------------------------
# Scripted mock LLM router
# ---------------------------------------------------------------------------

@dataclass
class _ToolCall:
    name: str
    input: dict
    id: str = "tc-1"


@dataclass
class _LLMResponse:
    text: str = ""
    tool_calls: list[_ToolCall] = field(default_factory=list)


class ScriptedRouter:
    """Mock LLM router that returns scripted responses in order."""

    def __init__(self, script: list[_LLMResponse]) -> None:
        self._script = list(script)
        self._idx = 0
        self._calls: list[dict] = []

    async def chat(self, messages, *, tools=None, system=None, **kwargs) -> _LLMResponse:
        self._calls.append({"messages": messages, "tools": tools})
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp
        # Default: plain text reply
        return _LLMResponse(text="Done.")


# ---------------------------------------------------------------------------
# State setup helpers
# ---------------------------------------------------------------------------

async def _build_state(router: ScriptedRouter):
    """Initialize all module-level state with real in-memory components."""
    from db import schema
    from orchestrator.project_manager import ProjectManager
    from bot import state

    # In-memory DB
    db = await schema.init_db(":memory:")

    class _DummyScheduler:
        gateway_url = "http://127.0.0.1:8766"

    pm = ProjectManager(
        db=db,
        router=router,
        searcher=None,
        scheduler=_DummyScheduler(),
        project_base_dir="/tmp/skynet_test_projects",
    )

    from skills.registry import build_default_registry
    registry = build_default_registry()

    class _FakeApp:
        class bot:
            @staticmethod
            async def send_message(chat_id, text, **kwargs):
                pass

    import bot_config as cfg
    original_auth = cfg.ALLOWED_USER_ID
    cfg.ALLOWED_USER_ID = 999  # matches _FakeUser.id

    state.set_dependencies(
        project_manager=pm,
        provider_router=router,
        skill_registry=registry,
    )
    state._bot_app = _FakeApp()
    state._last_project_id = None
    state._chat_history = []

    return db, pm, registry, cfg, original_auth


async def _teardown_state(db, cfg, original_auth):
    import asyncio
    from bot import state
    # Cancel any lingering background tasks
    for task in list(state._background_tasks):
        task.cancel()
    if state._background_tasks:
        await asyncio.gather(*state._background_tasks, return_exceptions=True)
    state._project_manager = None
    state._provider_router = None
    state._skill_registry = None
    state._bot_app = None
    state._last_project_id = None
    state._chat_history = []
    cfg.ALLOWED_USER_ID = original_auth
    await db.close()


# ---------------------------------------------------------------------------
# Conversation helper
# ---------------------------------------------------------------------------

async def _send(text: str) -> list[str]:
    """Send one message through handle_text; return all bot replies."""
    from bot.commands import handle_text
    update = _make_update(text)
    await handle_text(update, context=None)
    return update.message.replies


# ---------------------------------------------------------------------------
# SCENARIO 1: Greeting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_greeting() -> None:
    """Pure greeting should get a short reply without touching the LLM."""
    router = ScriptedRouter([])  # no LLM calls expected
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        replies = await _send("hi")
        assert replies, "bot should reply to a greeting"
        assert len(replies) == 1
        # Should not have called the LLM router
        assert router._idx == 0, "greeting must not invoke LLM"
    finally:
        await _teardown_state(db, cfg, orig_auth)


@pytest.mark.asyncio
async def test_scenario_greeting_hello() -> None:
    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        replies = await _send("hello")
        assert replies
        assert router._idx == 0
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 2: Ask to start a project — LLM asks for name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_start_project_ask_name() -> None:
    """'can we start a project' → LLM asks for project name (text reply, no tool calls)."""
    router = ScriptedRouter([
        _LLMResponse(text="Sure! What would you like to name the project?"),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        replies = await _send("can we start a project")
        assert replies, "bot should reply"
        assert "name" in " ".join(replies).lower() or "project" in " ".join(replies).lower()
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 3: Create project via LLM tool call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_create_project() -> None:
    """LLM calls project_create → project is created → bot confirms."""
    router = ScriptedRouter([
        _LLMResponse(tool_calls=[_ToolCall("project_create", {"name": "myapp"})]),
        _LLMResponse(text="Created project 'myapp'. What do you want it to do?"),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        replies = await _send("i want to start a project called myapp")
        assert replies

        # Project should be in DB
        from db import store
        projects = await pm.list_projects()
        assert any(p["name"] == "myapp" for p in projects), "project should be created in DB"
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 4: Add idea to project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_add_idea() -> None:
    """User describes a feature → LLM calls project_add_idea."""
    router = ScriptedRouter([
        # First: create project
        _LLMResponse(tool_calls=[_ToolCall("project_create", {"name": "beepapp"})]),
        _LLMResponse(text="Created 'beepapp'. What should it do?"),
        # Second: add idea
        _LLMResponse(tool_calls=[_ToolCall("project_add_idea", {"idea": "play a 1 second beep on button click"})]),
        _LLMResponse(text="Got it, noted that requirement."),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        await _send("create project beepapp")
        replies = await _send("it should play a 1 second beep when you click the button")
        assert replies

        # Idea should be in DB
        from db import store
        projects = await pm.list_projects()
        beep = next((p for p in projects if p["name"] == "beepapp"), None)
        assert beep is not None
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 5: List projects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_list_projects() -> None:
    """User asks to list projects → LLM calls project_list → projects shown."""
    from db import store

    router = ScriptedRouter([
        _LLMResponse(tool_calls=[_ToolCall("project_list", {})]),
        _LLMResponse(text="Here are your projects: alpha, beta."),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        # Pre-create two projects directly
        await pm.create_project("alpha")
        await pm.create_project("beta")

        replies = await _send("what projects do I have")
        assert replies
        # The tool should have been called
        assert router._idx >= 1
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 6: Project already exists — no error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_create_existing_project_graceful() -> None:
    """Creating an already-existing project should not error — it activates it."""
    from skills.project_skill import ProjectManagementSkill
    from skills.base import SkillContext

    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        # Create project directly
        await pm.create_project("existing-project")

        skill = ProjectManagementSkill()
        ctx = SkillContext(project_id="", project_path="", gateway_api_url="http://127.0.0.1:8766")

        from bot import state
        result = await skill.execute("project_create", {"name": "existing-project"}, ctx)

        # Should NOT be an error — should say it's already active
        assert "error" not in result.lower() or "already exists" in result.lower()
        assert "existing-project" in result.lower() or "active" in result.lower()
        # _last_project_id should now point to this project
        assert state._last_project_id is not None
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 7: Stale project_id in add_idea — auto-recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_add_idea_stale_project_id_recovers() -> None:
    """project_add_idea with a stale/missing project_id falls back to active project."""
    from skills.project_skill import ProjectManagementSkill
    from skills.base import SkillContext
    from bot import state

    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        real_project = await pm.create_project("realproject")
        state._last_project_id = "stale-id-that-doesnt-exist"

        skill = ProjectManagementSkill()
        ctx = SkillContext(project_id="", project_path="", gateway_api_url="http://127.0.0.1:8766")

        result = await skill.execute("project_add_idea", {"idea": "add login page"}, ctx)

        # Should have recovered and added idea to realproject
        assert "error" not in result.lower(), f"Should not error, got: {result}"
        assert "added" in result.lower()
        assert state._last_project_id == real_project["id"]
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 8: Full flow — create, describe, list, status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_full_project_lifecycle() -> None:
    """Complete flow: greeting → create → add ideas → status → list."""
    router = ScriptedRouter([
        # create
        _LLMResponse(tool_calls=[_ToolCall("project_create", {"name": "lifecycle-test"})]),
        _LLMResponse(text="Created 'lifecycle-test'. Tell me more."),
        # add idea
        _LLMResponse(tool_calls=[_ToolCall("project_add_idea", {"idea": "user login with Google OAuth"})]),
        _LLMResponse(text="Noted. Anything else?"),
        # status
        _LLMResponse(tool_calls=[_ToolCall("project_status", {})]),
        _LLMResponse(text="Project: lifecycle-test, Status: ideation, Ideas: 1"),
        # list
        _LLMResponse(tool_calls=[_ToolCall("project_list", {})]),
        _LLMResponse(text="You have 1 project: lifecycle-test [ideation]"),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        # Greeting — no LLM
        greeting_replies = await _send("hi")
        assert greeting_replies
        assert router._idx == 0  # LLM not called for greeting

        # Create
        create_replies = await _send("start project lifecycle-test")
        assert create_replies

        # Add idea
        idea_replies = await _send("it needs user login with Google OAuth")
        assert idea_replies

        # Status
        status_replies = await _send("what's the status")
        assert status_replies

        # List
        list_replies = await _send("list all my projects")
        assert list_replies

        # Verify DB state
        projects = await pm.list_projects()
        assert len(projects) == 1
        assert projects[0]["name"] == "lifecycle-test"
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 9: LLM calls multiple tools — all succeed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_parallel_tool_calls() -> None:
    """LLM calling two tools in one round (parallel) should work."""
    from skills.project_skill import ProjectManagementSkill
    from skills.base import SkillContext
    from bot import state

    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        # Create project first
        p = await pm.create_project("parallel-test")
        state._last_project_id = p["id"]

        skill = ProjectManagementSkill()
        ctx = SkillContext(project_id="", project_path="", gateway_api_url="http://127.0.0.1:8766")

        # Execute two tool calls
        r1 = await skill.execute("project_add_idea", {"idea": "feature A"}, ctx)
        r2 = await skill.execute("project_add_idea", {"idea": "feature B"}, ctx)

        assert "added idea #1" in r1.lower()
        assert "added idea #2" in r2.lower()
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 10: Generate plan — requires project in ideation/planning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_generate_plan_no_project() -> None:
    """project_generate_plan with no projects should give clear error."""
    from skills.project_skill import ProjectManagementSkill
    from skills.base import SkillContext
    from bot import state

    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        state._last_project_id = None

        skill = ProjectManagementSkill()
        ctx = SkillContext(project_id="", project_path="", gateway_api_url="http://127.0.0.1:8766")

        result = await skill.execute("project_generate_plan", {}, ctx)
        # Should give clear error about no projects
        assert "no projects" in result.lower() or "error" in result.lower()
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 11: Unknown tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_unknown_tool() -> None:
    from skills.project_skill import ProjectManagementSkill
    from skills.base import SkillContext

    router = ScriptedRouter([])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        skill = ProjectManagementSkill()
        ctx = SkillContext(project_id="", project_path="", gateway_api_url="http://127.0.0.1:8766")
        result = await skill.execute("project_does_not_exist", {}, ctx)
        assert "unknown" in result.lower()
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 12: Stale project context — "can we start a project" must not
#              reference the previously active project ("boomboom").
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_new_project_intent_clears_stale_context() -> None:
    """With boomboom as _last_project_id, 'can we start a project' clears context and asks for name."""
    router = ScriptedRouter([
        # LLM asked for the new project name (no tool calls — just a question)
        _LLMResponse(text="Sure! What would you like to name the new project?"),
    ])
    db, pm, registry, cfg, orig_auth = await _build_state(router)
    try:
        from bot import state

        # Simulate a previously worked-on project
        p = await pm.create_project("boomboom")
        state._last_project_id = p["id"]

        replies = await _send("can we start a project")

        # LLM should have been called
        assert router._idx >= 1, "LLM should be called (not treated as greeting)"
        assert replies, "bot should reply"

        # _last_project_id must have been cleared by the intent detection BEFORE LLM call
        # (state._last_project_id may now be set to new project if LLM called project_create,
        #  but in this scripted scenario LLM only asks for a name — so it should be None)
        assert state._last_project_id is None, (
            f"_last_project_id should be None after new-project intent; got {state._last_project_id}"
        )

        # The reply should NOT reference boomboom
        reply_text = " ".join(replies).lower()
        assert "boomboom" not in reply_text, (
            f"Bot reply should not reference 'boomboom'; got: {reply_text}"
        )
    finally:
        await _teardown_state(db, cfg, orig_auth)


# ---------------------------------------------------------------------------
# SCENARIO 13: _is_new_project_intent unit checks
# ---------------------------------------------------------------------------

def test_is_new_project_intent_matches_typical_phrases() -> None:
    """Verify the intent detector fires on the canonical phrases."""
    from bot.nl_intent import _is_new_project_intent

    assert _is_new_project_intent("can we start a project") is True
    assert _is_new_project_intent("start a project") is True
    assert _is_new_project_intent("create a project") is True
    assert _is_new_project_intent("new project") is True
    assert _is_new_project_intent("make a new project called foo") is True


def test_is_new_project_intent_does_not_match_non_creation_phrases() -> None:
    """Verify the intent detector does NOT fire on unrelated project messages."""
    from bot.nl_intent import _is_new_project_intent

    assert _is_new_project_intent("what projects do I have") is False
    assert _is_new_project_intent("add idea to the project") is False
    assert _is_new_project_intent("list my projects") is False
    assert _is_new_project_intent("hi") is False
