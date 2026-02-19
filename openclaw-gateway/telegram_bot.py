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
import json
import logging
import html

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

import bot_config as cfg

logger = logging.getLogger("skynet.telegram")

# Injected at startup by main.py.
_project_manager = None
_provider_router = None
_heartbeat = None
_sentinel = None

# Stores pending CONFIRM actions keyed by a short ID.
_pending_confirms: dict[str, dict] = {}
_confirm_counter: int = 0

# Stores pending approval futures from the orchestrator worker.
# { "key": asyncio.Future }
_pending_approvals: dict[str, asyncio.Future] = {}
_approval_counter: int = 0

# Reference to the Telegram app for sending proactive messages.
_bot_app: Application | None = None

# Short rolling chat history for natural Telegram conversation.
_chat_history: list[dict[str, str]] = []
_CHAT_HISTORY_MAX: int = 12
_CHAT_SYSTEM_PROMPT = (
    "You are SKYNET's Telegram assistant. Reply naturally in plain language. "
    "Be concise, helpful, and ask clarifying questions when needed. "
    "Do not output JSON unless the user explicitly asks for it."
)


def set_dependencies(project_manager, provider_router, heartbeat=None, sentinel=None):
    """Called by main.py to inject dependencies."""
    global _project_manager, _provider_router, _heartbeat, _sentinel
    _project_manager = project_manager
    _provider_router = provider_router
    _heartbeat = heartbeat
    _sentinel = sentinel


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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
    return cfg.DEFAULT_WORKING_DIR


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


async def _reply_naturally(update: Update, text: str) -> None:
    """Reply to plain text as a normal AI conversation."""
    if not _provider_router:
        await update.message.reply_text("AI providers not configured.")
        return

    history = _chat_history[-(_CHAT_HISTORY_MAX * 2):]
    messages = [*history, {"role": "user", "content": text}]
    try:
        response = await _provider_router.chat(
            messages,
            system=_CHAT_SYSTEM_PROMPT,
            max_tokens=600,
            task_type="general",
        )
    except Exception as exc:
        await update.message.reply_text(f"AI unavailable: {exc}")
        return

    reply = (response.text or "").strip()
    if not reply:
        reply = "I could not generate a reply right now."
    if len(reply) > 3800:
        reply = reply[:3800] + "\n\n... (truncated)"

    _chat_history.append({"role": "user", "content": text})
    _chat_history.append({"role": "assistant", "content": reply})
    _trim_chat_history()

    await update.message.reply_text(reply)


async def _capture_idea(update: Update, text: str) -> None:
    """Save one idea into the active ideation project."""
    if not _project_manager:
        await update.message.reply_text("Project manager not initialized.")
        return

    project = await _project_manager.get_ideation_project()
    if not project:
        await update.message.reply_text(
            "No project in ideation mode.\n"
            "Use /newproject <name> first, then send ideas or use /idea.",
        )
        return

    try:
        count = await _project_manager.add_idea(project["id"], text)
        await update.message.reply_text(
            f"Added idea #{count} to <b>{html.escape(project['display_name'])}</b>.\n"
            f"Send more or use /plan to generate the plan.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ------------------------------------------------------------------
# Progress callback (called by the orchestrator worker)
# ------------------------------------------------------------------

async def on_project_progress(project_id: str, event_type: str, summary: str) -> None:
    """Called by the orchestrator to send progress updates to Telegram."""
    icons = {
        "started": "🚀", "task_started": "⚙️", "task_completed": "✅",
        "testing": "🧪", "completed": "🎉", "error": "❌",
        "paused": "⏸️", "resumed": "▶️", "cancelled": "🛑",
    }
    icon = icons.get(event_type, "📋")
    await _send_to_user(f"{icon} {html.escape(summary)}")


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
        await update.message.reply_text(
            f"<b>Project created:</b> {html.escape(project['display_name'])}\n\n"
            f"Send me your ideas as text messages.\n"
            f"When done, use /plan to generate the project plan.",
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
# v1 Agent commands (kept as-is)
# ------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await update.message.reply_text(
        "<b>SKYNET // CHATHAN — AI Project Factory</b>\n\n"
        "<b>Project Management:</b>\n"
        "  /newproject &lt;name&gt; — start a new project\n"
        "  (send text) — natural chat with SKYNET\n"
        "  /idea &lt;text&gt; — add idea to current project\n"
        "  /plan [name] — generate project plan\n"
        "  /projects — list all projects\n"
        "  /status &lt;name&gt; — project status\n"
        "  /pause &lt;name&gt; — pause project\n"
        "  /resume_project &lt;name&gt; — resume project\n"
        "  /cancel &lt;name&gt; — cancel project\n"
        "  /quota — AI provider status\n\n"
        "<b>SKYNET System:</b>\n"
        "  /agents [project] — list agents\n"
        "  /heartbeat — heartbeat task status\n"
        "  /sentinel — run health checks\n"
        "  /skills — list available skills\n\n"
        "<b>Agent Commands:</b>\n"
        "  /agent_status — agent connection check\n"
        "  /git_status [path]\n"
        "  /run_tests [path]\n"
        "  /lint [path]\n"
        "  /build [path]\n"
        "  /close_app [name]\n\n"
        "<b>Controls:</b>\n"
        "  /emergency_stop — kill everything\n"
        "  /resume — resume agent\n",
        parse_mode="HTML",
    )


async def cmd_agent_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    try:
        result = await _gateway_get("/status")
        connected = result.get("agent_connected", False)
        icon = "CONNECTED" if connected else "NOT CONNECTED"
        await update.message.reply_text(f"Agent: <b>{icon}</b>", parse_mode="HTML")
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
        from skills.registry import build_default_registry
        registry = build_default_registry()
        lines = ["<b>SKYNET Skills:</b>\n"]
        for name, skill in sorted(registry._skills.items()):
            roles = ", ".join(skill.allowed_roles) if skill.allowed_roles else "all"
            lines.append(f"  <b>{html.escape(name)}</b> — {html.escape(skill.description)}\n    Roles: {roles}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ------------------------------------------------------------------
# Plain text handler — captures ideas for projects in ideation
# ------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    text = update.message.text.strip()
    if not text:
        return

    # Optional explicit idea prefix while in chat mode.
    if text.lower().startswith("idea:"):
        idea_text = text[5:].strip()
        if not idea_text:
            await update.message.reply_text("Usage: idea: <text>")
            return
        await _capture_idea(update, idea_text)
        return

    if cfg.TELEGRAM_TEXT_MODE == "idea":
        await _capture_idea(update, text)
        return

    await _reply_naturally(update, text)


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
