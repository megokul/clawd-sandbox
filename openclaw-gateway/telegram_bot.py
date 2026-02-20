"""
SKYNET Gateway â€” Telegram Bot

Bridges Telegram to the SKYNET Gateway API and the SKYNET Core
orchestrator. Handles idea capture, plan generation, autonomous coding
progress updates, and CHATHAN worker commands.

Usage:
    Imported and started by main.py (merged into gateway process).
"""

from __future__ import annotations

import asyncio
import ast
import json
import logging
import html
import re
import uuid
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agents.main_persona import MainPersonaAgent
import bot_config as cfg
from ai.providers.base import ToolCall

logger = logging.getLogger("skynet.telegram")

# Injected at startup by main.py.
_project_manager = None
_provider_router = None
_heartbeat = None
_sentinel = None
_searcher = None
_skill_registry = None

# Stores pending CONFIRM actions keyed by a short ID.
_pending_confirms: dict[str, dict] = {}
_confirm_counter: int = 0

# Stores pending approval futures from the orchestrator worker.
# { "key": asyncio.Future }
_pending_approvals: dict[str, asyncio.Future] = {}
_approval_counter: int = 0
# Stores pending natural-language follow-up for project-name capture by user id.
_pending_project_name_requests: dict[int, dict[str, str]] = {}
# Stores pending project documentation intake by user id.
_pending_project_doc_intake: dict[int, dict[str, Any]] = {}

_PROJECT_DOC_INTAKE_STEPS: list[tuple[str, str]] = [
    ("problem", "What problem are we solving with this project?"),
    ("users", "Who are the primary users?"),
    ("requirements", "List the top requirements or features (comma-separated or bullets)."),
    ("non_goals", "What is explicitly out of scope?"),
    ("success_metrics", "How will we measure success?"),
    ("tech_stack", "Any preferred tech stack or constraints (language/framework/runtime)?"),
]

_DOC_INTAKE_FIELD_LIMITS: dict[str, int] = {
    "problem": 1500,
    "users": 800,
    "requirements": 2000,
    "non_goals": 1200,
    "success_metrics": 1200,
    "tech_stack": 1200,
}

# Reference to the Telegram app for sending proactive messages.
_bot_app: Application | None = None

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
# ------------------------------------------------------------------

def _authorised(update: Update) -> bool:
    user = update.effective_user
    if user and user.id == cfg.ALLOWED_USER_ID:
        return True
    logger.warning("Rejected message from user %s", user.id if user else "unknown")
    return False


async def _ensure_memory_user(update: Update) -> dict | None:
    user = update.effective_user
    if user is None or _project_manager is None:
        return None
    try:
        from db import store

        return await store.ensure_user(
            _project_manager.db,
            telegram_user_id=int(user.id),
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
        )
    except Exception:
        logger.exception("Failed to ensure user profile record.")
        return None


async def _append_user_conversation(
    update: Update,
    *,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> None:
    if _project_manager is None:
        return
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return
    try:
        from db import store

        msg = update.message
        await store.add_user_conversation(
            _project_manager.db,
            user_id=int(user_row["id"]),
            role=role,
            content=content,
            chat_id=str(getattr(msg, "chat_id", "")),
            telegram_message_id=str(getattr(msg, "message_id", "")),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("Failed to write user conversation record.")


async def _load_recent_conversation_messages(
    update: Update | None,
    *,
    limit: int | None = None,
) -> list[dict]:
    """
    Load recent user/assistant turns from durable storage.

    Falls back to process-local history when DB lookup is unavailable.
    """
    max_items = int(limit or (_CHAT_HISTORY_MAX * 2))
    if (
        update is None
        or _project_manager is None
        or not hasattr(_project_manager, "db")
    ):
        return _chat_history[-max_items:]

    try:
        user_row = await _ensure_memory_user(update)
        if not user_row:
            return _chat_history[-max_items:]
        from db import store

        rows = await store.list_user_conversations(
            _project_manager.db,
            user_id=int(user_row["id"]),
            limit=max_items,
        )
    except Exception:
        logger.exception("Failed to load persistent conversation history.")
        return _chat_history[-max_items:]

    messages: list[dict] = []
    for row in rows:
        role = str(row.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        messages.append({"role": role, "content": content[:4000]})

    return messages[-max_items:] if messages else _chat_history[-max_items:]


async def _profile_prompt_context(update: Update) -> str:
    if _project_manager is None:
        return ""
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return ""
    try:
        from db import store

        user_id = int(user_row["id"])
        facts = await store.list_profile_facts(_project_manager.db, user_id=user_id, active_only=True)
        prefs = await store.get_user_preferences(_project_manager.db, user_id=user_id)
        chunks: list[str] = []
        if user_row.get("timezone"):
            chunks.append(f"timezone={user_row['timezone']}")
        if user_row.get("region"):
            chunks.append(f"region={user_row['region']}")
        for pref in prefs[:12]:
            chunks.append(f"pref:{pref['pref_key']}={pref['pref_value']}")
        for fact in facts[:16]:
            chunks.append(f"fact:{fact['fact_key']}={fact['fact_value']}")
        return "\n".join(chunks)
    except Exception:
        logger.exception("Failed to build profile prompt context.")
        return ""


def _extract_memory_candidates(text: str) -> tuple[list[tuple[str, str, float]], list[tuple[str, str]]]:
    lowered = text.lower()
    facts: list[tuple[str, str, float]] = []
    prefs: list[tuple[str, str]] = []

    name_match = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 _-]{1,40})\b", text, re.IGNORECASE)
    if name_match:
        facts.append(("name", name_match.group(1).strip(), 0.95))

    call_me = re.search(r"\bcall me\s+([A-Za-z][A-Za-z0-9 _-]{1,40})\b", text, re.IGNORECASE)
    if call_me:
        facts.append(("preferred_name", call_me.group(1).strip(), 0.9))

    tz_match = re.search(r"\b(?:timezone is|tz is|i am in timezone)\s+([A-Za-z0-9_/\-+:.]{2,32})\b", text, re.IGNORECASE)
    if tz_match:
        facts.append(("timezone", tz_match.group(1).strip(), 0.9))
    else:
        utc_match = re.search(r"\butc\s*([+-]\d{1,2}(?::\d{2})?)\b", lowered)
        if utc_match:
            facts.append(("timezone", f"UTC{utc_match.group(1)}", 0.85))

    region_match = re.search(r"\b(?:i live in|i am in|i'm in|based in)\s+([A-Za-z0-9 ,._-]{2,60})\b", text, re.IGNORECASE)
    if region_match:
        facts.append(("region", region_match.group(1).strip(" .,"), 0.75))

    if "no emoji" in lowered or "no emojis" in lowered:
        prefs.append(("tone.no_emojis", "true"))
    if "be concise" in lowered or "short answers" in lowered:
        prefs.append(("response.verbosity", "concise"))
    if "be detailed" in lowered or "more detail" in lowered:
        prefs.append(("response.verbosity", "detailed"))
    if "no fluff" in lowered:
        prefs.append(("tone.no_fluff", "true"))

    for token, key in (
        ("ec2", "environment.ec2"),
        ("docker", "environment.docker"),
        ("windows", "environment.windows"),
        ("linux", "environment.linux"),
    ):
        if token in lowered:
            facts.append((key, "true", 0.6))

    return facts, prefs


def _is_no_store_once_message(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NO_STORE_ONCE_MARKERS)


def _is_no_store_chat_message(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NO_STORE_CHAT_MARKERS)


async def _capture_profile_memory(update: Update, text: str, *, skip_store: bool) -> None:
    if _project_manager is None:
        return
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return

    try:
        from db import store

        user_id = int(user_row["id"])
        await _append_user_conversation(update, role="user", content=text)

        if skip_store:
            await store.add_memory_audit_log(
                _project_manager.db,
                user_id=user_id,
                action="skip_store_once",
                target_type="message",
                target_key="user_text",
                detail="User requested no-store for this message.",
            )
            return

        if int(user_row.get("memory_enabled", 1)) != 1:
            return

        facts, prefs = _extract_memory_candidates(text)
        for key, value, confidence in facts:
            await store.add_or_update_profile_fact(
                _project_manager.db,
                user_id=user_id,
                fact_key=key,
                fact_value=value,
                confidence=confidence,
                source="telegram_text",
            )
            await store.add_memory_audit_log(
                _project_manager.db,
                user_id=user_id,
                action="fact_upsert",
                target_type="fact",
                target_key=key,
                detail=f"{key}={value}",
            )
            if key == "timezone":
                await store.update_user_core_fields(_project_manager.db, user_id=user_id, timezone=value)
            if key == "region":
                await store.update_user_core_fields(_project_manager.db, user_id=user_id, region=value)

        for pref_key, pref_value in prefs:
            await store.upsert_user_preference(
                _project_manager.db,
                user_id=user_id,
                pref_key=pref_key,
                pref_value=pref_value,
                source="telegram_text",
            )
            await store.add_memory_audit_log(
                _project_manager.db,
                user_id=user_id,
                action="preference_upsert",
                target_type="preference",
                target_key=pref_key,
                detail=f"{pref_key}={pref_value}",
            )
    except Exception:
        logger.exception("Failed memory capture pipeline.")


async def _format_profile_summary(update: Update) -> str:
    if _project_manager is None:
        return "User profile is unavailable."
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return "User profile is unavailable."

    from db import store

    user_id = int(user_row["id"])
    facts = await store.list_profile_facts(_project_manager.db, user_id=user_id, active_only=True)
    prefs = await store.get_user_preferences(_project_manager.db, user_id=user_id)

    lines = [
        "<b>User Profile</b>",
        f"Memory enabled: <b>{'yes' if int(user_row.get('memory_enabled', 1)) == 1 else 'no'}</b>",
    ]
    if user_row.get("timezone"):
        lines.append(f"Timezone: <code>{html.escape(str(user_row['timezone']))}</code>")
    if user_row.get("region"):
        lines.append(f"Region: <code>{html.escape(str(user_row['region']))}</code>")

    lines.append("")
    lines.append("<b>Facts</b>")
    if facts:
        for fact in facts[:20]:
            lines.append(
                f"- <code>{html.escape(str(fact['fact_key']))}</code>: "
                f"{html.escape(str(fact['fact_value']))} "
                f"(conf={float(fact.get('confidence', 0.0)):.2f})"
            )
    else:
        lines.append("- none")

    lines.append("")
    lines.append("<b>Preferences</b>")
    if prefs:
        for pref in prefs:
            lines.append(
                f"- <code>{html.escape(str(pref['pref_key']))}</code>: "
                f"{html.escape(str(pref['pref_value']))}"
            )
    else:
        lines.append("- none")

    return "\n".join(lines)


async def _gateway_get(endpoint: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{cfg.GATEWAY_API_URL}{endpoint}", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            return await resp.json()


async def _gateway_post(endpoint: str, body: dict | None = None) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{cfg.GATEWAY_API_URL}{endpoint}",
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=130),
        ) as resp:
            return await resp.json()


async def _send_action(action: str, params: dict, confirmed: bool = False) -> dict:
    return await _gateway_post("/action", {
        "action": action, "params": params, "confirmed": confirmed,
    })


def _format_result(result: dict) -> str:
    status = result.get("status", "unknown")
    action = result.get("action", "")
    if status == "error":
        error = result.get("error", "Unknown error")
        return f"<b>Error</b> ({action}):\n<code>{html.escape(error)}</code>"
    inner = result.get("result", {})
    rc = inner.get("returncode", "?")
    stdout = inner.get("stdout", "").strip()
    stderr = inner.get("stderr", "").strip()
    parts = [f"<b>{action}</b>  [exit {rc}]"]
    if stdout:
        if len(stdout) > 3500:
            stdout = stdout[:3500] + "\n... (truncated)"
        parts.append(f"<pre>{html.escape(stdout)}</pre>")
    if stderr:
        if len(stderr) > 1000:
            stderr = stderr[:1000] + "\n... (truncated)"
        parts.append(f"<b>stderr:</b>\n<pre>{html.escape(stderr)}</pre>")
    return "\n".join(parts)


def _parse_path(args: list[str], index: int = 0) -> str:
    if args and len(args) > index:
        return args[index]
    return cfg.PROJECT_BASE_DIR or cfg.DEFAULT_WORKING_DIR


def _store_pending(action: str, params: dict) -> str:
    global _confirm_counter
    _confirm_counter += 1
    key = f"c{_confirm_counter}"
    _pending_confirms[key] = {"action": action, "params": params}
    return key


async def _ask_confirm(update: Update, action: str, params: dict, summary: str) -> None:
    key = _store_pending(action, params)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"approve:{key}"),
        InlineKeyboardButton("Deny", callback_data=f"deny:{key}"),
    ]])
    await update.message.reply_text(
        f"<b>CONFIRM</b> -- {html.escape(action)}\n{summary}\n\nApprove this action?",
        parse_mode="HTML", reply_markup=keyboard,
    )


async def _send_to_user(text: str, parse_mode: str = "HTML") -> None:
    """Send a proactive message to the authorised user."""
    if _bot_app and _bot_app.bot:
        try:
            await _bot_app.bot.send_message(
                chat_id=cfg.ALLOWED_USER_ID, text=text, parse_mode=parse_mode,
            )
        except Exception as exc:
            logger.warning("Failed to send proactive message: %s", exc)


def _trim_chat_history() -> None:
    """Keep only the most recent conversation turns in memory."""
    global _chat_history
    max_items = _CHAT_HISTORY_MAX * 2
    if len(_chat_history) > max_items:
        _chat_history = _chat_history[-max_items:]


def _build_assistant_content(response) -> object:
    """Build assistant message content including tool_use blocks."""
    parts = []
    if response.text:
        parts.append({"type": "text", "text": response.text})
    for tc in response.tool_calls:
        parts.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.name,
            "input": tc.input,
        })
    return parts if parts else response.text


def _extract_textual_tool_call(text: str) -> ToolCall | None:
    """
    Recover a tool call when a model emits it as plain text instead of structured tool_calls.
    Supports payloads like:
      {'type': 'tool_use', 'id': '...', 'name': 'git_init', 'input': {...}}
    """
    raw = (text or "").strip()
    if not raw:
        return None

    candidates: list[str] = [raw]
    # Strip fenced block if present.
    if raw.startswith("```") and raw.endswith("```"):
        body = raw.strip("`").strip()
        body = re.sub(r"^(json|python)\s*", "", body, flags=re.IGNORECASE)
        candidates.append(body.strip())

    # Try first object-like block from freeform text.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for cand in candidates:
        obj = None
        try:
            obj = ast.literal_eval(cand)
        except Exception:
            try:
                obj = json.loads(cand)
            except Exception:
                obj = None
        if not isinstance(obj, dict):
            continue

        tool_type = str(obj.get("type", "")).strip().lower()
        name = obj.get("name")
        tool_input = obj.get("input")
        if tool_type not in {"tool_use", "function_call", ""}:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(tool_input, dict):
            continue

        tool_id = str(obj.get("id") or f"text_tool_{uuid.uuid4().hex[:10]}")
        return ToolCall(id=tool_id, name=name.strip(), input=tool_input)

    return None


def _extract_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None

    candidates: list[str] = [raw]
    if raw.startswith("```"):
        fenced = raw.strip("`").strip()
        fenced = re.sub(r"^(json|javascript|python)\s*", "", fenced, flags=re.IGNORECASE)
        candidates.append(fenced.strip())
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for cand in candidates:
        obj = None
        try:
            obj = json.loads(cand)
        except Exception:
            try:
                obj = ast.literal_eval(cand)
            except Exception:
                obj = None
        if isinstance(obj, dict):
            return obj
    return None


async def _maybe_notify_model_switch(update: Update, response) -> None:
    """Send a compact notice when provider/model changes."""
    global _last_model_signature
    provider = (getattr(response, "provider_name", "") or "").strip()
    model = (getattr(response, "model", "") or "").strip()
    if not provider and not model:
        return

    signature = f"{provider}:{model}"
    if _last_model_signature and signature != _last_model_signature:
        await update.message.reply_text(
            f"Note: switched model to {model} ({provider}) based on availability.",
        )
    _last_model_signature = signature


def _friendly_ai_error(exc: Exception) -> str:
    """Convert provider stack errors into a concise user-facing message."""
    text = str(exc)
    lower = text.lower()
    if "resource_exhausted" in lower or "quota" in lower or "429" in lower or "rate" in lower:
        if cfg.GEMINI_ONLY_MODE:
            return (
                "Gemini quota/rate limit reached. "
                "Please retry shortly or increase Gemini API quota."
            )
        return (
            "AI quota/rate limit reached for the current provider. "
            "I will use fallback cloud providers if available; otherwise add/refresh provider keys."
        )
    if "no ai providers available" in lower:
        if cfg.GEMINI_ONLY_MODE:
            return "Gemini provider is not available. Check GOOGLE_AI_API_KEY and GEMINI_MODEL."
        return "No cloud AI providers are currently available. Add at least one active API key."
    return f"OpenClaw chat error: {text}"


async def _reply_with_openclaw_capabilities(update: Update, text: str) -> None:
    """Route natural conversation through OpenClaw tools + skills."""
    if not _provider_router:
        await update.message.reply_text("AI providers are not configured.")
        return
    if not _skill_registry:
        await _reply_naturally_fallback(update, text)
        return

    history = await _load_recent_conversation_messages(update)
    messages = [*history, {"role": "user", "content": text}]
    tools = _skill_registry.get_all_tools()

    project_id = "telegram_chat"
    project_path = cfg.PROJECT_BASE_DIR or cfg.DEFAULT_WORKING_DIR
    if _project_manager and _last_project_id:
        try:
            from db import store
            project = await store.get_project(_project_manager.db, _last_project_id)
            if project:
                project_id = project["id"]
                project_path = project.get("local_path") or project_path
        except Exception:
            logger.exception("Failed to resolve project context for chat")

    base_system_prompt = (
        f"{_CHAT_SYSTEM_PROMPT}\n\n"
        f"Working directory: {project_path}\n"
        "If you perform filesystem/git/build actions, prefer this context unless the user specifies another path."
    )
    if _main_persona_agent.should_delegate(text):
        base_system_prompt += (
            "\n\nThis looks like long-running work. "
            "Prefer delegated execution through tools and avoid claiming completion "
            "until tool results confirm it."
        )

    profile_context = await _profile_prompt_context(update)
    system_prompt = _main_persona_agent.compose_system_prompt(
        base_system_prompt,
        profile_context=profile_context,
    )
    try:
        prompt_context = _skill_registry.get_prompt_skill_context(text, role="chat")
        if prompt_context:
            system_prompt += (
                "\n\n[External Skill Guidance]\n"
                "Use the following skill guidance if it helps solve the request:\n\n"
                f"{prompt_context}"
            )
    except Exception:
        logger.exception("Failed to inject external skill guidance into Telegram chat")

    rounds = 0
    final_text = ""
    try:
        while rounds < 12:
            response = await _provider_router.chat(
                messages,
                tools=tools,
                system=system_prompt,
                max_tokens=1500,
                require_tools=True,
                task_type="general",
                allowed_providers=_CHAT_PROVIDER_ALLOWLIST,
            )
            await _maybe_notify_model_switch(update, response)
            messages.append({"role": "assistant", "content": _build_assistant_content(response)})

            tool_calls = list(response.tool_calls or [])
            if not tool_calls:
                recovered = _extract_textual_tool_call(response.text or "")
                if recovered:
                    tool_calls = [recovered]

            if not tool_calls:
                final_text = (response.text or "").strip()
                break

            from skills.base import SkillContext

            context = SkillContext(
                project_id=project_id,
                project_path=project_path,
                gateway_api_url=cfg.GATEWAY_API_URL,
                searcher=_searcher,
                request_approval=request_worker_approval,
            )
            tool_results = []
            for tc in tool_calls:
                skill = _skill_registry.get_skill_for_tool(tc.name)
                if skill is None:
                    result = f"Unknown tool: {tc.name}"
                else:
                    result = await skill.execute(tc.name, tc.input, context)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
            rounds += 1
    except Exception as exc:
        await update.message.reply_text(_friendly_ai_error(exc))
        return

    if not final_text:
        try:
            summary = await _provider_router.chat(
                messages + [{
                    "role": "user",
                    "content": "Summarize the result and next step in plain language.",
                }],
                system=system_prompt,
                max_tokens=700,
                task_type="general",
                allowed_providers=_CHAT_PROVIDER_ALLOWLIST,
            )
            await _maybe_notify_model_switch(update, summary)
            final_text = (summary.text or "").strip()
        except Exception:
            final_text = ""

    reply = final_text
    if not reply:
        reply = "I could not generate a reply right now."
    reply = _main_persona_agent.compose_final_response(reply)
    if len(reply) > 3800:
        reply = reply[:3800] + "\n\n... (truncated)"

    # Keep chat history in a compact text form.
    _chat_history.append({"role": "user", "content": text})
    _chat_history.append({"role": "assistant", "content": reply})
    _trim_chat_history()

    await update.message.reply_text(reply)
    await _append_user_conversation(
        update,
        role="assistant",
        content=reply,
        metadata={"channel": "openclaw_capabilities"},
    )


async def _reply_naturally_fallback(update: Update, text: str) -> None:
    """Fallback chat path without tool execution."""
    if not _provider_router:
        await update.message.reply_text("AI providers are not configured.")
        return

    history = await _load_recent_conversation_messages(update)
    base_system_prompt = _CHAT_SYSTEM_PROMPT
    if _main_persona_agent.should_delegate(text):
        base_system_prompt += (
            "\n\nThis looks like long-running work. "
            "Do not pretend it is completed in chat; provide a concise delegated plan."
        )
    if _skill_registry:
        try:
            prompt_context = _skill_registry.get_prompt_skill_context(text, role="chat")
            if prompt_context:
                base_system_prompt += (
                    "\n\n[External Skill Guidance]\n"
                    "Use the following skill guidance if relevant:\n\n"
                    f"{prompt_context}"
                )
        except Exception:
            logger.exception("Failed to inject external skill guidance into fallback chat")
    profile_context = await _profile_prompt_context(update)
    system_prompt = _main_persona_agent.compose_system_prompt(
        base_system_prompt,
        profile_context=profile_context,
    )

    messages = [*history, {"role": "user", "content": text}]
    try:
        response = await _provider_router.chat(
            messages,
            system=system_prompt,
            max_tokens=700,
            task_type="general",
            allowed_providers=_CHAT_PROVIDER_ALLOWLIST,
        )
        await _maybe_notify_model_switch(update, response)
    except Exception as exc:
        await update.message.reply_text(_friendly_ai_error(exc))
        return

    reply = (response.text or "").strip() or "I could not generate a reply right now."
    reply = _main_persona_agent.compose_final_response(reply)
    _chat_history.append({"role": "user", "content": text})
    _chat_history.append({"role": "assistant", "content": reply})
    _trim_chat_history()
    await update.message.reply_text(reply)
    await _append_user_conversation(
        update,
        role="assistant",
        content=reply,
        metadata={"channel": "fallback"},
    )


async def _capture_idea(update: Update, text: str) -> None:
    """Save one idea into the active ideation project."""
    if not _project_manager:
        await update.message.reply_text("Project manager not initialized.")
        return

    project = await _project_manager.get_ideation_project()
    if not project:
        await update.message.reply_text(
            "I do not have an ideation project open right now. "
            "Tell me the project name and I will create one first.",
        )
        return

    try:
        count = await _project_manager.add_idea(project["id"], text)
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
        plan = await _project_manager.generate_plan(project_id)
        await _project_manager.approve_plan(project_id)
        await _project_manager.start_execution(project_id)

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


def _is_smalltalk_or_ack(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return bool(
        re.fullmatch(
            (
                r"("
                r"(?:hi|hello|hey|yo|sup)(?:\s+(?:there|skynet|bot))?"
                r"|good\s+(?:morning|afternoon|evening)"
                r"|thanks|thank you|ok|okay|cool|great|nice|got it|understood"
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
    key = _doc_intake_key(update)
    if key is not None:
        if key in _pending_project_name_requests:
            return base + " I am waiting for the project name. Reply with the name only, or say 'cancel'."
        intake = _pending_project_doc_intake.get(key)
        if intake:
            idx = int(intake.get("step_index", 0))
            total = len(_PROJECT_DOC_INTAKE_STEPS)
            if 0 <= idx < total:
                q = _PROJECT_DOC_INTAKE_STEPS[idx][1]
                return base + f" Let's continue the project documentation intake. Q{idx + 1}/{total}: {q}"

    if _project_manager is None or not _last_project_id:
        return base

    try:
        from db import store

        project = await store.get_project(_project_manager.db, _last_project_id)
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


def _norm_project(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _project_display(project: dict) -> str:
    return str(project.get("display_name") or project.get("name") or "project")


def _project_bootstrap_note(project: dict) -> str:
    summary = str(project.get("bootstrap_summary") or "").strip()
    if not summary:
        return ""
    lowered = summary.lower()
    if "failed" in lowered:
        return (
            f"Bootstrap issue: {summary}\n"
            "Project record was created, but workspace setup did not fully complete."
        )
    return f"Bootstrap: {summary}"


def _join_project_path(base: str, leaf: str) -> str:
    sep = "\\" if ("\\" in base or ":" in base) else "/"
    return base.rstrip("\\/") + sep + leaf.strip("\\/")


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


def _intake_answers_to_idea_text(project_name: str, answers: dict[str, str]) -> str:
    lines = [f"Initial documentation intake for {project_name}:"]
    for key, _question in _PROJECT_DOC_INTAKE_STEPS:
        val = _sanitize_intake_text(
            str(answers.get(key, "")),
            max_chars=_DOC_INTAKE_FIELD_LIMITS.get(key, 1200),
        )
        if val:
            lines.append(f"- {key}: {val}")
    return "\n".join(lines)


def _action_result_ok(result: dict[str, Any]) -> tuple[bool, str]:
    if result.get("status") == "error" or result.get("error"):
        return False, str(result.get("error", "Unknown action error"))
    inner = result.get("result", {}) if isinstance(result.get("result", {}), dict) else {}
    rc = inner.get("returncode", 0)
    try:
        rc_int = int(rc)
    except Exception:
        rc_int = 0
    if rc_int != 0:
        stderr = str(inner.get("stderr", "")).strip()
        stdout = str(inner.get("stdout", "")).strip()
        return False, stderr or stdout or f"exit code {rc_int}"
    return True, ""


async def _write_initial_project_docs(project: dict, answers: dict[str, str]) -> tuple[bool, str]:
    path = str(project.get("local_path") or "").strip()
    if not path:
        return False, "project local_path is empty"

    prd, overview, features = _format_initial_docs_from_answers(
        _project_display(project),
        answers,
    )
    docs_dir = _join_project_path(path, "docs")
    product_dir = _join_project_path(docs_dir, "product")

    ops = [
        ("create_directory", {"directory": docs_dir}),
        ("create_directory", {"directory": product_dir}),
        ("file_write", {"file": _join_project_path(product_dir, "PRD.md"), "content": prd}),
        ("file_write", {"file": _join_project_path(product_dir, "overview.md"), "content": overview}),
        ("file_write", {"file": _join_project_path(product_dir, "features.md"), "content": features}),
    ]
    for action, params in ops:
        result = await _send_action(action, params, confirmed=True)
        ok, err = _action_result_ok(result)
        if not ok:
            return False, f"{action} failed: {err}"
    return True, ""


async def _finalize_project_doc_intake(update: Update, state: dict[str, Any]) -> None:
    if _project_manager is None:
        return
    try:
        from db import store

        project = await store.get_project(_project_manager.db, state["project_id"])
    except Exception:
        logger.exception("Failed loading project for doc intake finalization.")
        project = None

    if not project:
        await update.message.reply_text("I could not load the project to finalize documentation intake.")
        return

    answers = dict(state.get("answers") or {})
    idea_text = _intake_answers_to_idea_text(_project_display(project), answers)
    idea_count = None
    try:
        idea_count = await _project_manager.add_idea(project["id"], idea_text)
    except Exception:
        logger.exception("Failed adding documentation intake as idea.")

    wrote_docs, err = await _write_initial_project_docs(project, answers)
    if wrote_docs:
        await update.message.reply_text(
            (
                f"Initial documentation created for '{_project_display(project)}' "
                f"at {_join_project_path(project.get('local_path', ''), 'docs/product')}."
                + (f"\nCaptured as idea #{idea_count}." if idea_count else "")
            )
        )
    else:
        await update.message.reply_text(
            (
                f"I captured the documentation intake for '{_project_display(project)}'"
                + (f" as idea #{idea_count}" if idea_count else "")
                + f", but could not write docs files yet: {err}"
            )
        )


async def _start_project_documentation_intake(update: Update, project: dict) -> None:
    key = _doc_intake_key(update)
    if key is None:
        return
    _pending_project_doc_intake[key] = {
        "project_id": project["id"],
        "step_index": 0,
        "answers": {},
    }
    first_q = _PROJECT_DOC_INTAKE_STEPS[0][1]
    await update.message.reply_text(
        (
            f"Starting skynet-project-documentation intake for '{_project_display(project)}'.\n"
            "Reply naturally; I will turn this into initial docs. "
            "Say 'skip docs' any time to stop.\n\n"
            f"Q1/{len(_PROJECT_DOC_INTAKE_STEPS)}: {first_q}"
        )
    )


async def _maybe_handle_project_doc_intake(update: Update, text: str) -> bool:
    key = _doc_intake_key(update)
    if key is None:
        return False
    state = _pending_project_doc_intake.get(key)
    if not state:
        return False
    if (text or "").strip().startswith("/"):
        return False

    lowered = (text or "").strip().lower()
    if lowered in {"skip", "skip docs", "cancel docs", "stop docs", "later"}:
        _pending_project_doc_intake.pop(key, None)
        await update.message.reply_text("Skipped documentation intake. Say 'resume docs intake' to continue later.")
        return True

    idx = int(state.get("step_index", 0))
    if idx < 0 or idx >= len(_PROJECT_DOC_INTAKE_STEPS):
        _pending_project_doc_intake.pop(key, None)
        return False

    field, _question = _PROJECT_DOC_INTAKE_STEPS[idx]
    answers = dict(state.get("answers") or {})
    answers[field] = _sanitize_intake_text(
        (text or ""),
        max_chars=_DOC_INTAKE_FIELD_LIMITS.get(field, 1200),
    )
    idx += 1
    state["step_index"] = idx
    state["answers"] = answers
    _pending_project_doc_intake[key] = state

    if idx < len(_PROJECT_DOC_INTAKE_STEPS):
        next_q = _PROJECT_DOC_INTAKE_STEPS[idx][1]
        await update.message.reply_text(f"Q{idx + 1}/{len(_PROJECT_DOC_INTAKE_STEPS)}: {next_q}")
        return True

    _pending_project_doc_intake.pop(key, None)
    await update.message.reply_text("Great. Finalizing initial documentation now...")
    await _finalize_project_doc_intake(update, state)
    return True


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


def _is_explicit_new_project_request(text: str) -> bool:
    raw = (text or "").strip()
    lowered = raw.lower()
    if re.search(
        r"\b(?:new|another)\s+(?:project|application|repo|proj|app)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:create|start|begin|kick\s*off|make|spin\s*up)\s+"
        r"(?:a\s+|an\s+|the\s+|my\s+)?(?:new\s+)?"
        r"(?:project|application|repo|proj|app)\b",
        raw,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:can\s+we|let'?s|i\s+want\s+to)\s+"
        r"(?:do|create|start|begin|make)\s+"
        r"(?:a|an|the|my)\s+(?:new\s+)?(?:project|application|repo|proj|app)\b",
        raw,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _extract_project_name_candidate(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
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

    # Pause / resume / cancel
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
    if not _provider_router:
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
        "cancel_project, project_status.\n"
        "Extract fields only when present: project_name, idea_text, agent, prompt, "
        "working_dir, provider, model, path.\n"
        "Rules:\n"
        "- If user asks to create/start a new project but gives no name, use create_project with no project_name.\n"
        "- If user asks to start/resume/pause/cancel an existing project, do NOT use create_project.\n"
        "- Use recent conversation context for references like 'same project', 'it', 'that'.\n"
        "- If unsure, return {\"intent\":\"none\"}."
    )
    history = await _load_recent_conversation_messages(update, limit=8)
    llm_messages = [*history, {"role": "user", "content": raw}]
    try:
        response = await _provider_router.chat(
            llm_messages,
            system=system_prompt,
            max_tokens=220,
            task_type="general",
            allowed_providers=_CHAT_PROVIDER_ALLOWLIST,
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
    global _last_project_id
    if not _project_manager:
        return None, "Project manager is not initialized."

    projects = await _project_manager.list_projects()
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

            _last_project_id = top[0]["id"]
            return top[0], None

    # No explicit reference: use recent context first.
    if _last_project_id:
        for project in projects:
            if project["id"] == _last_project_id:
                return project, None

    ideation = [p for p in projects if p.get("status") == "ideation"]
    if len(ideation) == 1:
        _last_project_id = ideation[0]["id"]
        return ideation[0], None

    if len(projects) == 1:
        _last_project_id = projects[0]["id"]
        return projects[0], None

    active_statuses = {"planning", "approved", "coding", "testing", "paused"}
    active = [p for p in projects if p.get("status") in active_statuses]
    if len(active) == 1:
        _last_project_id = active[0]["id"]
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
    global _last_project_id
    if not _project_manager:
        return False
    if not _looks_like_implicit_idea(text):
        return False

    project = await _project_manager.get_ideation_project()
    if not project:
        return False

    try:
        count = await _project_manager.add_idea(project["id"], text)
        _last_project_id = project["id"]
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
    global _last_project_id
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
                f"Running {agent} in '{working_dir}' now.",
            )
            result = await _send_action(
                "run_coding_agent",
                {"agent": agent, "prompt": prompt, "working_dir": working_dir},
                confirmed=True,
            )
            await update.message.reply_text(_format_result(result), parse_mode="HTML")
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
                f"Switching Cline provider to {provider}" + (f" (model: {model})" if model else "") + ".",
            )
            result = await _send_action("configure_coding_agent", params, confirmed=True)
            await update.message.reply_text(_format_result(result), parse_mode="HTML")
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
                _last_project_id = project["id"]
                await update.message.reply_text(
                    f"Using existing project '{_project_display(project)}'. Share implementation details and I will proceed."
                )
            return True
        if not name:
            if not _is_explicit_new_project_request(text):
                # Likely follow-up details for current context; allow idea capture path.
                return False
            key = _pending_project_name_key(update)
            if key is not None:
                _pending_project_name_requests[key] = {"expected": "project_name"}
            await update.message.reply_text("Tell me the project name to create.")
            return True
        key = _pending_project_name_key(update)
        if key is not None:
            _pending_project_name_requests.pop(key, None)
        return await _create_project_from_name(update, name)

    if intent == "list_projects":
        try:
            projects = await _project_manager.list_projects()
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
            count = await _project_manager.add_idea(project["id"], idea_text)
            _last_project_id = project["id"]
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
                f"Generating a plan for '{_project_display(project)}' now."
            )
            plan = await _project_manager.generate_plan(project["id"])
            _last_project_id = project["id"]
            summary = (plan.get("summary") or "Plan generated.").strip()
            milestones = plan.get("milestones", []) or []
            top = [m.get("name", "").strip() for m in milestones if m.get("name")]
            top_text = ", ".join(top[:3]) if top else "No milestones listed."
            if len(top) > 3:
                top_text += f", and {len(top) - 3} more"
            if cfg.AUTO_APPROVE_AND_START:
                await _project_manager.approve_plan(project["id"])
                await _project_manager.start_execution(project["id"])
                await update.message.reply_text(
                    (
                        f"Plan generated and approved for '{_project_display(project)}'.\n"
                        f"{summary}\n\n"
                        f"Top milestones: {top_text}\n"
                        "Execution started. I will post milestone review updates."
                    ),
                )
            else:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Approve", callback_data=f"approve_plan:{project['id']}"),
                    InlineKeyboardButton("Cancel", callback_data=f"cancel_plan:{project['id']}"),
                ]])
                await update.message.reply_text(
                    f"Plan ready for '{_project_display(project)}'.\n"
                    f"{summary}\n\n"
                    f"Top milestones: {top_text}",
                    reply_markup=keyboard,
                )
        except Exception as exc:
            await update.message.reply_text(f"I couldn't generate the plan: {exc}")
        return True

    if intent in {"approve_and_start", "pause_project", "resume_project", "cancel_project", "project_status"}:
        project, error = await _resolve_project(intent_data.get("project_name"))
        if error:
            await update.message.reply_text(error)
            return True
        _last_project_id = project["id"]

        if intent == "approve_and_start":
            try:
                if project.get("status") in {"ideation", "planning"}:
                    await _project_manager.approve_plan(project["id"])
                if project.get("status") in {"planning", "approved", "ideation"}:
                    await _project_manager.start_execution(project["id"])
                    await update.message.reply_text(
                        f"Started execution for '{_project_display(project)}'."
                    )
                else:
                    await update.message.reply_text(
                        f"'{_project_display(project)}' is currently {project.get('status')}."
                    )
            except Exception as exc:
                await update.message.reply_text(f"I couldn't start execution: {exc}")
            return True

        if intent == "pause_project":
            try:
                await _project_manager.pause_project(project["id"])
                await update.message.reply_text(f"Paused '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't pause it: {exc}")
            return True

        if intent == "resume_project":
            try:
                await _project_manager.resume_project(project["id"])
                await update.message.reply_text(f"Resumed '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't resume it: {exc}")
            return True

        if intent == "cancel_project":
            try:
                await _project_manager.cancel_project(project["id"])
                await update.message.reply_text(f"Cancelled '{_project_display(project)}'.")
            except Exception as exc:
                await update.message.reply_text(f"I couldn't cancel it: {exc}")
            return True

        if intent == "project_status":
            try:
                status = await _project_manager.get_status(project["id"])
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


def _pending_project_name_key(update: Update) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    return int(user.id)


async def _create_project_from_name(update: Update, name: str) -> bool:
    global _last_project_id
    try:
        project = await _project_manager.create_project(name)
        _last_project_id = project["id"]
        repo_line = (
            f"\nGitHub: {project.get('github_repo')}"
            if project.get("github_repo") else ""
        )
        bootstrap_note = _project_bootstrap_note(project)
        if bootstrap_note:
            bootstrap_note = "\n" + bootstrap_note
        await update.message.reply_text(
            (
                f"Created project '{_project_display(project)}' at {project.get('local_path', '')}.{repo_line}\n"
                f"{bootstrap_note}\n"
                "Share details naturally. Once details are enough, I can auto-plan and execute."
            )
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
    if key is None or key not in _pending_project_name_requests:
        return False
    if (text or "").strip().startswith("/"):
        return False

    candidate = _extract_project_name_candidate(text)
    if candidate:
        _pending_project_name_requests.pop(key, None)
        previous_project_id = _last_project_id
        handled = await _create_project_from_name(update, candidate)

        # If the follow-up also contains build details, capture them as the first idea.
        new_project_id = _last_project_id
        if (
            handled
            and _project_manager is not None
            and new_project_id
            and new_project_id != previous_project_id
        ):
            idea_text = _extract_followup_idea_after_project_name(text, candidate)
            if idea_text:
                try:
                    count = await _project_manager.add_idea(new_project_id, idea_text)
                    await update.message.reply_text(
                        f"Captured that as idea #{count} for '{candidate}'.",
                    )
                except Exception:
                    logger.exception("Failed capturing follow-up idea after project-name reply.")
        return handled

    if _is_existing_project_reference_phrase(text):
        _pending_project_name_requests.pop(key, None)
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
        _pending_project_name_requests.pop(key, None)
        await update.message.reply_text("Okay, cancelled project creation.")
        return True

    intent_data = await _extract_nl_intent_hybrid(text, update=update)
    if intent_data and intent_data.get("intent") == "create_project" and intent_data.get("project_name"):
        if _is_existing_project_reference_phrase(intent_data.get("project_name", "")):
            _pending_project_name_requests.pop(key, None)
            project, error = await _resolve_project()
            if error:
                await update.message.reply_text(error)
            else:
                await update.message.reply_text(
                    f"Continuing with '{_project_display(project)}'. Share the app details and I will proceed."
                )
            return True
        _pending_project_name_requests.pop(key, None)
        return await _create_project_from_name(update, intent_data["project_name"])

    if intent_data and intent_data.get("intent") in {"help", "list_projects", "project_status"}:
        _pending_project_name_requests.pop(key, None)
        return False

    if intent_data and intent_data.get("intent") in {"run_coding_agent", "configure_coding_agent", "check_coding_agents"}:
        await update.message.reply_text(
            "I still need the project name first. Reply with the name only, or say 'cancel'.",
        )
        return True

    if not candidate:
        await update.message.reply_text(
            "Please send just the project name (example: boom-baby), or say 'cancel'.",
        )
        return True
    return True


# ------------------------------------------------------------------
# Progress callback (called by the orchestrator worker)
# ------------------------------------------------------------------

async def on_project_progress(project_id: str, event_type: str, summary: str) -> None:
    """Called by the orchestrator to send progress updates to Telegram."""
    tags = {
        "started": "[START]",
        "task_started": "[TASK]",
        "task_completed": "[DONE]",
        "milestone_started": "[MILESTONE]",
        "milestone_review": "[REVIEW]",
        "testing": "[TEST]",
        "completed": "[COMPLETE]",
        "error": "[ERROR]",
        "paused": "[PAUSE]",
        "resumed": "[RESUME]",
        "cancelled": "[CANCEL]",
    }
    tag = tags.get(event_type, "[INFO]")
    await _send_to_user(f"{tag} {html.escape(summary)}")


# ------------------------------------------------------------------
# Approval request (called by the orchestrator worker for git_push etc.)
# ------------------------------------------------------------------

async def request_worker_approval(
    project_id: str, action: str, params: dict,
) -> bool:
    """
    Called by the worker when it needs individual Telegram approval
    (e.g., for git_push, gh_create_repo).

    Sends an Approve/Deny message to Telegram and blocks until the
    user responds.
    """
    if cfg.AUTO_APPROVE_GIT_ACTIONS and action in {"git_push", "gh_create_repo"}:
        await _send_to_user(
            f"[AUTO-APPROVED] {html.escape(action)} for project {html.escape(project_id)}",
        )
        return True

    global _approval_counter
    _approval_counter += 1
    key = f"wa{_approval_counter}"

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_approvals[key] = future

    param_summary = "\n".join(f"  {k}: <code>{html.escape(str(v))}</code>" for k, v in params.items())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"wapprove:{key}"),
        InlineKeyboardButton("Deny", callback_data=f"wdeny:{key}"),
    ]])
    await _bot_app.bot.send_message(
        chat_id=cfg.ALLOWED_USER_ID,
        text=(
            f"<b>APPROVAL NEEDED</b> -- {html.escape(action)}\n"
            f"{param_summary}\n\n"
            f"Approve this action?"
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    try:
        return await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        _pending_approvals.pop(key, None)
        await _send_to_user(f"<b>TIMEOUT</b> -- {html.escape(action)} approval expired.")
        return False


# ------------------------------------------------------------------
# Callback handler for inline buttons
# ------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user or user.id != cfg.ALLOWED_USER_ID:
        await query.answer("Unauthorized.")
        return
    await query.answer()
    data = query.data or ""

    # --- v1 CONFIRM action approval ---
    if data.startswith("approve:"):
        key = data[8:]
        pending = _pending_confirms.pop(key, None)
        if not pending:
            await query.edit_message_text("Action expired or already handled.")
            return
        await query.edit_message_text(
            f"<b>APPROVED</b> -- executing {html.escape(pending['action'])} ...",
            parse_mode="HTML",
        )
        try:
            result = await _send_action(pending["action"], pending["params"], confirmed=True)
            await query.message.reply_text(_format_result(result), parse_mode="HTML")
        except Exception as exc:
            await query.message.reply_text(f"Error: {exc}")

    elif data.startswith("deny:"):
        key = data[5:]
        pending = _pending_confirms.pop(key, None)
        action_name = pending["action"] if pending else "unknown"
        await query.edit_message_text(
            f"<b>DENIED</b> -- {html.escape(action_name)} was not executed.",
            parse_mode="HTML",
        )

    # --- Worker approval (git_push, gh_create_repo) ---
    elif data.startswith("wapprove:"):
        key = data[9:]
        future = _pending_approvals.pop(key, None)
        if future and not future.done():
            future.set_result(True)
        await query.edit_message_text("<b>APPROVED</b>", parse_mode="HTML")

    elif data.startswith("wdeny:"):
        key = data[6:]
        future = _pending_approvals.pop(key, None)
        if future and not future.done():
            future.set_result(False)
        await query.edit_message_text("<b>DENIED</b>", parse_mode="HTML")

    # --- Plan approval ---
    elif data.startswith("approve_plan:"):
        project_id = data[13:]
        try:
            await _project_manager.approve_plan(project_id)
            await _project_manager.start_execution(project_id)
            await query.edit_message_text(
                "<b>Plan APPROVED</b> -- coding started!", parse_mode="HTML",
            )
        except Exception as exc:
            await query.edit_message_text(f"Error: {exc}")

    elif data.startswith("cancel_plan:"):
        project_id = data[12:]
        try:
            await _project_manager.cancel_project(project_id)
            await query.edit_message_text("<b>Plan CANCELLED</b>", parse_mode="HTML")
        except Exception as exc:
            await query.edit_message_text(f"Error: {exc}")


# ------------------------------------------------------------------
# v2 Project commands
# ------------------------------------------------------------------

async def cmd_newproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /newproject <name>\nExample: /newproject habit-tracker")
        return
    name = " ".join(context.args)
    await _create_project_from_name(update, name)


async def cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /idea <text>")
        return
    await _capture_idea(update, " ".join(context.args).strip())


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return

    # Find the project to plan.
    if context.args:
        from db import store
        project = await store.get_project_by_name(_project_manager.db, context.args[0])
    else:
        project = await _project_manager.get_ideation_project()

    if not project:
        await update.message.reply_text("No project found in ideation status. Use /newproject first.")
        return

    await update.message.reply_text(
        f"Generating plan for <b>{html.escape(project['display_name'])}</b> ...",
        parse_mode="HTML",
    )

    try:
        plan = await _project_manager.generate_plan(project["id"])
        # Format the plan for Telegram.
        milestones = plan.get("milestones", [])
        milestone_text = ""
        for i, ms in enumerate(milestones, 1):
            est = ms.get("estimated_minutes", "?")
            tasks = ms.get("tasks", [])
            task_list = "\n".join(f"    - {t.get('title', '?')}" for t in tasks)
            milestone_text += f"\n{i}. <b>{html.escape(ms.get('name', ''))}</b> (~{est} min)\n{task_list}\n"

        if cfg.AUTO_APPROVE_AND_START:
            await _project_manager.approve_plan(project["id"])
            await _project_manager.start_execution(project["id"])
            await update.message.reply_text(
                f"<b>PROJECT PLAN: {html.escape(project['display_name'])}</b>\n"
                f"{'=' * 30}\n"
                f"{html.escape(plan.get('summary', ''))}\n\n"
                f"<b>Milestones:</b>{milestone_text}\n"
                f"\n<b>Autonomous execution started.</b> "
                f"Milestone reviews and status updates will be posted automatically.",
                parse_mode="HTML",
            )
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"approve_plan:{project['id']}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel_plan:{project['id']}"),
            ]])
            await update.message.reply_text(
                f"<b>PROJECT PLAN: {html.escape(project['display_name'])}</b>\n"
                f"{'=' * 30}\n"
                f"{html.escape(plan.get('summary', ''))}\n\n"
                f"<b>Milestones:</b>{milestone_text}",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    except Exception as exc:
        await update.message.reply_text(f"Error generating plan: {exc}")


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        projects = await _project_manager.list_projects()
        if not projects:
            await update.message.reply_text("No projects yet. Use /newproject to start one.")
            return

        status_icons = {
            "ideation": "ðŸ’¡", "planning": "ðŸ“", "approved": "âœ…",
            "coding": "âš™ï¸", "testing": "ðŸ§ª", "completed": "ðŸŽ‰",
            "paused": "â¸ï¸", "failed": "âŒ", "cancelled": "ðŸ›‘",
        }
        lines = ["<b>Projects:</b>\n"]
        for p in projects:
            icon = status_icons.get(p["status"], "ðŸ“‹")
            lines.append(
                f"{icon} <b>{html.escape(p['display_name'])}</b> â€” {p['status']}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_project_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /status <project-name>")
        return

    from db import store
    project = await store.get_project_by_name(_project_manager.db, context.args[0])
    if not project:
        # Fall back to agent status if not a project name.
        try:
            result = await _gateway_get("/status")
            connected = result.get("agent_connected", False)
            icon = "CONNECTED" if connected else "NOT CONNECTED"
            await update.message.reply_text(f"Agent: <b>{icon}</b>", parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"Gateway unreachable: {exc}")
        return

    try:
        status = await _project_manager.get_status(project["id"])
        p = status["project"]
        lines = [
            f"<b>{html.escape(p['display_name'])}</b>",
            f"Status: {p['status']}",
            f"Progress: {status['progress']} ({status['percent']}%)",
        ]
        if status["current_task"]:
            lines.append(f"Current: {html.escape(status['current_task'])}")
        if p.get("github_repo"):
            lines.append(f"GitHub: {html.escape(p['github_repo'])}")
        if status["recent_events"]:
            lines.append("\n<b>Recent:</b>")
            for e in status["recent_events"][:5]:
                lines.append(f"  {html.escape(e['summary'])}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /pause <project-name>")
        return
    from db import store
    project = await store.get_project_by_name(_project_manager.db, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    try:
        await _project_manager.pause_project(project["id"])
        await update.message.reply_text(f"Paused: <b>{html.escape(project['display_name'])}</b>", parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_resume_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /resume_project <project-name>")
        return
    from db import store
    project = await store.get_project_by_name(_project_manager.db, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    try:
        await _project_manager.resume_project(project["id"])
        await update.message.reply_text(f"Resumed: <b>{html.escape(project['display_name'])}</b>", parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancel <project-name>")
        return
    from db import store
    project = await store.get_project_by_name(_project_manager.db, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    try:
        await _project_manager.cancel_project(project["id"])
        await update.message.reply_text(f"Cancelled: <b>{html.escape(project['display_name'])}</b>", parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not _provider_router:
        await update.message.reply_text("AI providers not configured.")
        return
    try:
        summary = await _provider_router.get_quota_summary()
        lines = ["<b>AI Provider Quota:</b>\n"]
        for p in summary:
            status = "âœ…" if p["available"] else "âŒ"
            limit = p["daily_limit"] or "âˆž"
            lines.append(
                f"{status} <b>{html.escape(p['provider'])}</b> ({p['model']})\n"
                f"    {p['daily_used']}/{limit} requests today"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ------------------------------------------------------------------
# Persona memory commands
# ------------------------------------------------------------------

async def _forget_profile_target(update: Update, target: str) -> str:
    if _project_manager is None:
        return "Profile store is not available."
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return "Profile store is not available."

    from db import store

    user_id = int(user_row["id"])
    removed = await store.forget_profile_facts(
        _project_manager.db,
        user_id=user_id,
        key_or_text=target,
    )
    await store.add_memory_audit_log(
        _project_manager.db,
        user_id=user_id,
        action="forget",
        target_type="fact",
        target_key=target,
        detail=f"Removed facts: {removed}",
    )
    if removed <= 0:
        return f"No stored facts matched '{target}'."
    return f"Forgot {removed} fact(s) matching '{target}'."


async def _set_memory_enabled_for_user(update: Update, enabled: bool, *, reason: str) -> str:
    if _project_manager is None:
        return "Profile store is not available."
    user_row = await _ensure_memory_user(update)
    if not user_row:
        return "Profile store is not available."

    from db import store

    user_id = int(user_row["id"])
    await store.set_user_memory_enabled(_project_manager.db, user_id=user_id, enabled=enabled)
    await store.add_memory_audit_log(
        _project_manager.db,
        user_id=user_id,
        action="memory_enabled" if enabled else "memory_disabled",
        target_type="policy",
        target_key="memory_enabled",
        detail=reason,
    )
    if enabled:
        return "Memory capture enabled."
    return "Memory capture disabled for this user. Use /store_on to re-enable."


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        summary = await _format_profile_summary(update)
        await update.message.reply_text(summary, parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /forget <fact key or text>")
        return
    target = " ".join(context.args).strip()
    try:
        await update.message.reply_text(await _forget_profile_target(update, target))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_no_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        await update.message.reply_text(
            await _set_memory_enabled_for_user(
                update,
                enabled=False,
                reason="Disabled by user command.",
            )
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_store_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        await update.message.reply_text(
            await _set_memory_enabled_for_user(
                update,
                enabled=True,
                reason="Enabled by user command.",
            )
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _maybe_handle_memory_text_command(update: Update, text: str) -> bool:
    lowered = text.strip().lower()

    if lowered in {"show my profile", "show profile", "what do you know about me"}:
        summary = await _format_profile_summary(update)
        await update.message.reply_text(summary, parse_mode="HTML")
        return True

    if lowered.startswith("forget "):
        target = text.strip()[7:].strip()
        if target:
            await update.message.reply_text(await _forget_profile_target(update, target))
            return True

    if _is_no_store_chat_message(text):
        await update.message.reply_text(
            await _set_memory_enabled_for_user(
                update,
                enabled=False,
                reason="Disabled by natural-language request.",
            )
        )
        return True

    return False


# ------------------------------------------------------------------
# v1 Agent commands (kept as-is)
# ------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await update.message.reply_text(
        "<b>SKYNET // CHATHAN - AI Project Factory</b>\n\n"
        "<b>Project Management:</b>\n"
        "  /newproject &lt;name&gt; - start a new project\n"
        "  (send text) - natural chat with SKYNET\n"
        "  /idea &lt;text&gt; - add idea to current project\n"
        "  /plan [name] - generate project plan\n"
        "  /projects - list all projects\n"
        "  /status &lt;name&gt; - project status\n"
        "  /pause &lt;name&gt; - pause project\n"
        "  /resume_project &lt;name&gt; - resume project\n"
        "  /cancel &lt;name&gt; - cancel project\n"
        "  /quota - AI provider status\n\n"
        "<b>Persona Memory:</b>\n"
        "  /profile - show stored profile and preferences\n"
        "  /forget &lt;fact-or-text&gt; - forget matching stored facts\n"
        "  /no_store - stop storing new memory\n"
        "  /store_on - re-enable memory storage\n\n"
        "<b>SKYNET System:</b>\n"
        "  /agents [project] - list agents\n"
        "  /heartbeat - heartbeat task status\n"
        "  /sentinel - run health checks\n"
        "  /skills - list available skills\n\n"
        "<b>Agent Commands:</b>\n"
        "  /agent_status - agent connection check\n"
        "  /git_status [path]\n"
        "  /run_tests [path]\n"
        "  /lint [path]\n"
        "  /build [path]\n"
        "  /vscode <path> - open folder/file in VS Code on laptop\n"
        "  /check_agents - check codex/claude/cline CLI availability\n"
        "  /run_agent <agent> [path=<dir>] <prompt> - run coding agent\n"
        "  /cline_provider <provider> [model] - switch Cline provider/model\n"
        "  /close_app [name]\n\n"
        "<b>Controls:</b>\n"
        "  /emergency_stop - kill everything\n"
        "  /resume - resume agent\n",
        parse_mode="HTML",
    )


async def cmd_agent_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        result = await _gateway_get("/status")
        execution_mode = str(result.get("execution_mode", "")).strip().lower()
        if execution_mode == "ssh_tunnel":
            ssh_enabled = result.get("ssh_fallback_enabled", False)
            ssh_healthy = result.get("ssh_fallback_healthy", False)
            ssh_target = result.get("ssh_fallback_target", "")
            if ssh_enabled:
                status = "SSH Tunnel Ready" if ssh_healthy else "SSH Tunnel Configured (unreachable)"
                msg = f"Execution: <b>{status}</b>\nMode: <code>ssh_tunnel (forced)</code>"
                if ssh_target:
                    msg += f"\nTarget: <code>{html.escape(str(ssh_target))}</code>"
                await update.message.reply_text(msg, parse_mode="HTML")
                return

        connected = result.get("agent_connected", False)
        if connected:
            await update.message.reply_text("Execution: <b>Worker Connected</b>", parse_mode="HTML")
            return

        ssh_enabled = result.get("ssh_fallback_enabled", False)
        ssh_healthy = result.get("ssh_fallback_healthy", False)
        ssh_target = result.get("ssh_fallback_target", "")
        if ssh_enabled:
            status = "SSH Tunnel Ready" if ssh_healthy else "SSH Tunnel Configured (unreachable)"
            msg = f"Execution: <b>{status}</b>"
            if ssh_target:
                msg += f"\nTarget: <code>{html.escape(str(ssh_target))}</code>"
            await update.message.reply_text(msg, parse_mode="HTML")
            return

        await update.message.reply_text("Execution: <b>No worker and no SSH fallback</b>", parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Gateway unreachable: {exc}")


async def cmd_git_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    path = _parse_path(context.args)
    await update.message.reply_text(f"Running git_status on <code>{html.escape(path)}</code> ...", parse_mode="HTML")
    try:
        result = await _send_action("git_status", {"working_dir": path}, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_run_tests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    path = _parse_path(context.args)
    runner = context.args[1] if context.args and len(context.args) > 1 else "pytest"
    await update.message.reply_text(f"Running tests ({runner}) ...", parse_mode="HTML")
    try:
        result = await _send_action("run_tests", {"working_dir": path, "runner": runner}, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_lint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    path = _parse_path(context.args)
    linter = context.args[1] if context.args and len(context.args) > 1 else "ruff"
    try:
        result = await _send_action("lint_project", {"working_dir": path, "linter": linter}, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    path = _parse_path(context.args)
    tool = context.args[1] if context.args and len(context.args) > 1 else "npm"
    try:
        result = await _send_action("build_project", {"working_dir": path, "build_tool": tool}, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_vscode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /vscode <path>")
        return
    path = " ".join(context.args).strip()
    await _ask_confirm(
        update,
        "open_in_vscode",
        {"path": path},
        f"Path: <code>{html.escape(path)}</code>",
    )


async def cmd_check_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        result = await _send_action("check_coding_agents", {}, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_run_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /run_agent <codex|claude|cline> [path=<dir>] <prompt>",
        )
        return

    agent = context.args[0].strip().lower()
    if agent not in {"codex", "claude", "cline"}:
        await update.message.reply_text("Agent must be one of: codex, claude, cline")
        return

    working_dir = cfg.PROJECT_BASE_DIR or cfg.DEFAULT_WORKING_DIR
    prompt_start_index = 1
    if len(context.args) >= 3 and context.args[1].startswith("path="):
        working_dir = context.args[1][len("path="):].strip() or working_dir
        prompt_start_index = 2

    prompt = " ".join(context.args[prompt_start_index:]).strip()
    if not prompt:
        await update.message.reply_text(
            "Usage: /run_agent <codex|claude|cline> [path=<dir>] <prompt>",
        )
        return

    await _ask_confirm(
        update,
        "run_coding_agent",
        {"agent": agent, "prompt": prompt, "working_dir": working_dir},
        (
            f"Agent: <code>{html.escape(agent)}</code>\n"
            f"Path: <code>{html.escape(working_dir)}</code>\n"
            f"Prompt: <i>{html.escape(prompt)}</i>"
        ),
    )


async def cmd_cline_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /cline_provider <gemini|deepseek|groq|openrouter|openai|anthropic> [model]",
        )
        return
    provider = context.args[0].strip().lower()
    if provider not in {"gemini", "deepseek", "groq", "openrouter", "openai", "anthropic"}:
        await update.message.reply_text(
            "Provider must be one of: gemini, deepseek, groq, openrouter, openai, anthropic.",
        )
        return
    model = " ".join(context.args[1:]).strip()
    params = {"agent": "cline", "provider": provider}
    if model:
        params["model"] = model
    try:
        result = await _send_action("configure_coding_agent", params, confirmed=True)
        await update.message.reply_text(_format_result(result), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_git_commit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(f"Usage: /git_commit [path] [message]")
        return
    path = context.args[0]
    message = " ".join(context.args[1:])
    await _ask_confirm(update, "git_commit", {"working_dir": path, "message": message},
                       f"Path: <code>{html.escape(path)}</code>\nMessage: <i>{html.escape(message)}</i>")


async def cmd_install_deps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    path = _parse_path(context.args)
    manager = context.args[1] if context.args and len(context.args) > 1 else "pip"
    await _ask_confirm(update, "install_dependencies", {"working_dir": path, "manager": manager},
                       f"Path: <code>{html.escape(path)}</code>\nManager: {html.escape(manager)}")


async def cmd_close_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /close_app [name]")
        return
    app_name = context.args[0].lower()
    await _ask_confirm(update, "close_app", {"app": app_name},
                       f"Application: <code>{html.escape(app_name)}</code>")


async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    # Cancel all running projects.
    if _project_manager and _project_manager.scheduler:
        count = _project_manager.scheduler.cancel_all()
        if count:
            await update.message.reply_text(f"Cancelled {count} running project(s).")
    try:
        result = await _gateway_post("/emergency-stop")
        await update.message.reply_text(
            f"EMERGENCY STOP sent.\nResponse: <code>{html.escape(json.dumps(result))}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        result = await _gateway_post("/resume")
        await update.message.reply_text(
            f"Resume sent.\nResponse: <code>{html.escape(json.dumps(result))}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ------------------------------------------------------------------
# SKYNET system commands
# ------------------------------------------------------------------

async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        from db import store
        from agents.roles import AGENT_CONFIGS
        if context.args:
            project = await store.get_project_by_name(_project_manager.db, context.args[0])
            if not project:
                await update.message.reply_text("Project not found.")
                return
            agents = await store.list_agents(_project_manager.db, project["id"])
            if not agents:
                await update.message.reply_text("No agents spawned for this project yet.")
                return
            lines = [f"<b>Agents for {html.escape(project['display_name'])}:</b>\n"]
            for a in agents:
                lines.append(
                    f"  {a['role']} â€” {a['status']} "
                    f"({a.get('tasks_completed', 0)} tasks)"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        else:
            lines = ["<b>Available Agent Roles:</b>\n"]
            for role, cfg_data in AGENT_CONFIGS.items():
                lines.append(f"  <b>{role}</b> â€” {html.escape(cfg_data['description'])}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not _heartbeat:
        await update.message.reply_text("Heartbeat scheduler not configured.")
        return
    status = _heartbeat.get_status()
    if not status:
        await update.message.reply_text("No heartbeat tasks registered.")
        return
    lines = [
        f"<b>SKYNET Heartbeat</b> ({'running' if _heartbeat.is_running else 'stopped'})\n",
    ]
    for t in status:
        enabled = "ON" if t["enabled"] else "OFF"
        next_in = int(t.get("next_run_in", 0))
        lines.append(
            f"  [{enabled}] <b>{html.escape(t['name'])}</b>\n"
            f"    {html.escape(t['description'])}\n"
            f"    Every {t['interval_seconds']}s | Runs: {t['run_count']} | Next: {next_in}s"
        )
        if t.get("last_error"):
            lines.append(f"    Last error: {html.escape(t['last_error'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_sentinel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    if not _sentinel:
        await update.message.reply_text("Sentinel not configured.")
        return
    await update.message.reply_text("Running SKYNET Sentinel health checks...")
    try:
        statuses = await _sentinel.run_all_checks()
        report = _sentinel.format_report(statuses)
        await update.message.reply_text(
            f"<pre>{html.escape(report)}</pre>", parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Sentinel error: {exc}")


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        if not _skill_registry:
            await update.message.reply_text("Skill registry is not configured.")
            return
        rows = _skill_registry.list_skills()
        if not rows:
            await update.message.reply_text("No skills are currently loaded.")
            return

        lines = ["<b>SKYNET Skills:</b>\n"]
        for row in sorted(rows, key=lambda r: (r.get("kind", "tool"), r["name"])):
            kind = row.get("kind", "tool")
            roles = ", ".join(row.get("allowed_roles", ["all"]))
            description = row.get("description", "")
            if kind == "prompt":
                src = row.get("source", "")
                lines.append(
                    f"  <b>{html.escape(row['name'])}</b> - {html.escape(description)}\n"
                    f"    Kind: prompt-only | Roles: {html.escape(roles)}\n"
                    f"    Source: <code>{html.escape(src)}</code>"
                )
            else:
                lines.append(
                    f"  <b>{html.escape(row['name'])}</b> - {html.escape(description)}\n"
                    f"    Kind: tools | Roles: {html.escape(roles)}"
                )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ------------------------------------------------------------------
# Plain text handler â€” natural conversation + intent extraction
# ------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    text = update.message.text.strip()
    if not text:
        return

    if await _maybe_handle_memory_text_command(update, text):
        return

    skip_store = _is_no_store_once_message(text)
    await _capture_profile_memory(update, text, skip_store=skip_store)

    if await _maybe_handle_pending_project_name(update, text):
        return

    if await _maybe_handle_project_doc_intake(update, text):
        return

    # Keep greetings/acks deterministic and out of tool execution flows.
    if _is_smalltalk_or_ack(text):
        reply = await _smalltalk_reply_with_context(update, text)
        await update.message.reply_text(reply)
        await _append_user_conversation(
            update,
            role="assistant",
            content=reply,
            metadata={"channel": "smalltalk"},
        )
        return

    # Optional explicit idea prefix while in chat mode.
    if text.lower().startswith("idea:"):
        idea_text = text[5:].strip()
        if not idea_text:
            await update.message.reply_text("Usage: idea: <text>")
            return
        await _capture_idea(update, idea_text)
        return

    if await _handle_natural_action(update, text):
        return

    if await _maybe_capture_implicit_idea(update, text):
        return

    await _reply_with_openclaw_capabilities(update, text)


# ------------------------------------------------------------------
# Build the Telegram Application
# ------------------------------------------------------------------

def build_app() -> Application:
    """Create and configure the Telegram bot application."""
    global _bot_app

    app = Application.builder().token(cfg.TELEGRAM_BOT_TOKEN).build()

    # v2 project commands.
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("newproject", cmd_newproject))
    app.add_handler(CommandHandler("idea", cmd_idea))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("status", cmd_project_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume_project", cmd_resume_project))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("quota", cmd_quota))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("no_store", cmd_no_store))
    app.add_handler(CommandHandler("store_on", cmd_store_on))

    # SKYNET system commands.
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CommandHandler("sentinel", cmd_sentinel))
    app.add_handler(CommandHandler("skills", cmd_skills))

    # v1 agent commands.
    app.add_handler(CommandHandler("agent_status", cmd_agent_status))
    app.add_handler(CommandHandler("git_status", cmd_git_status))
    app.add_handler(CommandHandler("run_tests", cmd_run_tests))
    app.add_handler(CommandHandler("lint", cmd_lint))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("vscode", cmd_vscode))
    app.add_handler(CommandHandler("check_agents", cmd_check_agents))
    app.add_handler(CommandHandler("run_agent", cmd_run_agent))
    app.add_handler(CommandHandler("cline_provider", cmd_cline_provider))
    app.add_handler(CommandHandler("git_commit", cmd_git_commit))
    app.add_handler(CommandHandler("install_deps", cmd_install_deps))
    app.add_handler(CommandHandler("close_app", cmd_close_app))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))

    # Inline buttons.
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Plain text â†’ idea capture.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    _bot_app = app
    return app




