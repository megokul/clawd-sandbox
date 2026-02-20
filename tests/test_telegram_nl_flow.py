"""Telegram natural-language intent parsing regressions."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

import pytest

def _load_module(path: Path, module_name: str):
    gateway_root = str(path.parent)
    if gateway_root not in sys.path:
        sys.path.insert(0, gateway_root)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_create_project_prompt_then_name_parsing() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl")

    intent = bot._extract_nl_intent("can we start a project")
    assert intent.get("intent") == "create_project"
    assert "project_name" not in intent

    assert bot._extract_project_name_candidate("'boom baby'") == "boom baby"


def test_do_project_phrase_maps_to_create_project() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl_3")

    intent = bot._extract_nl_intent("can we do a project")
    assert intent.get("intent") == "create_project"
    assert "project_name" not in intent


def test_pending_name_candidate_from_longer_sentence() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl_4")

    text = "python app. - 'kundan bhai' which when clicked makes a 1 sec beep"
    assert bot._extract_project_name_candidate(text) == "kundan bhai"


def test_start_the_project_not_misclassified_as_new_project() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl_2")

    intent = bot._extract_nl_intent(
        "its a python project. start the project and build it. "
        "a small app when clicked gives a 1 sec beep sound."
    )
    assert intent.get("intent") == "approve_and_start"


@pytest.mark.asyncio
async def test_hybrid_intent_prefers_llm_result() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl_5")

    async def _fake_llm(_: str) -> dict[str, str]:
        return {"intent": "list_projects"}

    bot._extract_nl_intent_llm = _fake_llm
    out = await bot._extract_nl_intent_hybrid("can we do a project")
    assert out.get("intent") == "list_projects"


@pytest.mark.asyncio
async def test_hybrid_intent_falls_back_to_rules() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_nl_6")

    async def _fake_llm(_: str) -> dict[str, str]:
        return {}

    bot._extract_nl_intent_llm = _fake_llm
    out = await bot._extract_nl_intent_hybrid("can we do a project")
    assert out.get("intent") == "create_project"
