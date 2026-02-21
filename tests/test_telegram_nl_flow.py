"""Telegram natural-language intent parsing regressions."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

def _ensure_gateway_path() -> None:
    repo_root = Path(__file__).parent.parent
    gateway_root = str(repo_root / "openclaw-gateway")
    if gateway_root not in sys.path:
        sys.path.insert(0, gateway_root)


def test_create_project_prompt_then_name_parsing() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    intent = bot._extract_nl_intent("can we start a project")
    assert intent.get("intent") == "create_project"
    assert "project_name" not in intent

    assert bot._extract_project_name_candidate("'boom baby'") == "boom baby"


def test_do_project_phrase_maps_to_create_project() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    intent = bot._extract_nl_intent("can we do a project")
    assert intent.get("intent") == "create_project"
    assert "project_name" not in intent


def test_pending_name_candidate_from_longer_sentence() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    text = "python app. - 'kundan bhai' which when clicked makes a 1 sec beep"
    assert bot._extract_project_name_candidate(text) == "kundan bhai"


def test_pending_name_candidate_from_descriptive_unquoted_sentence() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    text = "python app -kundi curry which when clicked give a 1 sec beep"
    assert bot._extract_project_name_candidate(text) == "kundi curry"


def test_start_the_project_not_misclassified_as_new_project() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    intent = bot._extract_nl_intent(
        "its a python project. start the project and build it. "
        "a small app when clicked gives a 1 sec beep sound."
    )
    assert intent.get("intent") == "approve_and_start"


def test_followup_implementation_text_not_classified_as_create_project() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    intent = bot._extract_nl_intent("make it a python app when clicked will produce 1 sec beep")
    assert intent.get("intent") != "create_project"


def test_build_project_typo_maps_to_approve_and_start() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    intent = bot._extract_nl_intent("build prpjetc")
    assert intent.get("intent") == "approve_and_start"


def test_same_project_phrase_not_treated_as_project_name() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    assert bot._extract_project_name_candidate("the same project") == ""


@pytest.mark.asyncio
async def test_hybrid_intent_prefers_llm_result() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    async def _fake_llm(_: str) -> dict[str, str]:
        return {"intent": "list_projects"}

    bot._extract_nl_intent_llm = _fake_llm
    out = await bot._extract_nl_intent_hybrid("can we do a project")
    assert out.get("intent") == "list_projects"


@pytest.mark.asyncio
async def test_hybrid_intent_falls_back_to_rules() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    async def _fake_llm(_: str) -> dict[str, str]:
        return {}

    bot._extract_nl_intent_llm = _fake_llm
    out = await bot._extract_nl_intent_hybrid("can we do a project")
    assert out.get("intent") == "create_project"


@pytest.mark.asyncio
async def test_pending_name_does_not_hijack_generate_plan_intent() -> None:
    _ensure_gateway_path()
    from bot import nl_intent as bot

    class _DummyUser:
        id = 123

    class _DummyMessage:
        async def reply_text(self, *_args, **_kwargs):
            return None

    class _DummyUpdate:
        effective_user = _DummyUser()
        message = _DummyMessage()

    from bot import state as bot_state

    user_id = int(_DummyUpdate.effective_user.id)
    bot_state._pending_project_name_requests[user_id] = {"expected": "project_name"}

    async def _fake_hybrid(_text: str, update=None) -> dict[str, str]:
        return {"intent": "generate_plan"}

    bot._extract_nl_intent_hybrid = _fake_hybrid
    handled = await bot._maybe_handle_pending_project_name(_DummyUpdate(), "generate plan")

    # pending-name gate should release and allow normal handler to process generate_plan.
    assert handled is False
    assert user_id not in bot_state._pending_project_name_requests
