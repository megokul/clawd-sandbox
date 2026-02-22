"""
bot/doc_intake.py -- Project documentation pipeline.

Provides document generation utilities called by ProjectManagementSkill.
The interactive doc intake modal has been removed in favour of natural
conversation: the LLM gathers requirements and calls project_generate_docs
when ready.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import state
from .helpers import (
    _action_result_ok,
    _extract_json_object,
    _join_project_path,
    _notify_styled,
    _project_display,
    _send_action,
    _spawn_background_task,
)

logger = logging.getLogger("skynet.telegram")

# Field names and character limits for doc generation answers.
_DOC_INTAKE_FIELDS = ("problem", "users", "requirements", "non_goals", "success_metrics", "tech_stack")
_DOC_INTAKE_FIELD_LIMITS: dict[str, int] = {
    "problem": 1200,
    "users": 800,
    "requirements": 2000,
    "non_goals": 1200,
    "success_metrics": 1200,
    "tech_stack": 800,
}


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
        max_chars=_DOC_INTAKE_FIELD_LIMITS["problem"],
    )
    users_list = _parse_natural_list(
        str(answers.get("users", "")),
        max_items=8,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["users"],
    )
    requirements = _parse_natural_list(
        str(answers.get("requirements", "")),
        max_items=20,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["requirements"],
    )
    non_goals_list = _parse_natural_list(
        str(answers.get("non_goals", "")),
        max_items=12,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["non_goals"],
    )
    metrics_list = _parse_natural_list(
        str(answers.get("success_metrics", "")),
        max_items=12,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["success_metrics"],
    )
    tech_list = _parse_natural_list(
        str(answers.get("tech_stack", "")),
        max_items=12,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["tech_stack"],
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
        max_chars=_DOC_INTAKE_FIELD_LIMITS["problem"],
    )
    users = _parse_natural_list(
        str(answers.get("users", "")),
        max_items=8,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["users"],
    )
    requirements = _parse_natural_list(
        str(answers.get("requirements", "")),
        max_items=20,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["requirements"],
    )
    tech = _parse_natural_list(
        str(answers.get("tech_stack", "")),
        max_items=12,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["tech_stack"],
    )
    non_goals = _parse_natural_list(
        str(answers.get("non_goals", "")),
        max_items=10,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["non_goals"],
    )
    metrics = _parse_natural_list(
        str(answers.get("success_metrics", "")),
        max_items=10,
        max_chars=_DOC_INTAKE_FIELD_LIMITS["success_metrics"],
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
            max_chars=_DOC_INTAKE_FIELD_LIMITS.get(field, 1200),
        )
        for field in _DOC_INTAKE_FIELDS
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
    notification so the user isn't alarmed twice.
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
