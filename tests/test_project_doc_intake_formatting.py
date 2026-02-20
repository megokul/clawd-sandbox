"""Formatting/sanitization for project documentation intake."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import sys


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


def test_project_doc_intake_sanitizes_and_formats_natural_language() -> None:
    repo_root = Path(__file__).parent.parent
    bot_path = repo_root / "openclaw-gateway" / "telegram_bot.py"
    bot = _load_module(bot_path, "oc_gateway_telegram_bot_doc_intake")

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
