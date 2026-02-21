"""
bot/doc_intake.py -- Project documentation intake pipeline.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from telegram import Update

import bot_config as cfg
from . import state
from .helpers import (
    _action_result_ok,
    _extract_json_object,
    _has_pending_project_route_for_user,
    _is_explicit_new_project_request,
    _is_smalltalk_or_ack,
    _join_project_path,
    _notify_styled,
    _project_display,
    _send_action,
    _send_to_user,
    _spawn_background_task,
)

logger = logging.getLogger("skynet.telegram")


def _doc_intake_key(update: Update) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    return int(user.id)

def _sanitize_intake_text(value: str, *, max_chars: int = 1200) -> str:
    text = (value or "").replace("\r", "\n")
    # Strip non-printable control chars but preserve newlines/tabs.
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = text.strip().strip("`").strip()
    text = text.replace("```", "''")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _sanitize_markdown_paragraph(value: str, *, max_chars: int = 1200) -> str:
    text = _sanitize_intake_text(value, max_chars=max_chars)
    if not text:
        return "TBD"
    # Avoid accidental markdown headings from user input.
    lines = [re.sub(r"^\s*#+\s*", "", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "TBD"
    out = "\n".join(lines)
    return out if out else "TBD"


def _normalize_list_item(value: str) -> str:
    item = re.sub(r"^\s*[-*•]+\s*", "", value or "").strip()
    item = re.sub(r"\s+", " ", item).strip(" .;,-")
    if not item:
        return ""
    if len(item) > 220:
        item = item[:220].rstrip()
    if item and item[0].isalpha():
        item = item[0].upper() + item[1:]
    return item


def _parse_natural_list(value: str, *, max_items: int = 12, max_chars: int = 1500) -> list[str]:
    text = _sanitize_intake_text(value, max_chars=max_chars)
    if not text:
        return []

    raw_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    items: list[str] = []
    for line in raw_lines:
        if re.match(r"^\s*[-*•]", line):
            norm = _normalize_list_item(line)
            if norm:
                items.append(norm)
            continue

        # Natural language often comes as comma/semicolon-separated phrases.
        parts = [p for p in re.split(r"\s*[;,]\s*", line) if p.strip()]
        if len(parts) == 1:
            parts = [line]
        for p in parts:
            norm = _normalize_list_item(p)
            if norm:
                items.append(norm)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def _to_checklist(items: list[str], *, fallback: list[str]) -> list[str]:
    src = items if items else fallback
    return [f"- [ ] {i}" for i in src]


def _to_bullets(items: list[str], *, fallback: str = "TBD") -> list[str]:
    if not items:
        return [f"- {fallback}"]
    return [f"- {i}" for i in items]

def _format_initial_docs_from_answers(project_name: str, answers: dict[str, str]) -> tuple[str, str, str]:
    problem = _sanitize_markdown_paragraph(
        str(answers.get("problem", "")),
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["problem"],
    )
    users_list = _parse_natural_list(
        str(answers.get("users", "")),
        max_items=8,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["users"],
    )
    requirements = _parse_natural_list(
        str(answers.get("requirements", "")),
        max_items=20,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["requirements"],
    )
    non_goals_list = _parse_natural_list(
        str(answers.get("non_goals", "")),
        max_items=12,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["non_goals"],
    )
    metrics_list = _parse_natural_list(
        str(answers.get("success_metrics", "")),
        max_items=12,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["success_metrics"],
    )
    tech_list = _parse_natural_list(
        str(answers.get("tech_stack", "")),
        max_items=12,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["tech_stack"],
    )

    req_lines = _to_checklist(
        requirements,
        fallback=["Define core user flow", "Define MVP scope"],
    )
    users_lines = _to_bullets(users_list)
    non_goal_lines = _to_bullets(non_goals_list)
    metric_lines = _to_bullets(metrics_list)
    tech_lines = _to_bullets(tech_list)

    prd = (
        "# Product Requirements Document (PRD)\n\n"
        f"## Problem\n{problem}\n\n"
        f"## Users\n{chr(10).join(users_lines)}\n\n"
        f"## Requirements\n{chr(10).join(req_lines)}\n\n"
        f"## Non-Goals\n{chr(10).join(non_goal_lines)}\n\n"
        f"## Success Metrics\n{chr(10).join(metric_lines)}\n\n"
        f"## Technical Constraints\n{chr(10).join(tech_lines)}\n"
    )

    overview = (
        "# Product Overview\n\n"
        f"{project_name} aims to solve:\n\n"
        f"- {problem}\n\n"
        "Primary users:\n\n"
        + "\n".join(users_lines)
        + "\n"
    )

    features = "# Features\n\n" + "\n".join(req_lines) + "\n"
    return prd, overview, features


def _intake_answers_to_idea_text(project_name: str, answers: dict[str, str]) -> str:
    lines = [f"Initial documentation intake for {project_name}:"]
    for key, _question in state._PROJECT_DOC_INTAKE_STEPS:
        val = _sanitize_intake_text(
            str(answers.get(key, "")),
            max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(key, 1200),
        )
        if val:
            lines.append(f"- {key}: {val}")
    return "\n".join(lines)


def _intake_has_any_content(answers: dict[str, str]) -> bool:
    for key, _question in state._PROJECT_DOC_INTAKE_STEPS:
        value = _sanitize_intake_text(
            str(answers.get(key, "")),
            max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(key, 1200),
        )
        if value:
            return True
    return False


def _doc_intake_opt_out_requested(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False

    if lowered in {"skip", "skip docs", "cancel docs", "stop docs", "later"}:
        return True

    if re.search(
        r"\b(?:no\s*need|noneed|don't\s*need|dont\s*need|do\s*not\s*need|skip|stop|cancel|avoid)\b"
        r".{0,40}\b(?:docs?|documentation|prd|write[- ]?up|writeup)\b",
        lowered,
    ):
        return True

    if re.search(
        r"\b(?:docs?|documentation|prd)\b.{0,40}\b(?:not\s*needed|not\s*required|unnecessary|skip|later)\b",
        lowered,
    ):
        return True

    if re.search(
        r"\bno\b.{0,24}\b(?:docs?|documentation|documen\w*|prd|write[- ]?up|writeup)\b"
        r".{0,24}\b(?:needed|required)\b",
        lowered,
    ):
        return True

    if re.search(
        r"\b(?:without|avoid)\b.{0,30}\b(?:docs?|documentation|documen\w*|prd)\b",
        lowered,
    ):
        return True

    if re.search(
        r"\b(?:just|only)\b.{0,30}\b(?:build|make|create|implement)\b.{0,80}\b(?:app|project|application)\b",
        lowered,
    ) and re.search(
        r"\b(?:no|without|dont|don't|do\s*not)\b.{0,30}\b(?:docs?|documentation|documen\w*|prd)\b",
        lowered,
    ):
        return True

    if re.search(
        r"\b(?:simple|basic|tiny)\s+(?:app|project)\b",
        lowered,
    ) and re.search(
        r"\b(?:no\s*need|noneed|don't|dont|do\s*not|without)\b.{0,40}\b(?:docs?|documentation|prd)\b",
        lowered,
    ):
        return True

    return False


async def _doc_intake_opt_out_requested_semantic(
    text: str,
    project_name: str,
    answers: dict[str, str],
) -> bool:
    """
    LLM-based intent check: does the user want to stop/skip documentation intake?
    """
    if state._provider_router is None:
        return False

    raw = (text or "").strip()
    if not raw:
        return False

    payload = {
        "message": raw,
        "project_name": project_name,
        "current_answers": answers,
        "task": "Classify whether the user intends to stop/skip/minimize documentation intake.",
    }
    system = (
        "You classify a user's intent in a project-intake chat.\n"
        "Return ONLY JSON: {\"opt_out\": true|false, \"confidence\": 0..1}.\n"
        "Set opt_out=true when user meaning implies: no docs, minimal docs, stop asking doc questions, "
        "or focus only on building now.\n"
        "Do not require exact words.\n"
    )
    try:
        response = await state._provider_router.chat(
            [{"role": "user", "content": json.dumps(payload)}],
            system=system,
            max_tokens=120,
            task_type="general",
            preferred_provider="groq",
            allowed_providers=state._CHAT_PROVIDER_ALLOWLIST,
        )
    except Exception:
        return False

    obj = _extract_json_object(response.text or "")
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("opt_out", False))

async def _capture_minimal_intake_idea_snapshot(intake_state: dict[str, Any], note: str = "") -> None:
    if state._project_manager is None:
        return
    project_id = str(intake_state.get("project_id", "")).strip()
    if not project_id:
        return
    answers = dict(intake_state.get("answers") or {})
    if not _intake_has_any_content(answers):
        return

    try:
        from db import store

        project = await store.get_project(state._project_manager.db, project_id)
        if not project:
            return
        idea_text = _intake_answers_to_idea_text(_project_display(project), answers)
        note_clean = _sanitize_intake_text(note, max_chars=300)
        if note_clean:
            idea_text += f"\n- intake_note: {note_clean}"
        await state._project_manager.add_idea(project_id, idea_text)
    except Exception:
        logger.exception("Failed capturing minimal intake snapshot.")


def _sanitize_markdown_document(value: str, *, max_chars: int = 50000) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    if not text:
        return ""
    if not text.endswith("\n"):
        text += "\n"
    return text


def _merge_intake_value(existing: str, new_value: str, *, max_chars: int = 2000) -> str:
    old = _sanitize_intake_text(existing, max_chars=max_chars)
    new = _sanitize_intake_text(new_value, max_chars=max_chars)
    if not new:
        return old
    if not old:
        return new

    old_parts = [p.strip() for p in re.split(r"\n+", old) if p.strip()]
    key_set = {re.sub(r"\s+", " ", p.lower()) for p in old_parts}
    for part in [p.strip() for p in re.split(r"\n+", new) if p.strip()]:
        key = re.sub(r"\s+", " ", part.lower())
        if key and key not in key_set:
            old_parts.append(part)
            key_set.add(key)
    merged = "\n".join(old_parts).strip()
    if len(merged) > max_chars:
        merged = merged[:max_chars].rstrip()
    return merged



def _heuristic_intake_extract(text: str) -> dict[str, str]:
    raw = (text or "").strip()
    lowered = raw.lower()
    out: dict[str, str] = {}

    tech_hits: list[str] = []
    tech_terms = (
        "python", "fastapi", "flask", "django", "streamlit", "tkinter",
        "react", "node", "sqlite", "postgres", "docker", "windows", "linux",
        "telegram", "desktop app", "web app",
    )
    for term in tech_terms:
        if term in lowered:
            tech_hits.append(term)
    if tech_hits:
        out["tech_stack"] = ", ".join(dict.fromkeys(tech_hits))

    if re.search(r"\b(will|should|must|when|on click|upon click|clicked|display|popup|pop up|beep|sound)\b", lowered):
        out["requirements"] = raw

    user_match = re.search(r"\b(?:for|used by|users are|target users are)\s+(.+)$", raw, flags=re.IGNORECASE)
    if user_match and len(user_match.group(1).strip()) >= 3:
        out["users"] = user_match.group(1).strip()

    if re.search(r"\b(problem|pain|issue|need|goal is|objective is|so that)\b", lowered):
        out["problem"] = raw

    if re.search(r"\b(out of scope|non-goal|won't|will not|not doing|exclude)\b", lowered):
        out["non_goals"] = raw

    if re.search(r"\b(success|done when|measure|metric|acceptance)\b", lowered):
        out["success_metrics"] = raw

    return out


async def _llm_intake_extract(
    project_name: str,
    text: str,
    current_answers: dict[str, str],
) -> dict[str, str]:
    if state._provider_router is None:
        return {}
    payload = {
        "project_name": project_name,
        "message": text,
        "current_answers": current_answers,
        "fields": list(state._DOC_INTAKE_FIELDS),
        "instruction": (
            "Extract all relevant fields from this single message. "
            "If a field is not present, return empty string. "
            "Do not invent facts."
        ),
    }
    system = (
        "You extract structured project documentation signals from a natural language message. "
        "Return ONLY JSON object with keys: "
        "problem, users, requirements, non_goals, success_metrics, tech_stack. "
        "Values must be short plain text strings."
    )
    try:
        response = await state._provider_router.chat(
            [{"role": "user", "content": json.dumps(payload)}],
            system=system,
            max_tokens=450,
            task_type="planning",
            preferred_provider="groq",
            allowed_providers=state._CHAT_PROVIDER_ALLOWLIST,
        )
    except Exception:
        return {}
    obj = _extract_json_object(response.text or "")
    if not isinstance(obj, dict):
        return {}
    out: dict[str, str] = {}
    for field in state._DOC_INTAKE_FIELDS:
        value = _sanitize_intake_text(str(obj.get(field, "")), max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(field, 1200))
        if value:
            out[field] = value
    return out


async def _extract_intake_signals(
    project_name: str,
    text: str,
    current_answers: dict[str, str],
) -> dict[str, str]:
    signals = _heuristic_intake_extract(text)
    llm_signals = await _llm_intake_extract(project_name, text, current_answers)
    for field in state._DOC_INTAKE_FIELDS:
        cur = signals.get(field, "")
        nxt = llm_signals.get(field, "")
        if nxt:
            signals[field] = _merge_intake_value(cur, nxt, max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(field, 1200))
    return signals


def _missing_intake_fields(answers: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for field in state._DOC_INTAKE_FIELDS:
        if not _sanitize_intake_text(str(answers.get(field, "")), max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(field, 1200)):
            missing.append(field)
    return missing


def _doc_intake_done_signal(text: str) -> bool:
    lowered = (text or "").strip().lower()
    done_phrases = {
        "done", "thats all", "that's all", "enough", "proceed", "continue", "go ahead",
        "start building", "build it", "generate docs", "finalize docs", "looks good", "that's enough",
    }
    return lowered in done_phrases or any(phrase in lowered for phrase in done_phrases)


def _intake_has_enough_context(answers: dict[str, str], turn_count: int, done_signal: bool) -> bool:
    filled = len(state._DOC_INTAKE_FIELDS) - len(_missing_intake_fields(answers))
    min_ctx = _has_minimum_doc_context(answers)
    if done_signal and min_ctx:
        return True
    if min_ctx and filled >= 5 and turn_count >= 2:
        return True
    if min_ctx and filled >= 4 and turn_count >= 3:
        return True
    return False


def _compose_dynamic_intake_followup(project_name: str, answers: dict[str, str], turn_count: int) -> str:
    missing = _missing_intake_fields(answers)
    if not missing:
        return "I have enough context. I will finalize the documentation now."

    next_field = missing[0]
    requirements_known = bool(_sanitize_intake_text(str(answers.get("requirements", ""))))
    tech_known = bool(_sanitize_intake_text(str(answers.get("tech_stack", ""))))

    question_map: dict[str, list[str]] = {
        "problem": [
            f"What core problem should '{project_name}' solve for users?",
            "What should improve for users after using this app?",
        ],
        "users": [
            "Who will actually use this first: only you, a team, or public users?",
            "Who is the primary user persona for this first version?",
        ],
        "requirements": [
            "What exact behavior should the first version implement end-to-end?",
            "What should happen step-by-step in the user flow?",
        ],
        "non_goals": [
            "What should we explicitly avoid in v1 so scope stays tight?",
            "Anything you do not want included in this first release?",
        ],
        "success_metrics": [
            "How should we measure that v1 is successful?",
            "What acceptance criteria should mark this as done?",
        ],
        "tech_stack": [
            "Any constraints on language/framework/runtime or packaging?",
            "Do you want to lock a specific stack, or should I choose a pragmatic default?",
        ],
    }

    if next_field == "users" and requirements_known:
        prompt = "I captured the feature direction. "
    elif next_field == "tech_stack" and not tech_known and requirements_known:
        prompt = "Feature scope is clear. "
    else:
        prompt = ""

    options = question_map.get(next_field, ["Tell me one more key detail about the project."])
    followup = options[turn_count % len(options)]
    return f"{prompt}{followup}"

def _normalize_doc_relpath(path: str) -> str:
    return re.sub(r"/{2,}", "/", (path or "").strip().replace("\\", "/")).strip("/")



def _load_finalized_template_files() -> dict[str, str]:
    root = state._FINALIZED_TEMPLATE_PATH
    if not root.exists() or not root.is_dir():
        return {}

    out: dict[str, str] = {}
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel = _normalize_doc_relpath(str(item.relative_to(root)))
        if not rel:
            continue
        try:
            out[rel] = item.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed loading template file: %s", item)
    return out



def _render_project_yaml(project: dict) -> str:
    def _yaml_quote(value: str) -> str:
        return (value or "").replace("\\", "\\\\").replace("\"", "\\\"")

    project_id = str(project.get("id", "")).strip()
    project_name = _project_display(project)
    description = _sanitize_markdown_paragraph(str(project.get("description", "")), max_chars=1500)
    created_at = str(project.get("created_at", "")).strip()
    return (
        "project:\n"
        f"  id: \"{_yaml_quote(project_id)}\"\n"
        f"  name: \"{_yaml_quote(project_name)}\"\n"
        f"  description: \"{_yaml_quote(description)}\"\n"
        f"  created_at: \"{_yaml_quote(created_at)}\"\n"
        "  created_by: \"skynet\"\n"
        "\n"
        "execution:\n"
        "  scheduler_enabled: true\n"
        "  parallel_execution: true\n"
        "  control_plane_managed: true\n"
        "\n"
        "paths:\n"
        "  docs_dir: docs/\n"
        "  planning_dir: planning/\n"
        "  control_dir: control/\n"
        "  source_dir: src/\n"
        "  tests_dir: tests/\n"
        "  infra_dir: infra/\n"
        "\n"
        "control_plane:\n"
        "  task_queue_table: control_tasks\n"
        "  file_registry_table: control_task_file_ownership\n"
    )


def _render_project_state_yaml() -> str:
    return (
        "state:\n"
        "  phase: planning\n"
        "  total_tasks: 0\n"
        "  completed_tasks: 0\n"
        "  active_tasks: 0\n"
        "  failed_tasks: 0\n"
        "  progress_percentage: 0\n"
        "  last_updated: \"\"\n"
    )


def _has_minimum_doc_context(answers: dict[str, str]) -> bool:
    problem = _sanitize_intake_text(str(answers.get("problem", "")))
    requirements = _sanitize_intake_text(str(answers.get("requirements", "")))
    users = _sanitize_intake_text(str(answers.get("users", "")))
    success = _sanitize_intake_text(str(answers.get("success_metrics", "")))
    tech = _sanitize_intake_text(str(answers.get("tech_stack", "")))
    if not problem or not requirements:
        return False
    return bool(users or success or tech)


def _build_baseline_doc_pack(project_name: str, answers: dict[str, str]) -> dict[str, str]:
    prd, overview, features = _format_initial_docs_from_answers(project_name, answers)
    problem = _sanitize_markdown_paragraph(
        str(answers.get("problem", "")),
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["problem"],
    )
    users = _parse_natural_list(
        str(answers.get("users", "")),
        max_items=8,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["users"],
    )
    requirements = _parse_natural_list(
        str(answers.get("requirements", "")),
        max_items=20,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["requirements"],
    )
    tech = _parse_natural_list(
        str(answers.get("tech_stack", "")),
        max_items=12,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["tech_stack"],
    )
    non_goals = _parse_natural_list(
        str(answers.get("non_goals", "")),
        max_items=10,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["non_goals"],
    )
    metrics = _parse_natural_list(
        str(answers.get("success_metrics", "")),
        max_items=10,
        max_chars=state._DOC_INTAKE_FIELD_LIMITS["success_metrics"],
    )
    user_lines = _to_bullets(users, fallback="TBD")
    req_lines = _to_checklist(requirements, fallback=["TBD"])
    tech_lines = _to_bullets(tech, fallback="TBD")
    non_goal_lines = _to_bullets(non_goals, fallback="TBD")
    metric_lines = _to_bullets(metrics, fallback="TBD")

    docs: dict[str, str] = {
        "docs/product/PRD.md": prd,
        "docs/product/overview.md": overview,
        "docs/product/features.md": features,
        "docs/architecture/overview.md": (
            "# Architecture Overview (Current State)\n\n"
            f"## Project\n{project_name}\n\n"
            "## Problem Context\n"
            f"- {problem}\n\n"
            "## Major Components (Project-Specific)\n"
            "- UI / user interaction layer\n"
            "- Application logic layer\n"
            "- Data/config storage layer (if applicable)\n"
        ),
        "docs/architecture/system-design.md": (
            "# System Design (Current State)\n\n"
            "## Components\n"
            "- Client/entrypoint\n"
            "- Core service/module\n"
            "- Supporting utilities and storage\n\n"
            "## Technical Direction\n"
            + "\n".join(tech_lines)
            + "\n"
        ),
        "docs/architecture/data-flow.md": (
            "# Data Flow (Current State)\n\n"
            "1. User triggers action.\n"
            "2. Input is validated and mapped to domain behavior.\n"
            "3. Core logic executes and returns output.\n"
            "4. Results are displayed or persisted.\n"
        ),
        "docs/runbooks/local-dev.md": (
            "# Runbook: Local Development\n\n"
            "## Prerequisites\n"
            + "\n".join(tech_lines)
            + "\n\n## Steps\n1. Setup environment\n2. Run app locally\n3. Verify expected behavior\n"
        ),
        "docs/runbooks/deploy.md": (
            "# Runbook: Deploy\n\n"
            "Define deploy packaging, runtime target, and verification checklist for this project.\n"
        ),
        "docs/runbooks/recovery.md": (
            "# Runbook: Recovery\n\n"
            "Document failure modes, rollback strategy, and quick recovery steps specific to this project.\n"
        ),
        "docs/guides/getting-started.md": (
            "# Getting Started\n\n"
            f"## Project\n{project_name}\n\n"
            "## Target Users\n"
            + "\n".join(user_lines)
            + "\n\n## First Scope\n"
            + "\n".join(req_lines[:6])
            + "\n"
        ),
        "docs/guides/configuration.md": (
            "# Configuration\n\n"
            "## Runtime and Tooling\n"
            + "\n".join(tech_lines)
            + "\n\n## Constraints\n"
            + "\n".join(non_goal_lines)
            + "\n"
        ),
        "docs/decisions/ADR-001-tech-stack.md": (
            "# ADR-001: Initial Technical Stack\n\n"
            "## Status\nAccepted\n\n"
            "## Context\n"
            f"{problem}\n\n"
            "## Decision\n"
            + "\n".join(tech_lines)
            + "\n\n## Consequences\n"
            + "\n".join(metric_lines)
            + "\n"
        ),
        "planning/task_plan.md": (
            "STATUS: DRAFT\n\n"
            "# Project Plan\n\n"
            "## Goal\n"
            f"{problem}\n\n"
            "## Milestones (Initial)\n\n"
            "### TASK-001: Lock requirements and acceptance criteria\n"
            "Dependencies:\n"
            "Outputs:\n"
            "  - docs/product/PRD.md\n\n"
            "### TASK-002: Implement MVP behavior\n"
            "Dependencies: TASK-001\n"
            "Outputs:\n"
            "  - src/\n"
            "  - tests/\n\n"
            "### TASK-003: Validation and documentation hardening\n"
            "Dependencies: TASK-002\n"
            "Outputs:\n"
            "  - docs/runbooks/local-dev.md\n"
            "  - planning/progress.md\n"
        ),
        "planning/progress.md": (
            "Project Progress: 0%\n\n"
            "Completed:\n\n"
            "In Progress:\n\n"
            "Pending:\n"
            "- TASK-001: Lock requirements and acceptance criteria\n"
            "- TASK-002: Implement MVP behavior\n"
            "- TASK-003: Validation and documentation hardening\n\n"
            "Success Metrics (Draft):\n"
            + "\n".join(metric_lines)
            + "\n"
        ),
        "planning/findings.md": (
            "# Findings\n\n"
            "Track assumptions, risks, validation evidence, and corrections specific to this project.\n"
        ),
    }
    return {k: _sanitize_markdown_document(v) for k, v in docs.items()}

def _sanitize_generated_doc_pack(payload: dict) -> dict[str, str]:
    docs = payload.get("documents") if isinstance(payload.get("documents"), dict) else payload
    if not isinstance(docs, dict):
        return {}
    out: dict[str, str] = {}
    for raw_path, raw_body in docs.items():
        if not isinstance(raw_path, str) or not isinstance(raw_body, str):
            continue
        rel = _normalize_doc_relpath(raw_path)
        if rel not in state._DOC_LLM_TARGET_PATHS:
            continue
        body = _sanitize_markdown_document(raw_body)
        if len(body) < 80:
            continue
        out[rel] = body
    return out

async def _generate_detailed_doc_pack_with_llm(
    project: dict,
    answers: dict[str, str],
    baseline_docs: dict[str, str],
    *,
    review_pass: bool = False,
) -> tuple[dict[str, str], str]:
    if state._provider_router is None:
        return {}, "provider router unavailable"

    intake = {
        field: _sanitize_intake_text(
            str(answers.get(field, "")),
            max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(field, 1200),
        )
        for field, _ in state._PROJECT_DOC_INTAKE_STEPS
    }
    baseline_excerpt = {
        k: v[:2000]
        for k, v in baseline_docs.items()
    }

    mode = "review_and_refine" if review_pass else "generate"
    system = (
        "You are a principal software architect and technical writer. "
        "Produce project-specific documentation only. "
        "Do NOT mention SKYNET, OpenClaw, control-plane internals, or platform details "
        "unless the user explicitly asked for them in this specific project. "
        "Use explicit assumptions where needed, but keep them tied to this project scope. "
        "Return ONLY valid JSON with shape: "
        "{\"documents\": {\"<relative/path>.md\": \"<markdown>\"}}. "
        "Do not include keys outside required paths."
    )
    user_payload = {
        "project_name": _project_display(project),
        "project_id": str(project.get("id", "")),
        "project_path": str(project.get("local_path", "")),
        "required_document_paths": list(state._DOC_LLM_TARGET_PATHS),
        "user_intake": intake,
        "baseline_documents_excerpt": baseline_excerpt,
        "mode": mode,
        "quality_bar": [
            "Detailed sections with assumptions, constraints, risks, and acceptance criteria",
            "Concrete, technically consistent architecture and data flow for this project",
            "Actionable runbooks and configuration guidance for this project context",
            "Remove irrelevant platform/vendor references if unrelated to project requirements",
            "Do not just restate user text; synthesize missing details responsibly",
        ],
    }
    try:
        response = await state._provider_router.chat(
            [{"role": "user", "content": json.dumps(user_payload)}],
            system=system,
            max_tokens=8000,
            task_type="planning",
            preferred_provider="groq",
            allowed_providers=state._CHAT_PROVIDER_ALLOWLIST,
        )
    except Exception as exc:
        return {}, str(exc)

    payload = _extract_json_object(response.text or "")
    if not payload:
        return {}, "model did not return JSON"
    docs = _sanitize_generated_doc_pack(payload)
    if not docs:
        return {}, "model returned no valid document content"
    return docs, ""

async def _write_initial_project_docs(
    project: dict,
    answers: dict[str, str],
    *,
    scaffold_only: bool = False,
) -> tuple[bool, str]:
    path = str(project.get("local_path") or "").strip()
    if not path:
        return False, "project local_path is empty"

    template_files = _load_finalized_template_files()
    if not template_files:
        return False, f"finalized template not found at {state._FINALIZED_TEMPLATE_PATH}"

    template_files["PROJECT.yaml"] = _render_project_yaml(project)
    template_files["PROJECT_STATE.yaml"] = _render_project_state_yaml()

    if scaffold_only:
        directories: set[str] = set()
        for rel in template_files.keys():
            rel_norm = _normalize_doc_relpath(rel)
            if "/" in rel_norm:
                directories.add(rel_norm.rsplit("/", 1)[0])

        ops: list[tuple[str, dict[str, str]]] = []
        for rel_dir in sorted(directories):
            ops.append(("create_directory", {"directory": _join_project_path(path, rel_dir)}))
        for rel, content in sorted(template_files.items(), key=lambda item: item[0]):
            ops.append(
                (
                    "file_write",
                    {
                        "file": _join_project_path(path, _normalize_doc_relpath(rel)),
                        "content": str(content),
                    },
                )
            )
        for action, params in ops:
            result = await _send_action(action, params, confirmed=True)
            ok, err = _action_result_ok(result)
            if not ok:
                return False, f"{action} failed: {err}"
        return True, "Template scaffold created; waiting for richer project details before populating docs."

    if not _has_minimum_doc_context(answers):
        return True, "Not enough project information yet; docs population deferred."

    baseline_docs = _build_baseline_doc_pack(_project_display(project), answers)
    llm_docs, llm_warning = await _generate_detailed_doc_pack_with_llm(
        project,
        answers,
        baseline_docs,
        review_pass=False,
    )

    final_docs = dict(baseline_docs)
    final_docs.update(llm_docs)
    reviewed_docs, review_warning = await _generate_detailed_doc_pack_with_llm(
        project,
        answers,
        final_docs,
        review_pass=True,
    )
    if reviewed_docs:
        final_docs.update(reviewed_docs)
    for rel, content in final_docs.items():
        template_files[rel] = content

    directories: set[str] = set()
    for rel in template_files.keys():
        rel_norm = _normalize_doc_relpath(rel)
        if "/" in rel_norm:
            directories.add(rel_norm.rsplit("/", 1)[0])

    ops: list[tuple[str, dict[str, str]]] = []
    for rel_dir in sorted(directories):
        ops.append(("create_directory", {"directory": _join_project_path(path, rel_dir)}))
    for rel, content in sorted(template_files.items(), key=lambda item: item[0]):
        ops.append(
            (
                "file_write",
                {
                    "file": _join_project_path(path, _normalize_doc_relpath(rel)),
                    "content": str(content),
                },
            )
        )

    for action, params in ops:
        result = await _send_action(action, params, confirmed=True)
        ok, err = _action_result_ok(result)
        if not ok:
            return False, f"{action} failed: {err}"
    notes: list[str] = []
    if llm_warning and not llm_docs:
        notes.append(f"LLM enrichment unavailable ({llm_warning}); baseline project docs were written.")
    elif llm_warning:
        notes.append(f"LLM enrichment note: {llm_warning}")
    if review_warning and not reviewed_docs:
        notes.append(f"Review pass unavailable ({review_warning}).")
    elif review_warning:
        notes.append(f"Review pass note: {review_warning}")
    if notes:
        return True, " ".join(notes)
    return True, ""

def _is_docs_infra_error(message: str) -> bool:
    """Return True when a doc-write failure is due to agent/SSH being unreachable.

    These are infrastructure gaps, not code bugs — suppress the red ERROR
    notification so the user isn't alarmed twice (they already saw the
    bootstrap-deferred message).
    """
    text = (message or "").lower()
    return any(marker in text for marker in (
        "ssh action failed",
        "unable to connect to port",
        "no connected agent",
        "agent not connected",
        "agent disconnected",
        "ssh fallback is not configured",
        "no existing session",
        "connection reset",
        "could not resolve hostname",
        "timed out",
        "timeout",
        "service unavailable",
    ))


async def _run_project_docs_generation_async(
    project: dict,
    answers: dict[str, str],
    *,
    reason: str,
    notify_user: bool = True,
) -> None:
    name = _project_display(project)
    if notify_user:
        await _notify_styled(
            "progress",
            "Documentation Update",
            f"Started documentation processing ({reason}). I will send a completion update.",
            project=name,
        )
    start = time.monotonic()
    ok, note = await _write_initial_project_docs(
        project,
        answers,
        scaffold_only=(reason == "project_create"),
    )
    elapsed = round(time.monotonic() - start, 1)
    if not ok and _is_docs_infra_error(note):
        # Agent/SSH unreachable — silently defer, no red notification.
        logger.info(
            "Doc generation deferred (agent unavailable) for project %s: %s",
            project.get("id", "?"),
            note,
        )
        return
    if ok:
        msg = (
            f"Documentation update complete ({reason}) in {elapsed}s.\n"
            f"Template root: {_join_project_path(project.get('local_path', ''), 'docs')}"
        )
        if note:
            msg += f"\nNote: {note}"
    else:
        msg = (
            f"Documentation update failed ({reason}) after {elapsed}s.\n"
            f"Error: {note}"
        )
    if notify_user:
        await _notify_styled("success" if ok else "error", "Documentation Update", msg, project=name)


async def _finalize_project_doc_intake(update: Update | None, intake_state: dict[str, Any]) -> None:
    if state._project_manager is None:
        return
    try:
        from db import store

        project = await store.get_project(state._project_manager.db, intake_state["project_id"])
    except Exception:
        logger.exception("Failed loading project for doc intake finalization.")
        project = None

    if not project:
        if update and update.message:
            await update.message.reply_text("I could not load the project to finalize documentation intake.")
        else:
            await _send_to_user("I could not load the project to finalize documentation intake.")
        return

    answers = dict(intake_state.get("answers") or {})
    idea_text = _intake_answers_to_idea_text(_project_display(project), answers)
    idea_count = None
    try:
        idea_count = await state._project_manager.add_idea(project["id"], idea_text)
    except Exception:
        logger.exception("Failed adding documentation intake as idea.")

    await _run_project_docs_generation_async(
        project,
        answers,
        reason="intake_finalize",
    )
    if idea_count:
        await _send_to_user(
            f"Captured documentation intake as idea #{idea_count} for '{_project_display(project)}'."
        )


async def _start_project_documentation_intake(update: Update, project: dict) -> None:
    key = _doc_intake_key(update)
    if key is None:
        return
    state._pending_project_doc_intake[key] = {
        "project_id": project["id"],
        "project_name": _project_display(project),
        "turn_count": 0,
        "last_doc_refresh_sig": "",
        "answers": {},
    }
    await update.message.reply_text(
        (
            f"Starting project documentation intake for '{_project_display(project)}'.\n"
            "Reply naturally in any format. I will extract details across problem, users, scope, success metrics, and technical constraints.\n"
            "You can say 'skip docs' to stop or 'proceed' when you feel context is enough.\n\n"
            "Tell me what you want this project to do in v1."
        )
    )

async def _maybe_handle_project_doc_intake(update: Update, text: str) -> bool:
    key = _doc_intake_key(update)
    if key is None:
        return False
    intake_state = state._pending_project_doc_intake.get(key)
    if not intake_state:
        return False
    if (text or "").strip().startswith("/"):
        return False

    # Keep greetings out of intake capture so the user can naturally greet
    # without corrupting documentation fields.
    if _is_smalltalk_or_ack(text):
        return False

    # If the user asks to start a new project while intake is pending, switch
    # context immediately and let the normal create-project flow run.
    if _is_explicit_new_project_request(text):
        state._pending_project_doc_intake.pop(key, None)
        return False

    answers_snapshot = dict(intake_state.get("answers") or {})
    project_name = str(intake_state.get("project_name") or "project")
    opt_out = _doc_intake_opt_out_requested(text)
    if not opt_out:
        opt_out = await _doc_intake_opt_out_requested_semantic(text, project_name, answers_snapshot)

    if opt_out:
        intake_state["answers"] = answers_snapshot
        state._pending_project_doc_intake.pop(key, None)
        _spawn_background_task(
            _capture_minimal_intake_idea_snapshot(state, note=text),
            tag=f"doc-intake-skip-snapshot-{intake_state.get('project_id', 'unknown')}",
        )
        await update.message.reply_text(
            (
                "Understood. I will not force documentation questions for this project.\n"
                "I kept docs minimal and captured your details as project notes. "
                "We can continue building."
            )
        )
        # Continue processing this same message as project detail input.
        await _maybe_capture_implicit_idea(update, text)
        return True

    answers = answers_snapshot
    extracted = await _extract_intake_signals(project_name, text, answers)
    for field in state._DOC_INTAKE_FIELDS:
        if field not in extracted:
            continue
        current = str(answers.get(field, ""))
        answers[field] = _merge_intake_value(
            current,
            extracted[field],
            max_chars=state._DOC_INTAKE_FIELD_LIMITS.get(field, 1200),
        )

    turn_count = int(intake_state.get("turn_count", 0)) + 1
    intake_state["turn_count"] = turn_count
    intake_state["answers"] = answers

    # Progressive docs refresh: once minimum context exists, keep template docs
    # aligned in background as new information arrives.
    if _has_minimum_doc_context(answers):
        sig = json.dumps(
            {k: answers.get(k, "") for k in state._DOC_INTAKE_FIELDS},
            sort_keys=True,
            ensure_ascii=False,
        )
        last_sig = str(intake_state.get("last_doc_refresh_sig", ""))
        if sig != last_sig and state._project_manager is not None:
            intake_state["last_doc_refresh_sig"] = sig
            try:
                from db import store

                project = await store.get_project(state._project_manager.db, str(intake_state.get("project_id", "")))
            except Exception:
                logger.exception("Failed loading project for progressive docs refresh.")
                project = None
            if project:
                _spawn_background_task(
                    _run_project_docs_generation_async(
                        project,
                        dict(answers),
                        reason="intake_progress",
                        notify_user=False,
                    ),
                    tag=f"doc-intake-progress-{intake_state.get('project_id', 'unknown')}",
                )

    state._pending_project_doc_intake[key] = state

    done_signal = _doc_intake_done_signal(text)
    if _intake_has_enough_context(answers, turn_count, done_signal):
        state._pending_project_doc_intake.pop(key, None)
        await update.message.reply_text(
            "Great, I have enough context. I will finalize detailed documentation now and notify you when it is done."
        )
        _spawn_background_task(
            _finalize_project_doc_intake(None, state),
            tag=f"doc-intake-finalize-{intake_state.get('project_id', 'unknown')}",
        )
        return True

    followup = _compose_dynamic_intake_followup(project_name, answers, turn_count)
    await update.message.reply_text(followup)
    return True

