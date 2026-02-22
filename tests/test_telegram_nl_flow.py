"""Telegram bot natural-language flow regressions.

Tests the LLM-first conversation architecture introduced to replace the
old waterfall intent pipeline. Functions under test are the lightweight
helpers that remain in nl_intent.py plus the ProjectManagementSkill tools.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


def _ensure_gateway_path() -> None:
    repo_root = Path(__file__).parent.parent
    gateway_root = str(repo_root / "openclaw-gateway")
    if gateway_root not in sys.path:
        sys.path.insert(0, gateway_root)


# ---------------------------------------------------------------------------
# _is_pure_greeting
# ---------------------------------------------------------------------------

def test_pure_greeting_hi() -> None:
    _ensure_gateway_path()
    from bot.nl_intent import _is_pure_greeting

    assert _is_pure_greeting("hi") is True
    assert _is_pure_greeting("Hello!") is True
    assert _is_pure_greeting("hey there") is True
    assert _is_pure_greeting("good morning") is True


def test_pure_greeting_rejects_substantive_text() -> None:
    _ensure_gateway_path()
    from bot.nl_intent import _is_pure_greeting

    assert _is_pure_greeting("hi, start a project") is False
    assert _is_pure_greeting("hello, what projects do I have?") is False
    assert _is_pure_greeting("build the app") is False
    assert _is_pure_greeting("") is False


def test_pure_greeting_case_insensitive() -> None:
    _ensure_gateway_path()
    from bot.nl_intent import _is_pure_greeting

    assert _is_pure_greeting("HI") is True
    assert _is_pure_greeting("HELLO") is True
    assert _is_pure_greeting("Hey Skynet") is True


# ---------------------------------------------------------------------------
# _resolve_project — no project manager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_project_no_manager_returns_error() -> None:
    _ensure_gateway_path()
    from bot import state
    from bot.nl_intent import _resolve_project

    original = state._project_manager
    try:
        state._project_manager = None
        project, err = await _resolve_project()
        assert project is None
        assert err is not None
        assert "not initialized" in err.lower()
    finally:
        state._project_manager = original


# ---------------------------------------------------------------------------
# ProjectManagementSkill — basic tool interface
# ---------------------------------------------------------------------------

def _make_context():
    """Build a minimal SkillContext for testing."""
    _ensure_gateway_path()
    from skills.base import SkillContext
    return SkillContext(
        project_id="",
        project_path="",
        gateway_api_url="http://127.0.0.1:8766",
    )


@pytest.mark.asyncio
async def test_project_skill_no_manager_returns_error() -> None:
    _ensure_gateway_path()
    from skills.project_skill import ProjectManagementSkill
    from bot import state

    skill = ProjectManagementSkill()
    ctx = _make_context()

    original = state._project_manager
    try:
        state._project_manager = None
        result = await skill.execute("project_create", {"name": "test"}, ctx)
        assert "error" in result.lower() or "manager" in result.lower()
    finally:
        state._project_manager = original


@pytest.mark.asyncio
async def test_project_skill_create_empty_name_returns_error() -> None:
    _ensure_gateway_path()
    from skills.project_skill import ProjectManagementSkill
    from bot import state

    skill = ProjectManagementSkill()
    ctx = _make_context()

    class _DummyManager:
        async def create_project(self, name: str):
            return {"id": "x", "name": name, "status": "ideation", "bootstrap_ok": True, "bootstrap_summary": ""}

    original = state._project_manager
    try:
        state._project_manager = _DummyManager()
        result = await skill.execute("project_create", {"name": ""}, ctx)
        assert "required" in result.lower() or "name" in result.lower() or "error" in result.lower()
    finally:
        state._project_manager = original


@pytest.mark.asyncio
async def test_project_skill_list_with_empty_project_list() -> None:
    _ensure_gateway_path()
    from skills.project_skill import ProjectManagementSkill
    from bot import state

    skill = ProjectManagementSkill()
    ctx = _make_context()

    class _DummyManager:
        async def list_projects(self):
            return []

    original = state._project_manager
    try:
        state._project_manager = _DummyManager()
        result = await skill.execute("project_list", {}, ctx)
        assert "no projects" in result.lower()
    finally:
        state._project_manager = original


@pytest.mark.asyncio
async def test_project_skill_create_calls_manager() -> None:
    _ensure_gateway_path()
    from skills.project_skill import ProjectManagementSkill
    from bot import state

    skill = ProjectManagementSkill()
    ctx = _make_context()
    created_names: list[str] = []

    class _DummyManager:
        async def create_project(self, name: str):
            created_names.append(name)
            return {
                "id": "proj-123",
                "name": name,
                "display_name": name,
                "status": "ideation",
                "bootstrap_ok": True,
                "bootstrap_summary": "ok",
                "local_path": "/projects/TestBot",
            }

    original = state._project_manager
    try:
        state._project_manager = _DummyManager()
        result = await skill.execute("project_create", {"name": "TestBot"}, ctx)
        assert "TestBot" in result
        assert created_names == ["TestBot"]
    finally:
        state._project_manager = original


@pytest.mark.asyncio
async def test_project_skill_add_idea_no_active_project() -> None:
    _ensure_gateway_path()
    from skills.project_skill import ProjectManagementSkill
    from bot import state

    skill = ProjectManagementSkill()
    ctx = _make_context()

    class _DummyManager:
        async def add_idea(self, project_id: str, idea: str) -> int:
            return 1

    original_pm = state._project_manager
    original_pid = state._last_project_id
    try:
        state._project_manager = _DummyManager()
        state._last_project_id = None
        result = await skill.execute("project_add_idea", {"idea": "build a thing"}, ctx)
        # No active project → should report an error
        assert "no active project" in result.lower() or "error" in result.lower()
    finally:
        state._project_manager = original_pm
        state._last_project_id = original_pid
