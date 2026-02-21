"""
bot/nl_intent.py -- NL intent extraction, project routing, and idea capture.
"""
from __future__ import annotations

import html
import logging
import re
import uuid
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

import bot_config as cfg
from . import state
from .helpers import (
    _ask_project_routing_choice,
    _ask_remove_project_confirmation,
    _clear_pending_project_route_for_user,
    _extract_json_object,
    _format_result,
    _has_pending_project_route_for_user,
    _is_explicit_new_project_request,
    _is_smalltalk_or_ack,
    _norm_project,
    _notify_styled,
    _pending_project_name_key,
    _project_bootstrap_note,
    _project_choice_label,
    _project_display,
    _run_gateway_action_in_background,
    _send_action,
    _send_to_user,
    _spawn_background_task,
    _store_pending_project_route_request,
)
from .doc_intake import (
    _compose_dynamic_intake_followup,
    _doc_intake_key,
    _run_project_docs_generation_async,
    _start_project_documentation_intake,
)
from .memory import _load_recent_conversation_messages

logger = logging.getLogger("skynet.telegram")


async def _capture_idea(update: Update, text: str) -> None:
    """Save one idea into the active ideation project."""
    if not state._project_manager:
        await update.message.reply_text("Project manager not initialized.")
        return

    project = await state._project_manager.get_ideation_project()
    if not project:
        await update.message.reply_text(
            "I do not have an ideation project open right now. "
            "Tell me the project name and I will create one first.",
        )
        return

    try:
        count = await state._project_manager.add_idea(project["id"], text)
        if cfg.AUTO_APPROVE_AND_START and count >= max(cfg.AUTO_PLAN_MIN_IDEAS, 1):
            await update.message.reply_text(
                (
                    f"Added idea #{count} to <b>{html.escape(project['display_name'])}</b>.\n"
                    f"Enough details received. Auto-generating plan and starting execution."
                ),
                parse_mode="HTML",
            )
            await _auto_plan_and_start(update, project["id"], project["display_name"])
            return

        await update.message.reply_text(
            f"Added idea #{count} to <b>{html.escape(project['display_name'])}</b>.\n"
            "Share more details naturally, or say 'generate the plan' when ready.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _auto_plan_and_start(update: Update, project_id: str, display_name: str) -> None:
    """Generate plan, approve it, and start execution without extra user prompts."""
    try:
        plan = await state._project_manager.generate_plan(project_id)
        await state._project_manager.approve_plan(project_id)
        await state._project_manager.start_execution(project_id)

        milestones = plan.get("milestones", []) or []
        milestone_names = [m.get("name", "").strip() for m in milestones if m.get("name")]
        top = ", ".join(milestone_names[:3]) if milestone_names else "No milestones listed."
        if len(milestone_names) > 3:
            top += f", and {len(milestone_names) - 3} more"

        await update.message.reply_text(
            (
                f"Autonomous execution started for <b>{html.escape(display_name)}</b>.\n"
                f"Top milestones: {html.escape(top)}\n"
                "I will report progress at milestone boundaries."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"I couldn't auto-start execution: {exc}")


def _clean_entity(text: str) -> str:
    """Trim punctuation/quotes from extracted NL entities."""
    cleaned = (text or "").strip().strip(" \t\r\n.,!?;:-")
    cleaned = re.sub(r"^(?:called|named|is)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[-:]+\s*", "", cleaned)
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"', "`"}:
        cleaned = cleaned[1:-1].strip()
    return re.sub(r"\s+", " ", cleaned)



def _is_pure_greeting(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return bool(
        re.fullmatch(
            (
                r"("
                r"(?:hi|hello|hey|heya|yo|sup)(?:\s+(?:there|skynet|bot))?"
                r"|good\s+(?:morning|afternoon|evening)"
                r")[.!? ]*"
            ),
            lowered,
        ),
    )



def _smalltalk_reply(text: str) -> str:
    lowered = (text or "").strip().lower()
    if any(tok in lowered for tok in ("thanks", "thank you")):
        return "You're welcome. What should we work on next?"
    return "Hi! How can I help today?"


async def _smalltalk_reply_with_context(update: Update, text: str) -> str:
    """
    Greeting policy:
    - Stay on current topic when there is an active pending workflow.
    - Otherwise keep greeting brief and open-ended.
    """
    base = _smalltalk_reply(text)
    # Pure greetings should stay lightweight and not force project/workflow prompts.
    if _is_pure_greeting(text):
        return base

    key = _doc_intake_key(update)
    if key is not None:
        if _has_pending_project_route_for_user(key):
            return base + " Please choose using the New Project / Add to Existing buttons."
        if key in state._pending_project_name_requests:
            return base + " I am waiting for the project name. Reply with the name only, or say 'cancel'."
        intake = state._pending_project_doc_intake.get(key)
        if intake:
            answers = dict(intake.get("answers") or {})
            turn_count = int(intake.get("turn_count", 0))
            project_name = str(intake.get("project_name") or "this project")
            q = _compose_dynamic_intake_followup(project_name, answers, turn_count)
            return base + " Let's continue the project documentation intake. " + q

    if state._project_manager is None or not state._last_project_id:
        return base

    try:
        from db import store

        project = await store.get_project(state._project_manager.db, state._last_project_id)
    except Exception:
        logger.exception("Failed resolving current project for contextual smalltalk.")
        project = None

    if not project:
        return base

    active_statuses = {"ideation", "planning", "approved", "coding", "testing", "paused"}
    status = str(project.get("status", "")).strip().lower()
    if status in active_statuses:
        return (
            base
            + f" We are currently on '{_project_display(project)}' ({status}). "
            "Do you want to continue this topic or switch to something else?"
        )
    return base



def _is_plausible_project_name(name: str) -> bool:
    cleaned = _clean_entity(name)
    if not cleaned:
        return False
    if len(cleaned) > 64:
        return False
    if any(ch in cleaned for ch in "\n\r\t"):
        return False
    if re.search(r"[.!?]", cleaned):
        return False
    lowered = cleaned.lower()
    if re.match(r"^(and|to|please)\b", lowered):
        return False
    if (
        len(cleaned.split()) > 4
        and re.search(
            r"\b(start|build|make|implement|create|run|click|beep|sound)\b",
            lowered,
        )
    ):
        return False
    # Reject names that are entirely a "start/create/make a new project" command phrase
    # e.g. "start a new project", "create a project", "a new project"
    if re.match(
        r"^(?:start|create|make|begin|kick\s*off|spin\s*up)\s+"
        r"(?:a\s+|an\s+|my\s+)?(?:new\w*\s+)?"
        r"(?:project|application|repo|proj|app)$",
        lowered,
    ):
        return False
    if re.match(r"^(?:a\s+|an\s+)?(?:new\s+)?(?:project|application|repo|proj|app)$", lowered):
        return False
    return True


def _extract_quoted_project_name_candidate(text: str) -> str:
    for match in re.finditer(r"[\"'`](.+?)[\"'`]", text or ""):
        candidate = _clean_entity(match.group(1))
        if _is_plausible_project_name(candidate) and not _is_existing_project_reference_phrase(candidate):
            return candidate
    return ""


def _is_existing_project_reference_phrase(text: str) -> bool:
    cleaned = _clean_entity(text).lower()
    generic_refs = {
        "same",
        "same project",
        "the same project",
        "this project",
        "that project",
        "current project",
        "existing project",
        "it",
        "this",
        "that",
    }
    return cleaned in generic_refs



def _extract_project_name_candidate(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if _is_smalltalk_or_ack(raw):
        return ""
    lowered = raw.lower()
    if re.match(r"^\s*(?:can\s+we|i\s+want\s+to|let'?s|could\s+you|would\s+you)\b", lowered):
        return ""
    quoted_name = _extract_quoted_project_name_candidate(raw)
    if quoted_name:
        return quoted_name

    # Handle descriptive replies while awaiting name:
    # "python app - my-name which does X"
    descriptive_patterns = (
        r"^(?:[a-z0-9+.#_-]+\s+)?(?:app|project|application|repo)\s*[-:]\s*(?P<name>.+)$",
        r"^(?:.*?\b)?(?:called|named)\s+(?P<name>.+)$",
    )
    for pattern in descriptive_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        tail = _clean_entity(match.group("name"))
        tail = re.split(
            r"\b(which|that|with|where|when|to|for)\b",
            tail,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        tail = _clean_entity(tail)
        if _is_plausible_project_name(tail):
            return tail

    # For follow-up name replies, prefer short plain phrases.
    if any(ch in raw for ch in ".!?;\n"):
        return ""
    name = _clean_entity(raw)
    if _is_existing_project_reference_phrase(name):
        return ""
    if len(name.split()) > 8:
        return ""
    return name if _is_plausible_project_name(name) else ""


def _extract_nl_intent(text: str) -> dict[str, str]:
    """
    Extract action intent/entities from natural language.

    Returns {} when input should be handled as normal chat.
    """
    raw = text.strip()
    lowered = raw.lower()

    # Keep greetings/small talk in regular chat flow.
    if _is_smalltalk_or_ack(raw):
        return {}

    if _is_explicit_new_project_request(raw):
        candidate = _extract_project_name_candidate(raw)
        if candidate and not _is_existing_project_reference_phrase(candidate):
            return {"intent": "create_project", "project_name": candidate}
        return {"intent": "create_project"}

    if re.search(
        r"\b(?:can\s+we|let'?s|i\s+want\s+to|we\s+should)\s+"
        r"(?:do|work\s+on)\s+(?:a|an|the|my)\s+(?:new\s+)?"
        r"(?:project|application|repo|proj|app)\b",
        raw,
        flags=re.IGNORECASE,
    ):
        return {"intent": "create_project"}

    # Create project
    create_patterns = [
        r"\b(?:create|start|begin|kick\s*off|make|spin\s*up)\s+"
        r"(?:a\s+|an\s+|the\s+|my\s+)?(?:new\s+|demo\s+|sample\s+|test\s+)?"
        r"(?:project|application|repo|proj|app)\b"
        r"(?:\s+(?:directory|dir|folder))?(?:\s+(?:called|named|for|with\s+name))?"
        r"\s*(?:-|:)?\s*(?P<name>.+)$",
        r"\b(?:i\s+want\s+to|let'?s|can\s+we|can\s+i)\s+"
        r"(?:create|start|begin|kick\s*off|make)\s+"
        r"(?:a\s+|an\s+|the\s+|my\s+)?(?:new\s+|demo\s+|sample\s+|test\s+)?"
        r"(?:project|application|repo|proj|app)\b"
        r"(?:\s+(?:called|named|for|with\s+name))?\s*(?:-|:)?\s*(?P<name>.+)$",
        r"\b(?:project|application|repo|proj|app)\b\s+(?:called|named)\s+(?P<name>.+)$",
        r"\bnew\s+(?:project|application|repo|proj|app)\b\s+(?:directory|dir|folder)?\s*(?:called|named)?\s*(?P<name>.+)$",
    ]
    for pattern in create_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            name = _clean_entity(match.group("name"))
            if _is_plausible_project_name(name):
                return {"intent": "create_project", "project_name": name}
    if re.search(
        r"\b(?:start|begin|run|kick\s*off)\s+(?:the|this|that|my)\s+"
        r"(?:project|application|repo|proj|app)\b",
        raw,
        flags=re.IGNORECASE,
    ):
        return {"intent": "approve_and_start"}
    if re.search(
        r"\b(?:create|start|begin|kick\s*off|make|spin\s*up|new)\b.*\b(?:project|proj|app|application|repo)\b",
        raw,
        flags=re.IGNORECASE,
    ) and not re.search(
        r"\b(?:execution|coding|work)\b",
        lowered,
    ) and not re.search(
        r"\b(?:start|begin|run|kick\s*off)\s+(?:the|this|that|my)\s+(?:project|proj|app|application|repo)\b",
        raw,
        flags=re.IGNORECASE,
    ) and not re.search(
        r"\b(?:make|build|create)\s+(?:it|this|that)\b",
        lowered,
    ):
        return {"intent": "create_project"}

    if re.search(
        r"\b(?:execute|run|build|proceed|continue)\b.*\b(?:project|prpjetc|proj|app|it|this|that)\b",
        lowered,
    ) or lowered in {
        "execute",
        "run it",
        "build project",
        "build prpjetc",
        "execute project",
    }:
        return {"intent": "approve_and_start"}

    # Add idea
    match = re.search(
        r"\b(?:add|save|capture|record)\s+(?:this\s+)?idea\s+for\s+(?P<project>[^:]+):\s*(?P<idea>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return {
            "intent": "add_idea",
            "project_name": _clean_entity(match.group("project")),
            "idea_text": _clean_entity(match.group("idea")),
        }
    match = re.search(
        r"\b(?:add|save|capture|record)\s+(?:this\s+)?idea\s*:\s*(?P<idea>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return {"intent": "add_idea", "idea_text": _clean_entity(match.group("idea"))}

    # Generate plan
    match = re.search(
        r"\b(?:generate|create|make|build)\s+(?:a\s+|the\s+)?plan(?:\s+(?:for|of)\s+(?P<project>.+))?$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        out = {"intent": "generate_plan"}
        project_name = _clean_entity(match.group("project") or "")
        if project_name:
            out["project_name"] = project_name
        return out
    match = re.search(r"\bplan\s+(?:for|of)\s+(?P<project>.+)$", raw, flags=re.IGNORECASE)
    if match:
        return {"intent": "generate_plan", "project_name": _clean_entity(match.group("project"))}

    # Approve/start plan
    match = re.search(
        r"\b(?:approve|accept)\s+(?:the\s+)?plan(?:\s+(?:for|of)\s+(?P<project>.+))?$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        out = {"intent": "approve_and_start"}
        project_name = _clean_entity(match.group("project") or "")
        if project_name:
            out["project_name"] = project_name
        return out
    match = re.search(
        r"\b(?:start|begin|run|kick off)\s+(?:execution|coding|work)(?:\s+(?:for|on)\s+(?P<project>.+))?$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        out = {"intent": "approve_and_start"}
        project_name = _clean_entity(match.group("project") or "")
        if project_name:
            out["project_name"] = project_name
        return out

    # Status / list
    if re.search(r"\b(?:list|show|which|what)\b.*\bprojects?\b", lowered) or lowered in {
        "projects",
        "list projects",
        "show projects",
    }:
        return {"intent": "list_projects"}
    if re.search(r"\b(?:status|progress|update)\b", lowered):
        match = re.search(
            r"\b(?:status|progress|update)(?:\s+(?:for|of|on))?\s+(?P<project>.+)$",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            return {"intent": "project_status", "project_name": _clean_entity(match.group("project"))}
        return {"intent": "project_status"}

    # Pause / resume / cancel / remove
    match = re.search(r"\bpause(?:\s+(?:project\s+)?)?(?P<project>.+)$", raw, flags=re.IGNORECASE)
    if match:
        return {"intent": "pause_project", "project_name": _clean_entity(match.group("project"))}
    match = re.search(
        r"\bresume(?:\s+(?:project\s+)?)?(?P<project>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return {"intent": "resume_project", "project_name": _clean_entity(match.group("project"))}
    if re.search(r"\b(?:remove|delete|drop)\b.*\bproject\b", lowered):
        match = re.search(
            r"\b(?:remove|delete|drop)\s+(?:the\s+)?project"
            r"(?:\s+(?:named|called))?(?:\s*[:-]\s*|\s+)?(?P<project>.*)$",
            raw,
            flags=re.IGNORECASE,
        )
        out: dict[str, str] = {"intent": "remove_project"}
        if match:
            project_name = _clean_entity(match.group("project") or "")
            if project_name:
                out["project_name"] = project_name
        return out
    match = re.search(
        r"\b(?:cancel|stop)\s+(?:project\s+)?(?P<project>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return {"intent": "cancel_project", "project_name": _clean_entity(match.group("project"))}

    # Coding agent checks
    if (
        lowered in {
            "check agents",
            "check coding agents",
            "list coding agents",
            "show coding agents",
            "which coding agents",
        }
        or re.search(
            r"\b(?:check|list|show|which|verify)\b.*\b(?:coding\s+agents?|codex|claude|cline)\b",
            lowered,
        )
    ):
        return {"intent": "check_coding_agents"}

    # Open path/project in VS Code
    for pattern in (
        r"\b(?:open|launch)\s+(?:(?P<path>.+?)\s+)?in\s+vs\s*code\b",
        r"\bopen\s+vscode(?:\s+(?P<path>.+))?$",
        r"\bopen\s+(?P<path>.+?)\s+with\s+vs\s*code\b",
    ):
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            path = _clean_entity(match.groupdict().get("path") or "")
            if path.lower() in {"this", "it", "project", "current project", "current"}:
                path = ""
            out = {"intent": "open_in_vscode"}
            if path:
                out["path"] = path
            return out

    # Run coding agent naturally.
    match = re.search(
        r"\b(?:use|run|ask)\s+(?P<agent>codex|claude|cline)\b"
        r"(?:\s+(?:on|in|at)\s+(?P<path>[^:]+?))?"
        r"(?:\s*(?::|to)\s*(?P<prompt>.+))?$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        out = {
            "intent": "run_coding_agent",
            "agent": _clean_entity(match.group("agent")).lower(),
        }
        path = _clean_entity(match.group("path") or "")
        prompt = _clean_entity(match.group("prompt") or "")
        if path:
            out["working_dir"] = path
        if prompt:
            out["prompt"] = prompt
        return out
    match = re.search(
        r"\b(?P<agent>codex|claude|cline)\s*:\s*(?P<prompt>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return {
            "intent": "run_coding_agent",
            "agent": _clean_entity(match.group("agent")).lower(),
            "prompt": _clean_entity(match.group("prompt")),
        }

    # Switch Cline provider/model
    provider_pattern = r"(?P<provider>gemini|deepseek|groq|openrouter|openai|anthropic)"
    model_pattern = r"(?:.*?\bmodel\s+(?P<model>[^,;]+))?"
    match = re.search(
        rf"\b(?:switch|set|change|configure)\s+cline(?:\s+(?:to|provider|using|use)\s+)?{provider_pattern}\b{model_pattern}",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            rf"\buse\s+{provider_pattern}\s+for\s+cline\b{model_pattern}",
            raw,
            flags=re.IGNORECASE,
        )
    if match:
        out = {
            "intent": "configure_coding_agent",
            "agent": "cline",
            "provider": _clean_entity(match.group("provider")).lower(),
        }
        model = _clean_entity(match.groupdict().get("model") or "")
        if model:
            out["model"] = model
        return out

    if lowered in {"help", "show help", "what can you do"}:
        return {"intent": "help"}

    return {}


_ALLOWED_NL_INTENTS = {
    "help",
    "check_coding_agents",
    "open_in_vscode",
    "run_coding_agent",
    "configure_coding_agent",
    "create_project",
    "list_projects",
    "add_idea",
    "generate_plan",
    "approve_and_start",
    "pause_project",
    "resume_project",
    "cancel_project",
    "remove_project",
    "project_status",
}



def _intent_is_actionable(intent_data: dict[str, str]) -> bool:
    """
    Return True when an intent has the minimum fields needed for execution.
    """
    intent = str(intent_data.get("intent", "")).strip().lower()
    if not intent:
        return False

    if intent == "run_coding_agent":
        agent = str(intent_data.get("agent", "")).strip().lower()
        prompt = str(intent_data.get("prompt", "")).strip()
        return agent in {"codex", "claude", "cline"} and bool(prompt)

    if intent == "configure_coding_agent":
        provider = str(intent_data.get("provider", "")).strip().lower()
        return provider in {"gemini", "deepseek", "groq", "openrouter", "openai", "anthropic"}

    if intent == "add_idea":
        return bool(str(intent_data.get("idea_text", "")).strip())

    # Other intents are valid without additional required entities.
    return True


def _merge_intent_payload(
    preferred: dict[str, str],
    fallback: dict[str, str],
) -> dict[str, str]:
    """
    Fill missing entities in `preferred` using `fallback` when intent matches.
    """
    if not preferred:
        return dict(fallback)
    if not fallback:
        return dict(preferred)
    if preferred.get("intent") != fallback.get("intent"):
        return dict(preferred)

    merged = dict(preferred)
    for key, value in fallback.items():
        if key == "intent":
            continue
        if key not in merged or not str(merged.get(key, "")).strip():
            merged[key] = value
    return merged


def _sanitize_nl_intent_payload(payload: dict | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    intent = str(payload.get("intent", "")).strip().lower()
    if intent in {"", "none", "chat", "general"}:
        return {}
    if intent not in _ALLOWED_NL_INTENTS:
        return {}

    out: dict[str, str] = {"intent": intent}
    if isinstance(payload.get("project_name"), str) and payload.get("project_name", "").strip():
        out["project_name"] = _clean_entity(str(payload["project_name"]))
    if isinstance(payload.get("idea_text"), str) and payload.get("idea_text", "").strip():
        out["idea_text"] = str(payload["idea_text"]).strip()
    if isinstance(payload.get("agent"), str) and payload.get("agent", "").strip():
        out["agent"] = _clean_entity(str(payload["agent"])).lower()
    if isinstance(payload.get("prompt"), str) and payload.get("prompt", "").strip():
        out["prompt"] = str(payload["prompt"]).strip()
    if isinstance(payload.get("working_dir"), str) and payload.get("working_dir", "").strip():
        out["working_dir"] = str(payload["working_dir"]).strip()
    if isinstance(payload.get("provider"), str) and payload.get("provider", "").strip():
        out["provider"] = _clean_entity(str(payload["provider"])).lower()
    if isinstance(payload.get("model"), str) and payload.get("model", "").strip():
        out["model"] = str(payload["model"]).strip()
    if isinstance(payload.get("path"), str) and payload.get("path", "").strip():
        out["path"] = str(payload["path"]).strip()

    return out



async def _extract_nl_intent_llm(text: str, update: Update | None = None) -> dict[str, str]:
    """LLM-first natural-language intent extraction for Telegram actions."""
    if not state._provider_router:
        return {}
    raw = (text or "").strip()
    if not raw or _is_smalltalk_or_ack(raw):
        return {}

    system_prompt = (
        "You are an intent classifier for a Telegram automation bot.\n"
        "Return ONLY one JSON object.\n"
        "Intent must be one of: none, help, check_coding_agents, open_in_vscode, "
        "run_coding_agent, configure_coding_agent, create_project, list_projects, "
        "add_idea, generate_plan, approve_and_start, pause_project, resume_project, "
        "cancel_project, remove_project, project_status.\n"
        "Extract fields only when present: project_name, idea_text, agent, prompt, "
        "working_dir, provider, model, path.\n"
        "Rules:\n"
        "- If user asks to create/start a new project but gives no name, use create_project with no project_name.\n"
        "- If user asks to start/resume/pause/cancel an existing project, do NOT use create_project.\n"
        "- Use remove_project only when user explicitly asks to delete/remove/drop a project.\n"
        "- Use recent conversation context for references like 'same project', 'it', 'that'.\n"
        "- If unsure, return {\"intent\":\"none\"}."
    )
    history = await _load_recent_conversation_messages(update, limit=8)
    llm_messages = [*history, {"role": "user", "content": raw}]
    try:
        response = await state._provider_router.chat(
            llm_messages,
            system=system_prompt,
            max_tokens=220,
            task_type="general",
            allowed_providers=state._CHAT_PROVIDER_ALLOWLIST,
        )
    except Exception as exc:
        logger.debug("LLM intent extraction failed: %s", exc)
        return {}

    payload = _extract_json_object(response.text or "")
    return _sanitize_nl_intent_payload(payload)


async def _extract_nl_intent_hybrid(text: str, update: Update | None = None) -> dict[str, str]:
    """
    Strict NL policy: use LLM intent extraction first, regex as resilience fallback.
    """
    regex_intent = _extract_nl_intent(text)
    try:
        llm_intent = await _extract_nl_intent_llm(text, update=update)
    except TypeError:
        # Backward-compatible for tests/mocks that still implement (text) only.
        llm_intent = await _extract_nl_intent_llm(text)  # type: ignore[misc]
    if not llm_intent:
        return regex_intent
    if not regex_intent:
        return llm_intent

    # If both sources agree on intent, merge entities so missing LLM fields
    # (for example prompt/agent) are backfilled from regex extraction.
    if llm_intent.get("intent") == regex_intent.get("intent"):
        return _merge_intent_payload(llm_intent, regex_intent)

    llm_actionable = _intent_is_actionable(llm_intent)
    regex_actionable = _intent_is_actionable(regex_intent)

    # Resilience fallback: if LLM output is not executable but regex is,
    # prefer executable regex intent to avoid dropping the user's action.
    if not llm_actionable and regex_actionable:
        logger.debug(
            "Intent mismatch; selecting actionable regex intent (llm=%s, regex=%s)",
            llm_intent,
            regex_intent,
        )
        return regex_intent

    # If LLM returned a generic "create_project" without a name but regex found
    # a specific actionable command (for example run_coding_agent), use regex.
    if (
        llm_intent.get("intent") == "create_project"
        and not str(llm_intent.get("project_name", "")).strip()
        and regex_actionable
    ):
        logger.debug(
            "LLM returned generic create_project; selecting more specific regex intent=%s",
            regex_intent,
        )
        return regex_intent

    return llm_intent


async def _resolve_project(reference: str | None = None) -> tuple[dict | None, str | None]:
    """Resolve a natural-language project reference to a concrete project."""
    if not state._project_manager:
        return None, "Project manager is not initialized."

    projects = await state._project_manager.list_projects()
    if not projects:
        return None, "No projects exist yet. Tell me the project name and I will create it."

    if reference:
        ref = _clean_entity(reference)
        ref_norm = _norm_project(ref)
        if not ref_norm:
            reference = None
        else:
            scored: list[tuple[int, dict]] = []
            for project in projects:
                display = _project_display(project)
                name = str(project.get("name", ""))
                d_norm = _norm_project(display)
                n_norm = _norm_project(name)
                if ref_norm in {d_norm, n_norm}:
                    scored.append((100, project))
                elif d_norm.startswith(ref_norm) or n_norm.startswith(ref_norm):
                    scored.append((80, project))
                elif ref_norm in d_norm or ref_norm in n_norm:
                    scored.append((60, project))

            if not scored:
                return None, f"I couldn't find a project named '{ref}'."

            scored.sort(key=lambda item: item[0], reverse=True)
            top_score = scored[0][0]
            top = [p for score, p in scored if score == top_score]
            if len(top) > 1:
                choices = ", ".join(_project_display(p) for p in top[:4])
                return None, f"I found multiple matches: {choices}. Tell me the exact name."

            state._last_project_id = top[0]["id"]
            return top[0], None

    # No explicit reference: use recent context first.
    if state._last_project_id:
        for project in projects:
            if project["id"] == state._last_project_id:
                return project, None

    ideation = [p for p in projects if p.get("status") == "ideation"]
    if len(ideation) == 1:
        state._last_project_id = ideation[0]["id"]
        return ideation[0], None

    if len(projects) == 1:
        state._last_project_id = projects[0]["id"]
        return projects[0], None

    active_statuses = {"planning", "approved", "coding", "testing", "paused"}
    active = [p for p in projects if p.get("status") in active_statuses]
    if len(active) == 1:
        state._last_project_id = active[0]["id"]
        return active[0], None

    choices = ", ".join(_project_display(p) for p in projects[:5])
    return None, f"Which project do you mean? I have: {choices}."



def _looks_like_implicit_idea(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < 8:
        return False
    if _is_smalltalk_or_ack(cleaned):
        return False
    lowered = cleaned.lower()
    if cleaned.endswith("?"):
        return False
    if lowered.startswith("/"):
        return False
    return True


async def _maybe_capture_implicit_idea(update: Update, text: str) -> bool:
    """Treat freeform follow-up text as an idea when a project is in ideation."""
    if not state._project_manager:
        return False
    if _is_explicit_new_project_request(text):
        return False
    if not _looks_like_implicit_idea(text):
        return False

    project = await state._project_manager.get_ideation_project()
    if not project:
        return False

    try:
        count = await state._project_manager.add_idea(project["id"], text)
        state._last_project_id = project["id"]
        if cfg.AUTO_APPROVE_AND_START and count >= max(cfg.AUTO_PLAN_MIN_IDEAS, 1):
            await update.message.reply_text(
                (
                    f"Added that as idea #{count} for <b>{html.escape(_project_display(project))}</b>.\n"
                    "Enough detail captured. Auto-generating the plan and starting execution."
                ),
                parse_mode="HTML",
            )
            await _auto_plan_and_start(update, project["id"], _project_display(project))
            return True

        await update.message.reply_text(
            (
                f"Added that as idea #{count} for <b>{html.escape(_project_display(project))}</b>.\n"
                "Share more details naturally, or say 'generate the plan' when ready."
            ),
            parse_mode="HTML",
        )
        return True
    except Exception:
        logger.exception("Failed implicit idea capture")
        return False



async def _handle_natural_action(update: Update, text: str) -> bool:
    """
    Execute extracted NL intent when possible.

    Returns True when a structured action was handled.
    """
    intent_data = await _extract_nl_intent_hybrid(text, update=update)
    if not intent_data:
        return False

    intent = intent_data.get("intent", "")

    if intent == "help":
        await update.message.reply_text(
            "You can talk naturally. Example phrases: "
            "'create project called API dashboard', "
            "'add idea for API dashboard: support OAuth', "
            "'generate plan for API dashboard', "
            "'status of API dashboard', 'pause API dashboard', "
            "'remove project API dashboard', "
            "'check coding agents', 'open current project in VS Code', "
            "'use codex to add JWT auth', "
            "'switch cline to gemini model gemini-2.0-flash'."
        )
        return True

    if intent == "check_coding_agents":
        try:
            result = await _send_action("check_coding_agents", {}, confirmed=True)
            await update.message.reply_text(_format_result(result), parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"I couldn't check coding agents: {exc}")
        return True

    if intent == "open_in_vscode":
        path = _clean_entity(intent_data.get("path", ""))
        if not path:
            project, _ = await _resolve_project()
            if project and project.get("local_path"):
                path = str(project["local_path"])
            else:
                path = cfg.PROJECT_BASE_DIR or cfg.DEFAULT_WORKING_DIR
        try:
            result = await _send_action("open_in_vscode", {"path": path}, confirmed=True)
            await update.message.reply_text(_format_result(result), parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"I couldn't open VS Code: {exc}")
        return True

    if intent == "run_coding_agent":
        agent = _clean_entity(intent_data.get("agent", "")).lower()
        prompt = _clean_entity(intent_data.get("prompt", ""))
        working_dir = _clean_entity(intent_data.get("working_dir", ""))
        if agent not in {"codex", "claude", "cline"}:
            await update.message.reply_text("Agent must be one of: codex, claude, cline.")
            return True
        if not prompt:
            await update.message.reply_text(f"Tell me what to ask {agent} to do.")
            return True
        if not working_dir:
            project, _ = await _resolve_project()
            if project and project.get("local_path"):
                working_dir = str(project["local_path"])
            else:
                working_dir = cfg.PROJECT_BASE_DIR or cfg.DEFAULT_WORKING_DIR

        try:
            await update.message.reply_text(
                (
                    f"Queued {agent} for background execution in '{working_dir}'.\n"
                    "You can continue chatting. I will send a styled notification with results."
                ),
            )
            _spawn_background_task(
                _run_gateway_action_in_background(
                    action="run_coding_agent",
                    params={"agent": agent, "prompt": prompt, "working_dir": working_dir},
                    title=f"Coding Agent ({agent})",
                    project=working_dir,
                ),
                tag=f"run-coding-agent-{agent}-{uuid.uuid4().hex[:8]}",
            )
        except Exception as exc:
            await update.message.reply_text(f"I couldn't run {agent}: {exc}")
        return True

    if intent == "configure_coding_agent":
        provider = _clean_entity(intent_data.get("provider", "")).lower()
        model = _clean_entity(intent_data.get("model", ""))
        if provider not in {"gemini", "deepseek", "groq", "openrouter", "openai", "anthropic"}:
            await update.message.reply_text(
                "Provider must be one of: gemini, deepseek, groq, openrouter, openai, anthropic.",
            )
            return True
        params = {"agent": "cline", "provider": provider}
        if model:
            params["model"] = model
        try:
            await update.message.reply_text(
                (
                    "Queued Cline provider update in background: "
                    f"{provider}" + (f" ({model})" if model else "") + "."
                ),
            )
            _spawn_background_task(
                _run_gateway_action_in_background(
                    action="configure_coding_agent",
                    params=params,
                    title="Cline Provider Update",
                    project="cline",
                ),
                tag=f"configure-coding-agent-{uuid.uuid4().hex[:8]}",
            )
        except Exception as exc:
            await update.message.reply_text(f"I couldn't switch Cline provider: {exc}")
        return True

    if intent == "create_project":
        name = intent_data.get("project_name", "")
        if name and _is_existing_project_reference_phrase(name):
            project, error = await _resolve_project()
            if error:
                await update.message.reply_text(error)
            else:
                state._last_project_id = project["id"]
                await update.message.reply_text(
                    f"Great, we'll continue in '{_project_display(project)}'. Tell me what you want to build."
                )
            return True
        if not name:
            return await _ask_project_routing_choice(update, text)
        key = _pending_project_name_key(update)
        if key is not None:
            state._pending_project_name_requests.pop(key, None)
        return await _create_project_from_name(update, name)

    if intent == "list_projects":
        try:
            projects = await state._project_manager.list_projects()
            if not projects:
                await update.message.reply_text("No projects yet.")
            else:
                lines = ["Here are your projects:"]
                for project in projects[:10]:
                    lines.append(f"- {_project_display(project)} ({project.get('status', 'unknown')})")
                await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"I couldn't list projects: {exc}")
        return True

    if intent == "add_idea":
        idea_text = intent_data.get("idea_text", "")
        project, error = await _resolve_project(intent_data.get("project_name"))
        if error:
            await update.message.reply_text(error)
            return True
        if not idea_text:
            await update.message.reply_text("Tell me the idea text to add.")
            return True
        try:
            count = await state._project_manager.add_idea(project["id"], idea_text)
            state._last_project_id = project["id"]
            if cfg.AUTO_APPROVE_AND_START and count >= max(cfg.AUTO_PLAN_MIN_IDEAS, 1):
                await update.message.reply_text(
                    (
                        f"Added that as idea #{count} for '{_project_display(project)}'.\n"
                        "Enough detail captured. Auto-generating plan and starting execution."
                    )
                )
                await _auto_plan_and_start(
                    update,
                    project["id"],
                    _project_display(project),
                )
                return True
            await update.message.reply_text(
                f"Added that as idea #{count} for '{_project_display(project)}'."
            )
        except Exception as exc:
            await update.message.reply_text(f"I couldn't add the idea: {exc}")
        return True

    if intent == "generate_plan":
        project, error = await _resolve_project(intent_data.get("project_name"))
        if error:
            await update.message.reply_text(error)
            return True
        try:
            await update.message.reply_text(
                (
                    f"Plan generation queued for '{_project_display(project)}'.\n"
                    "This runs in background; I will notify you with formatted updates."
                )
            )
            state._last_project_id = project["id"]
            project_name = _project_display(project)

            async def _bg_generate_plan() -> None:
                await _notify_styled(
                    "progress",
                    "Plan Generation",
                    "Started plan generation in background.",
                    project=project_name,
                )
                plan = await state._project_manager.generate_plan(project["id"])
                summary = (plan.get("summary") or "Plan generated.").strip()
                milestones = plan.get("milestones", []) or []
                top = [m.get("name", "").strip() for m in milestones if m.get("name")]
                top_text = ", ".join(top[:3]) if top else "No milestones listed."
                if len(top) > 3:
                    top_text += f", and {len(top) - 3} more"

                if cfg.AUTO_APPROVE_AND_START:
                    await state._project_manager.approve_plan(project["id"])
                    await state._project_manager.start_execution(project["id"])
                    await _notify_styled(
                        "success",
                        "Plan Generation",
                        (
                            "Plan generated, approved, and execution started.\n"
                            f"Summary: {summary}\n"
                            f"Top milestones: {top_text}"
                        ),
                        project=project_name,
                    )
                    return

                await _notify_styled(
                    "success",
                    "Plan Generation",
                    (
                        "Plan generated and awaiting approval.\n"
                        f"Summary: {summary}\n"
                        f"Top milestones: {top_text}"
                    ),
                    project=project_name,
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Approve", callback_data=f"approve_plan:{project['id']}"),
                    InlineKeyboardButton("Cancel", callback_data=f"cancel_plan:{project['id']}"),
                ]])
                if state._bot_app and state._bot_app.bot:
                    await state._bot_app.bot.send_message(
                        chat_id=cfg.ALLOWED_USER_ID,
                        text=(
                            f"<b>Plan approval needed</b>\n"
                            f"Project: <b>{html.escape(project_name)}</b>\n"
                            f"Top milestones: {html.escape(top_text)}"
                        ),
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )

            _spawn_background_task(
                _bg_generate_plan(),
                tag=f"generate-plan-{project['id']}-{uuid.uuid4().hex[:8]}",
            )
        except Exception as exc:
            await update.message.reply_text(f"I couldn't generate the plan: {exc}")
        return True

    if intent == "remove_project":
        project, error = await _resolve_project(intent_data.get("project_name"))
        if error:
            await update.message.reply_text(error)
            return True
        state._last_project_id = project["id"]
        await _ask_remove_project_confirmation(update, project)
        return True

    if intent in {"approve_and_start", "pause_project", "resume_project", "cancel_project", "project_status"}:
        project, error = await _resolve_project(intent_data.get("project_name"))
        if error:
            await update.message.reply_text(error)
            return True
        state._last_project_id = project["id"]

        if intent == "approve_and_start":
            try:
                await update.message.reply_text(
                    (
                        f"Queued execution start for '{_project_display(project)}'.\n"
                        "I will notify you when the state transition completes."
                    )
                )

                async def _bg_approve_start() -> None:
                    project_name = _project_display(project)
                    await _notify_styled(
                        "progress",
                        "Execution Start",
                        "Starting execution workflow in background.",
                        project=project_name,
                    )
                    status = str(project.get("status", ""))
                    if status in {"ideation", "planning"}:
                        await state._project_manager.approve_plan(project["id"])
                    if status in {"planning", "approved", "ideation"}:
                        await state._project_manager.start_execution(project["id"])
                        await _notify_styled(
                            "success",
                            "Execution Start",
                            "Execution started successfully.",
                            project=project_name,
                        )
                        return
                    await _notify_styled(
                        "warning",
                        "Execution Start",
                        f"Project is currently '{status}', so start was skipped.",
                        project=project_name,
                    )

                _spawn_background_task(
                    _bg_approve_start(),
                    tag=f"approve-start-{project['id']}-{uuid.uuid4().hex[:8]}",
                )
            except Exception as exc:
                await update.message.reply_text(f"I couldn't start execution: {exc}")
            return True

        if intent == "pause_project":
            try:
                await state._project_manager.pause_project(project["id"])
                await update.message.reply_text(f"Paused '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't pause it: {exc}")
            return True

        if intent == "resume_project":
            try:
                await state._project_manager.resume_project(project["id"])
                await update.message.reply_text(f"Resumed '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't resume it: {exc}")
            return True

        if intent == "cancel_project":
            try:
                await state._project_manager.cancel_project(project["id"])
                await update.message.reply_text(f"Cancelled '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't cancel it: {exc}")
            return True

        if intent == "project_status":
            try:
                status = await state._project_manager.get_status(project["id"])
                current = status.get("current_task")
                sentence = (
                    f"'{_project_display(project)}' is {status['project']['status']} "
                    f"with progress {status['progress']} ({status['percent']}%)."
                )
                if current:
                    sentence += f" Current task: {current}."
                await update.message.reply_text(sentence)
            except Exception as exc:
                await update.message.reply_text(f"I couldn't fetch status: {exc}")
            return True

    return False



async def _create_project_from_name(update: Update, name: str) -> bool:
    user = update.effective_user
    if user is not None:
        _clear_pending_project_route_for_user(int(user.id))
    try:
        project = await state._project_manager.create_project(name)
        state._last_project_id = project["id"]
        repo_line = (
            f"\nGitHub: {project.get('github_repo')}"
            if project.get("github_repo") else ""
        )
        bootstrap_note = _project_bootstrap_note(project)
        if bootstrap_note:
            bootstrap_note = "\n" + bootstrap_note
        await update.message.reply_text(
            (
                f"Created project '{_project_display(project)}' at {project.get('local_path', '')}.{repo_line}"
                f"{bootstrap_note}\n"
                "Tell me what you want it to do, and I'll take it forward."
            )
        )
        _spawn_background_task(
            _run_project_docs_generation_async(project, {}, reason="project_create", notify_user=False),
            tag=f"doc-init-{project['id']}",
        )
        await _start_project_documentation_intake(update, project)
        return True
    except Exception as exc:
        await update.message.reply_text(f"I couldn't create that project: {exc}")
        return True



def _extract_followup_idea_after_project_name(text: str, project_name: str) -> str:
    raw = text or ""
    idea = raw

    # Prefer removing quoted occurrence first when present.
    quoted_pattern = re.compile(rf"[\"'`]\s*{re.escape(project_name)}\s*[\"'`]", re.IGNORECASE)
    idea = quoted_pattern.sub("", idea, count=1)

    if idea == raw:
        idea = re.sub(re.escape(project_name), "", idea, count=1, flags=re.IGNORECASE)

    idea = re.sub(r"\s*[-:]\s*", " ", idea)
    idea = re.sub(r"\s+", " ", idea).strip(" .,;:-")
    if len(idea) < 12:
        return ""
    if _is_smalltalk_or_ack(idea):
        return ""
    return idea


async def _maybe_handle_pending_project_name(update: Update, text: str) -> bool:
    key = _pending_project_name_key(update)
    if key is None or key not in state._pending_project_name_requests:
        return False
    if (text or "").strip().startswith("/"):
        return False
    if _is_smalltalk_or_ack(text):
        return False

    intent_data = await _extract_nl_intent_hybrid(text, update=update)
    intent = str(intent_data.get("intent", "")).strip() if intent_data else ""

    if intent == "create_project" and intent_data.get("project_name"):
        if _is_existing_project_reference_phrase(intent_data.get("project_name", "")):
            state._pending_project_name_requests.pop(key, None)
            project, error = await _resolve_project()
            if error:
                await update.message.reply_text(error)
            else:
                await update.message.reply_text(
                    f"Continuing with '{_project_display(project)}'. Share the app details and I will proceed."
                )
            return True
        state._pending_project_name_requests.pop(key, None)
        return await _create_project_from_name(update, intent_data["project_name"])

    # If the user moved to another actionable request, release "pending name"
    # and let normal intent handling continue.
    non_name_intents = {
        "help",
        "list_projects",
        "project_status",
        "generate_plan",
        "approve_and_start",
        "pause_project",
        "resume_project",
        "cancel_project",
        "remove_project",
        "add_idea",
        "open_in_vscode",
    }
    if intent in non_name_intents:
        state._pending_project_name_requests.pop(key, None)
        return False

    # Keep waiting for project name if user asks runtime/tool actions.
    if intent in {"run_coding_agent", "configure_coding_agent", "check_coding_agents"}:
        await update.message.reply_text(
            "Before we continue, what should I name the project? (or say 'cancel')",
        )
        return True

    candidate = _extract_project_name_candidate(text)
    if candidate:
        state._pending_project_name_requests.pop(key, None)
        previous_project_id = state._last_project_id
        handled = await _create_project_from_name(update, candidate)

        # If the follow-up also contains build details, capture them as the first idea.
        new_project_id = state._last_project_id
        if (
            handled
            and state._project_manager is not None
            and new_project_id
            and new_project_id != previous_project_id
        ):
            idea_text = _extract_followup_idea_after_project_name(text, candidate)
            if idea_text:
                try:
                    count = await state._project_manager.add_idea(new_project_id, idea_text)
                    await update.message.reply_text(
                        f"Captured that as idea #{count} for '{candidate}'.",
                    )
                except Exception:
                    logger.exception("Failed capturing follow-up idea after project-name reply.")
        return handled

    if _is_existing_project_reference_phrase(text):
        state._pending_project_name_requests.pop(key, None)
        project, error = await _resolve_project()
        if error:
            await update.message.reply_text(error)
        else:
            await update.message.reply_text(
                f"Continuing with '{_project_display(project)}'. Share the app details and I will proceed."
            )
        return True

    lowered = (text or "").strip().lower()
    if lowered in {"cancel", "cancel it", "never mind", "nevermind", "forget it"}:
        state._pending_project_name_requests.pop(key, None)
        await update.message.reply_text("Okay, cancelled project creation.")
        return True

    if not candidate:
        await update.message.reply_text(
            "I didn't catch the name yet. Just send the project name (or say 'cancel').",
        )
        return True
    return True


# ------------------------------------------------------------------
# Progress callback (called by the orchestrator worker)
# ------------------------------------------------------------------

