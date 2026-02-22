"""Formatting/sanitization for project documentation intake."""

from __future__ import annotations

from pathlib import Path
import sys


def _ensure_gateway_path() -> None:
    repo_root = Path(__file__).parent.parent
    gateway_root = str(repo_root / "openclaw-gateway")
    if gateway_root not in sys.path:
        sys.path.insert(0, gateway_root)


def test_project_doc_intake_sanitizes_and_formats_natural_language() -> None:
    _ensure_gateway_path()
    from bot import doc_intake as bot

    answers = {
        "problem": "# users need quick test beep\x00\n\ncreate tiny utility",
        "users": "developers, qa engineers; students",
        "requirements": "- play 1 sec beep\npackage as exe, tiny ui",
        "non_goals": "cloud sync, user accounts",
        "success_metrics": "beep starts <1s; works offline",
        "tech_stack": "python 3.12, tkinter",
    }

    prd, overview, features = bot._format_initial_docs_from_answers("Pennu Pidi", answers)

    # Sanitization
    assert "\x00" not in prd
    assert "```" not in prd
    assert "\n\n\n" not in prd

    # Structured formatting from natural language
    assert "## Users\n- Developers\n- Qa engineers\n- Students" in prd
    assert "- [ ] Play 1 sec beep" in prd
    assert "- [ ] Package as exe" in prd
    assert "- [ ] Tiny ui" in prd
    assert "## Non-Goals\n- Cloud sync\n- User accounts" in prd
    assert "## Success Metrics\n- Beep starts <1s\n- Works offline" in prd

    # Companion docs should also be list-formatted
    assert "Primary users:" in overview
    assert "- Developers" in overview
    assert "- [ ] Play 1 sec beep" in features


def test_has_minimum_doc_context_requires_problem_and_requirements() -> None:
    _ensure_gateway_path()
    from bot import doc_intake as bot

    # Missing problem → not enough
    assert not bot._has_minimum_doc_context({"requirements": "play a beep", "users": "devs"})
    # Missing requirements → not enough
    assert not bot._has_minimum_doc_context({"problem": "need a beep", "users": "devs"})
    # problem + requirements + at least one more field → enough
    assert bot._has_minimum_doc_context({
        "problem": "need a beep app",
        "requirements": "click to beep",
        "tech_stack": "python",
    })
    assert bot._has_minimum_doc_context({
        "problem": "need a beep app",
        "requirements": "click to beep",
        "users": "developers",
    })
