"""
bot/helpers.py -- HTTP/gateway wrappers, formatting, confirm/approval utilities,
                  background task runner, and proactive-message helper.
"""
from __future__ import annotations

import ast
import asyncio
import html
import json
import logging
import re
import time
import uuid
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

import bot_config as cfg
from ai.providers.base import ToolCall
from . import state

logger = logging.getLogger("skynet.telegram")


def _authorised(update: Update) -> bool:
    user = update.effective_user
    if user and user.id == cfg.ALLOWED_USER_ID:
        return True
    logger.warning("Rejected message from user %s", user.id if user else "unknown")
    return False



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
    state._confirm_counter += 1
    key = f"c{state._confirm_counter}"
    state._pending_confirms[key] = {"action": action, "params": params}
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


def _store_pending_project_removal(project: dict[str, Any]) -> str:
    key = f"rp{uuid.uuid4().hex[:10]}"
    state._pending_project_removals[key] = {
        "project_id": str(project.get("id", "")),
        "display_name": _project_display(project),
    }
    if project.get("local_path"):
        state._pending_project_removals[key]["local_path"] = str(project["local_path"])
    return key


async def _build_project_context_block() -> str:
    """Build a short context string about the last worked-on project for the LLM system prompt."""
    if not state._project_manager or not state._last_project_id:
        return ""
    try:
        from db import store
        project = await store.get_project(state._project_manager.db, state._last_project_id)
        if not project:
            return ""
        name = _project_display(project)
        status = str(project.get("status") or "unknown")
        ideas = project.get("ideas") or []
        idea_count = len(ideas) if isinstance(ideas, list) else "?"
        return (
            f"\n\n## Last worked on: {name}\n"
            f"Status: {status} | Ideas captured: {idea_count}\n"
            "IMPORTANT: Do NOT assume the user wants to continue this project. "
            "If they say 'start a project', 'new project', 'create a project', or anything suggesting "
            "they want to build something new — ask for the new project name and call project_create. "
            "Only reference this project if the user clearly continues its conversation."
        )
    except Exception:
        return ""


def _truncate_for_notice(value: str, *, max_chars: int = 700) -> str:
    text = (value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ..."


def _format_notification(level: str, title: str, body: str, *, project: str = "") -> str:
    theme_map: dict[str, tuple[str, str, str]] = {
        "info": ("🔵", "INFO", "BLUE"),
        "progress": ("🟣", "IN_PROGRESS", "PURPLE"),
        "success": ("🟢", "SUCCESS", "GREEN"),
        "warning": ("🟠", "WARNING", "ORANGE"),
        "error": ("🔴", "ERROR", "RED"),
    }
    accent, label, theme = theme_map.get(level, ("🔵", "INFO", "BLUE"))
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [
        f"<b>{accent} {html.escape(label)} | SKYNET STATUS</b>",
        f"<b>{html.escape(title)}</b>",
        f"<code>theme={html.escape(theme)} | time={html.escape(ts)}</code>",
    ]
    if project:
        lines.append(f"<code>project={html.escape(project)}</code>")
    lines.append("")
    lines.append(f"{accent} {html.escape(_truncate_for_notice(body, max_chars=1800))}")
    return "\n".join(lines)


async def _notify_styled(level: str, title: str, body: str, *, project: str = "") -> None:
    await _send_to_user(_format_notification(level, title, body, project=project), parse_mode="HTML")


async def _run_gateway_action_in_background(
    *,
    action: str,
    params: dict[str, str],
    title: str,
    project: str = "",
) -> None:
    await _notify_styled(
        "progress",
        title,
        f"Started background execution for action '{action}'.",
        project=project,
    )
    try:
        result = await _send_action(action, params, confirmed=True)
        if str(result.get("status", "")).lower() == "error":
            err = str(result.get("error") or "Unknown gateway error")
            await _notify_styled("error", title, f"Action '{action}' failed: {err}", project=project)
            return

        inner = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
        rc = inner.get("returncode", "?")
        stdout = _truncate_for_notice(str(inner.get("stdout", "")).strip(), max_chars=650)
        stderr = _truncate_for_notice(str(inner.get("stderr", "")).strip(), max_chars=500)
        summary_lines = [f"Action: {action}", f"Exit code: {rc}"]
        if stdout:
            summary_lines.append(f"stdout: {stdout}")
        if stderr:
            summary_lines.append(f"stderr: {stderr}")
        await _notify_styled("success", title, "\n".join(summary_lines), project=project)
    except Exception as exc:
        await _notify_styled("error", title, f"Action '{action}' raised: {exc}", project=project)


async def _ask_remove_project_confirmation(update: Update, project: dict[str, Any]) -> None:
    key = _store_pending_project_removal(project)
    display = html.escape(_project_display(project))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=f"confirm_remove_project:{key}"),
        InlineKeyboardButton("No", callback_data=f"cancel_remove_project:{key}"),
    ]])
    await update.message.reply_text(
        (
            f"Remove project <b>{display}</b> permanently from SKYNET records?\n"
            "This deletes its tasks/ideas/plans/history from the DB. "
            "Workspace files are not deleted."
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _send_remove_project_confirmation(project: dict[str, Any]) -> None:
    """Send a remove-project confirmation via proactive message (no update object needed)."""
    key = _store_pending_project_removal(project)
    display = html.escape(_project_display(project))
    from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
    keyboard = IKM([[
        IKB("Yes", callback_data=f"confirm_remove_project:{key}"),
        IKB("No", callback_data=f"cancel_remove_project:{key}"),
    ]])
    text = (
        f"Remove project <b>{display}</b> permanently from SKYNET records?\n"
        "This deletes its tasks/ideas/plans/history from the DB. "
        "Workspace files are not deleted."
    )
    if state._bot_app and state._bot_app.bot:
        try:
            await state._bot_app.bot.send_message(
                chat_id=cfg.ALLOWED_USER_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.warning("Failed to send remove confirmation: %s", exc)


async def _send_to_user(text: str, parse_mode: str = "HTML") -> None:
    """Send a proactive message to the authorised user."""
    if state._bot_app and state._bot_app.bot:
        try:
            await state._bot_app.bot.send_message(
                chat_id=cfg.ALLOWED_USER_ID, text=text, parse_mode=parse_mode,
            )
        except Exception as exc:
            logger.warning("Failed to send proactive message: %s", exc)

def _trim_chat_history() -> None:
    """Keep only the most recent conversation turns in memory."""
    max_items = state._CHAT_HISTORY_MAX * 2
    if len(state._chat_history) > max_items:
        state._chat_history = state._chat_history[-max_items:]


def _spawn_background_task(coro, *, tag: str) -> None:
    """Run a coroutine in background and surface failures in logs."""
    task = asyncio.create_task(coro, name=tag)
    state._background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        state._background_tasks.discard(t)
        try:
            t.result()
        except Exception:
            logger.exception("Background task failed: %s", tag)
            if not tag.endswith("-notify-failure"):
                _spawn_background_task(
                    _notify_styled(
                        "error",
                        "Background Task Failure",
                        f"Task '{tag}' failed. Check gateway logs for details.",
                    ),
                    tag=f"{tag}-notify-failure",
                )

    task.add_done_callback(_done)



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
    provider = (getattr(response, "provider_name", "") or "").strip()
    model = (getattr(response, "model", "") or "").strip()
    if not provider and not model:
        return

    signature = f"{provider}:{model}"
    if state._last_model_signature and signature != state._last_model_signature:
        await update.message.reply_text(
            f"Note: switched model to {model} ({provider}) based on availability.",
        )
    state._last_model_signature = signature


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



def _norm_project(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _project_display(project: dict) -> str:
    return str(project.get("display_name") or project.get("name") or "project")


def _project_bootstrap_note(project: dict) -> str:
    summary = str(project.get("bootstrap_summary") or "").strip()
    if not summary:
        return ""
    bootstrap_ok = project.get("bootstrap_ok", True)
    lowered = summary.lower()
    # Use bootstrap_ok flag (not text search) so "SSH action failed" inside a
    # deferred summary doesn't trigger the hard-failure branch.
    if bootstrap_ok and "deferred" in lowered:
        return (
            "Workspace bootstrap deferred — the agent is currently unreachable.\n"
            "The directory and git scaffold will be created automatically when the agent reconnects."
        )
    if not bootstrap_ok:
        return (
            f"Bootstrap issue: {summary}\n"
            "Project record was created, but workspace setup did not fully complete."
        )
    if "warning" in lowered or "failed" in lowered:
        return f"Bootstrap note: {summary}"
    return ""


def _join_project_path(base: str, leaf: str) -> str:
    sep = "\\" if ("\\" in base or ":" in base) else "/"
    return base.rstrip("\\/") + sep + leaf.strip("\\/")


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




