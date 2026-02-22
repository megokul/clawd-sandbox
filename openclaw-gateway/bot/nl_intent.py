"""
bot/nl_intent.py -- Greeting helpers and project reference resolution.

The waterfall intent-extraction pipeline has been replaced by an LLM-first
approach where the LLM calls project management tools directly. This module
now contains only the utilities that remain needed by commands.py.
"""
from __future__ import annotations

import logging
import re

from telegram import Update

import bot_config as cfg
from . import state
from .helpers import (
    _is_smalltalk_or_ack,
    _norm_project,
    _project_display,
)

logger = logging.getLogger("skynet.telegram")


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
    """Return a brief greeting, optionally mentioning the active project."""
    base = _smalltalk_reply(text)
    if _is_pure_greeting(text):
        return base

    if not state._project_manager or not state._last_project_id:
        return base

    try:
        from db import store
        project = await store.get_project(state._project_manager.db, state._last_project_id)
    except Exception:
        return base

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


async def _resolve_project(reference: str | None = None) -> tuple[dict | None, str | None]:
    """Resolve a natural-language project reference to a concrete project."""
    if not state._project_manager:
        return None, "Project manager is not initialized."

    projects = await state._project_manager.list_projects()
    if not projects:
        return None, "No projects exist yet. Tell me the project name and I will create it."

    if reference:
        from .helpers import _clean_entity
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
