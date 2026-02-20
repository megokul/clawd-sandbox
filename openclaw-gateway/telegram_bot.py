"""
SKYNET Gateway — Telegram Bot

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

    history = _chat_history[-(_CHAT_HISTORY_MAX * 2):]
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

    history = _chat_history[-(_CHAT_HISTORY_MAX * 2):]
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
            r"(hi|hello|hey|yo|sup|thanks|thank you|ok|okay|cool|great|nice|got it|understood)[.!? ]*",
            lowered,
        ),
    )


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
        if _is_plausible_project_name(candidate):
            return candidate
    return ""


def _extract_project_name_candidate(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted_name = _extract_quoted_project_name_candidate(raw)
    if quoted_name:
        return quoted_name

    # For follow-up name replies, prefer short plain phrases.
    if any(ch in raw for ch in ".!?;\n"):
        return ""
    name = _clean_entity(raw)
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
    ):
        return {"intent": "create_project"}

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
    intent_data = _extract_nl_intent(text)
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
        if not name:
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

    intent_data = _extract_nl_intent(text)
    if intent_data:
        # User provided a full create instruction as follow-up.
        if intent_data.get("intent") == "create_project" and intent_data.get("project_name"):
            _pending_project_name_requests.pop(key, None)
            return await _create_project_from_name(update, intent_data["project_name"])
        # Different intent: clear pending and let normal intent path handle it.
        _pending_project_name_requests.pop(key, None)
        return False

    candidate = _extract_project_name_candidate(text)
    if not candidate:
        await update.message.reply_text(
            "Please send just the project name (example: boom-baby).",
        )
        return True

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
    try:
        project = await _project_manager.create_project(name)
        repo_line = (
            f"\n<b>GitHub:</b> {html.escape(project.get('github_repo', ''))}"
            if project.get("github_repo") else ""
        )
        bootstrap_note_raw = _project_bootstrap_note(project)
        bootstrap_line = (
            f"\n\n<b>Bootstrap:</b>\n{html.escape(bootstrap_note_raw)}"
            if bootstrap_note_raw else ""
        )
        await update.message.reply_text(
            f"<b>Project created:</b> {html.escape(project['display_name'])}\n\n"
            f"<b>Path:</b> <code>{html.escape(project.get('local_path', ''))}</code>"
            f"{repo_line}{bootstrap_line}\n\n"
            f"Send ideas as text messages.\n"
            f"I can auto-plan/start once enough details are captured "
            f"(threshold: {cfg.AUTO_PLAN_MIN_IDEAS} ideas).",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


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
            "ideation": "💡", "planning": "📝", "approved": "✅",
            "coding": "⚙️", "testing": "🧪", "completed": "🎉",
            "paused": "⏸️", "failed": "❌", "cancelled": "🛑",
        }
        lines = ["<b>Projects:</b>\n"]
        for p in projects:
            icon = status_icons.get(p["status"], "📋")
            lines.append(
                f"{icon} <b>{html.escape(p['display_name'])}</b> — {p['status']}"
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
            status = "✅" if p["available"] else "❌"
            limit = p["daily_limit"] or "∞"
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
                    f"  {a['role']} — {a['status']} "
                    f"({a.get('tasks_completed', 0)} tasks)"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        else:
            lines = ["<b>Available Agent Roles:</b>\n"]
            for role, cfg_data in AGENT_CONFIGS.items():
                lines.append(f"  <b>{role}</b> — {html.escape(cfg_data['description'])}")
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
# Plain text handler — natural conversation + intent extraction
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

    # Plain text → idea capture.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    _bot_app = app
    return app



