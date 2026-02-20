"""
SKYNET — Data Access Layer

Async CRUD operations backed by SQLite.  Every public method takes
an ``aiosqlite.Connection`` and returns plain dicts / lists.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------
# Projects
# ------------------------------------------------------------------

async def create_project(
    db: aiosqlite.Connection,
    name: str,
    display_name: str,
    local_path: str,
) -> dict[str, Any]:
    project_id = _uuid()
    await db.execute(
        "INSERT INTO projects (id, name, display_name, local_path) VALUES (?, ?, ?, ?)",
        (project_id, name, display_name, local_path),
    )
    await db.commit()
    return await get_project(db, project_id)


async def get_project(db: aiosqlite.Connection, project_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_project_by_name(db: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM projects WHERE name = ?", (name,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with db.execute("SELECT * FROM projects ORDER BY created_at DESC") as cur:
        return [dict(row) for row in await cur.fetchall()]


async def update_project(
    db: aiosqlite.Connection,
    project_id: str,
    **fields: Any,
) -> None:
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [project_id]
    await db.execute(f"UPDATE projects SET {sets} WHERE id = ?", vals)
    await db.commit()


async def remove_project_cascade(
    db: aiosqlite.Connection,
    project_id: str,
) -> bool:
    """
    Permanently remove a project and all project-scoped records.

    Returns True when a project row was deleted.
    """
    tables = (
        "ideas",
        "tasks",
        "plans",
        "agents",
        "conversations",
        "project_events",
        "agent_runs",
        "task_artifacts",
    )
    for table in tables:
        await db.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
    cur = await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await db.commit()
    return int(cur.rowcount or 0) > 0


async def get_projects_by_status(
    db: aiosqlite.Connection,
    status: str,
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM projects WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


# ------------------------------------------------------------------
# Ideas
# ------------------------------------------------------------------

async def add_idea(
    db: aiosqlite.Connection,
    project_id: str,
    message_text: str,
) -> int:
    async with db.execute(
        "INSERT INTO ideas (project_id, message_text) VALUES (?, ?)",
        (project_id, message_text),
    ) as cur:
        idea_id = cur.lastrowid
    await db.commit()
    return idea_id


async def get_ideas(db: aiosqlite.Connection, project_id: str) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM ideas WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


# ------------------------------------------------------------------
# Plans
# ------------------------------------------------------------------

async def create_plan(
    db: aiosqlite.Connection,
    project_id: str,
    summary: str,
    timeline: list[dict],
    milestones: list[dict],
) -> int:
    # Deactivate any previous active plans.
    await db.execute(
        "UPDATE plans SET is_active = 0 WHERE project_id = ? AND is_active = 1",
        (project_id,),
    )
    async with db.execute(
        "INSERT INTO plans (project_id, summary, timeline, milestones) VALUES (?, ?, ?, ?)",
        (project_id, summary, json.dumps(timeline), json.dumps(milestones)),
    ) as cur:
        plan_id = cur.lastrowid
    await db.commit()
    return plan_id


async def get_active_plan(db: aiosqlite.Connection, project_id: str) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM plans WHERE project_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return None
        plan = dict(row)
        plan["timeline"] = json.loads(plan["timeline"])
        plan["milestones"] = json.loads(plan["milestones"])
        return plan


# ------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------

async def create_tasks(
    db: aiosqlite.Connection,
    project_id: str,
    plan_id: int,
    tasks: list[dict[str, str]],
) -> list[int]:
    ids = []
    for i, task in enumerate(tasks):
        async with db.execute(
            "INSERT INTO tasks (project_id, plan_id, milestone, title, description, order_index) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, plan_id, task.get("milestone", ""),
             task["title"], task.get("description", ""), i),
        ) as cur:
            ids.append(cur.lastrowid)
    await db.commit()
    return ids


async def get_tasks(
    db: aiosqlite.Connection,
    project_id: str,
    plan_id: int | None = None,
) -> list[dict[str, Any]]:
    if plan_id:
        sql = "SELECT * FROM tasks WHERE project_id = ? AND plan_id = ? ORDER BY order_index"
        params = (project_id, plan_id)
    else:
        sql = "SELECT * FROM tasks WHERE project_id = ? ORDER BY order_index"
        params = (project_id,)
    async with db.execute(sql, params) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def update_task(
    db: aiosqlite.Connection,
    task_id: int,
    **fields: Any,
) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [task_id]
    await db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    await db.commit()


# ------------------------------------------------------------------
# Conversations
# ------------------------------------------------------------------

async def add_conversation_message(
    db: aiosqlite.Connection,
    project_id: str,
    role: str,
    content: Any,
    token_count: int = 0,
    phase: str = "coding",
) -> int:
    content_json = json.dumps(content, default=str) if not isinstance(content, str) else content
    async with db.execute(
        "INSERT INTO conversations (project_id, role, content, token_count, phase) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, role, content_json, token_count, phase),
    ) as cur:
        msg_id = cur.lastrowid
    await db.commit()
    return msg_id


async def get_conversation(
    db: aiosqlite.Connection,
    project_id: str,
    phase: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if phase:
        sql = ("SELECT * FROM conversations WHERE project_id = ? AND phase = ? "
               "ORDER BY id DESC LIMIT ?")
        params = (project_id, phase, limit)
    else:
        sql = "SELECT * FROM conversations WHERE project_id = ? ORDER BY id DESC LIMIT ?"
        params = (project_id, limit)
    async with db.execute(sql, params) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    rows.reverse()  # oldest first
    for row in rows:
        try:
            row["content"] = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            pass
    return rows


# ------------------------------------------------------------------
# Provider Usage
# ------------------------------------------------------------------

async def record_provider_usage(
    db: aiosqlite.Connection,
    provider_name: str,
    requests: int = 1,
    tokens: int = 0,
    error: bool = False,
) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = _now()
    await db.execute(
        """INSERT INTO provider_usage (provider_name, date, requests_used, tokens_used, errors, last_request_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider_name, date) DO UPDATE SET
               requests_used = requests_used + ?,
               tokens_used = tokens_used + ?,
               errors = errors + ?,
               last_request_at = ?""",
        (provider_name, today, requests, tokens, int(error), now,
         requests, tokens, int(error), now),
    )
    await db.commit()


async def get_provider_usage(
    db: aiosqlite.Connection,
    provider_name: str,
    date: str | None = None,
) -> dict[str, Any] | None:
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT * FROM provider_usage WHERE provider_name = ? AND date = ?",
        (provider_name, date),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_provider_usage_today(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT * FROM provider_usage WHERE date = ? ORDER BY provider_name",
        (today,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


# ------------------------------------------------------------------
# Project Events
# ------------------------------------------------------------------

async def add_event(
    db: aiosqlite.Connection,
    project_id: str,
    event_type: str,
    summary: str,
    detail: str = "",
) -> int:
    async with db.execute(
        "INSERT INTO project_events (project_id, event_type, summary, detail) "
        "VALUES (?, ?, ?, ?)",
        (project_id, event_type, summary, detail),
    ) as cur:
        event_id = cur.lastrowid
    await db.commit()
    return event_id


async def get_events(
    db: aiosqlite.Connection,
    project_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM project_events WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


# ------------------------------------------------------------------
# Agents (v3)
# ------------------------------------------------------------------

async def create_agent(
    db: aiosqlite.Connection,
    project_id: str,
    role: str,
) -> str:
    agent_id = _uuid()
    await db.execute(
        "INSERT INTO agents (id, project_id, role, created_at) VALUES (?, ?, ?, ?)",
        (agent_id, project_id, role, _now()),
    )
    await db.commit()
    return agent_id


async def get_agent(db: aiosqlite.Connection, agent_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_agent_by_project_role(
    db: aiosqlite.Connection,
    project_id: str,
    role: str,
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM agents WHERE project_id = ? AND role = ?",
        (project_id, role),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_agents(
    db: aiosqlite.Connection,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    if project_id:
        sql = "SELECT * FROM agents WHERE project_id = ? ORDER BY role"
        params: tuple = (project_id,)
    else:
        sql = "SELECT * FROM agents ORDER BY project_id, role"
        params = ()
    async with db.execute(sql, params) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def update_agent(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    status: str | None = None,
    tasks_completed_delta: int = 0,
    total_tokens_delta: int = 0,
    last_active_at: str | None = None,
) -> None:
    parts: list[str] = []
    vals: list[Any] = []
    if status is not None:
        parts.append("status = ?")
        vals.append(status)
    if tasks_completed_delta:
        parts.append("tasks_completed = tasks_completed + ?")
        vals.append(tasks_completed_delta)
    if total_tokens_delta:
        parts.append("total_tokens = total_tokens + ?")
        vals.append(total_tokens_delta)
    if last_active_at is not None:
        parts.append("last_active_at = ?")
        vals.append(last_active_at)
    if not parts:
        return
    vals.append(agent_id)
    await db.execute(f"UPDATE agents SET {', '.join(parts)} WHERE id = ?", vals)
    await db.commit()


# ------------------------------------------------------------------
# Agent Runs + Task Artifacts
# ------------------------------------------------------------------

async def create_agent_run(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    task_id: int | None,
    agent_id: str,
    agent_role: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    now = _now()
    async with db.execute(
        """
        INSERT INTO agent_runs (
            project_id, task_id, agent_id, agent_role, status,
            started_at, heartbeat_at, metadata
        ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            project_id,
            task_id,
            agent_id,
            agent_role,
            now,
            now,
            json.dumps(metadata or {}),
        ),
    ) as cur:
        run_id = int(cur.lastrowid)
    await db.commit()
    return run_id


async def heartbeat_agent_run(
    db: aiosqlite.Connection,
    *,
    run_id: int,
    metadata_patch: dict[str, Any] | None = None,
) -> None:
    if metadata_patch:
        async with db.execute(
            "SELECT metadata FROM agent_runs WHERE id = ?",
            (int(run_id),),
        ) as cur:
            row = await cur.fetchone()
        existing = {}
        if row and row[0]:
            try:
                existing = json.loads(row[0])
            except Exception:
                existing = {}
        existing.update(metadata_patch)
        await db.execute(
            "UPDATE agent_runs SET heartbeat_at = ?, metadata = ? WHERE id = ?",
            (_now(), json.dumps(existing), int(run_id)),
        )
    else:
        await db.execute(
            "UPDATE agent_runs SET heartbeat_at = ? WHERE id = ?",
            (_now(), int(run_id)),
        )
    await db.commit()


async def finish_agent_run(
    db: aiosqlite.Connection,
    *,
    run_id: int,
    status: str,
    error_message: str = "",
    metadata_patch: dict[str, Any] | None = None,
) -> None:
    status_norm = (status or "").strip().lower() or "unknown"
    now = _now()
    if metadata_patch:
        async with db.execute(
            "SELECT metadata FROM agent_runs WHERE id = ?",
            (int(run_id),),
        ) as cur:
            row = await cur.fetchone()
        existing = {}
        if row and row[0]:
            try:
                existing = json.loads(row[0])
            except Exception:
                existing = {}
        existing.update(metadata_patch)
        await db.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, heartbeat_at = ?, error_message = ?, metadata = ?
            WHERE id = ?
            """,
            (
                status_norm,
                now,
                now,
                (error_message or "")[:2000],
                json.dumps(existing),
                int(run_id),
            ),
        )
    else:
        await db.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, heartbeat_at = ?, error_message = ?
            WHERE id = ?
            """,
            (status_norm, now, now, (error_message or "")[:2000], int(run_id)),
        )
    await db.commit()


async def list_agent_runs(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    async with db.execute(
        """
        SELECT *
        FROM agent_runs
        WHERE project_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project_id, int(limit)),
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    rows.reverse()
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata", "{}"))
        except Exception:
            row["metadata"] = {}
    return rows


async def add_task_artifact(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    task_id: int | None,
    artifact_type: str,
    title: str,
    content: str = "",
    file_path: str = "",
    url: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    async with db.execute(
        """
        INSERT INTO task_artifacts (
            project_id, task_id, artifact_type, title,
            content, file_path, url, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            task_id,
            artifact_type,
            title,
            content,
            file_path,
            url,
            json.dumps(metadata or {}),
            _now(),
        ),
    ) as cur:
        artifact_id = int(cur.lastrowid)
    await db.commit()
    return artifact_id


async def list_task_artifacts(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    task_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if task_id is None:
        sql = (
            "SELECT * FROM task_artifacts WHERE project_id = ? "
            "ORDER BY id DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (project_id, int(limit))
    else:
        sql = (
            "SELECT * FROM task_artifacts WHERE project_id = ? AND task_id = ? "
            "ORDER BY id DESC LIMIT ?"
        )
        params = (project_id, int(task_id), int(limit))

    async with db.execute(sql, params) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    rows.reverse()
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata", "{}"))
        except Exception:
            row["metadata"] = {}
    return rows


# ------------------------------------------------------------------
# User Memory / Persona Profile
# ------------------------------------------------------------------

async def ensure_user(
    db: aiosqlite.Connection,
    *,
    telegram_user_id: int,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
) -> dict[str, Any]:
    now = _now()
    await db.execute(
        """
        INSERT INTO users (
            telegram_user_id, username, first_name, last_name, created_at, updated_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            updated_at = excluded.updated_at,
            last_seen_at = excluded.last_seen_at
        """,
        (int(telegram_user_id), username, first_name, last_name, now, now, now),
    )
    await db.commit()
    user = await get_user_by_telegram_id(db, telegram_user_id)
    if not user:
        raise ValueError("Failed to load ensured user.")
    return user


async def get_user_by_telegram_id(
    db: aiosqlite.Connection,
    telegram_user_id: int,
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?",
        (int(telegram_user_id),),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(db: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_user_memory_enabled(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    enabled: bool,
) -> None:
    await db.execute(
        "UPDATE users SET memory_enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, _now(), int(user_id)),
    )
    await db.commit()


async def update_user_core_fields(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    timezone: str | None = None,
    region: str | None = None,
) -> None:
    fields: dict[str, Any] = {}
    if timezone is not None:
        fields["timezone"] = timezone.strip()
    if region is not None:
        fields["region"] = region.strip()
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [int(user_id)]
    await db.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
    await db.commit()


async def upsert_user_preference(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    pref_key: str,
    pref_value: str,
    source: str = "chat",
) -> None:
    await db.execute(
        """
        INSERT INTO user_preferences (user_id, pref_key, pref_value, source, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, pref_key) DO UPDATE SET
            pref_value = excluded.pref_value,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (int(user_id), pref_key.strip(), pref_value.strip(), source, _now()),
    )
    await db.commit()


async def get_user_preferences(
    db: aiosqlite.Connection,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    async with db.execute(
        """
        SELECT user_id, pref_key, pref_value, source, updated_at
        FROM user_preferences
        WHERE user_id = ?
        ORDER BY pref_key
        """,
        (int(user_id),),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def get_provider_usage_summary(
    db: aiosqlite.Connection,
    date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return provider usage summary rows for a date (defaults to today).

    This is a compatibility helper used by heartbeat snapshot tasks.
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with db.execute(
        """
        SELECT
            provider_name,
            date,
            requests_used,
            tokens_used,
            errors,
            last_request_at
        FROM provider_usage
        WHERE date = ?
        ORDER BY provider_name
        """,
        (date,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def add_or_update_profile_fact(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    fact_key: str,
    fact_value: str,
    source: str = "chat",
    confidence: float = 0.6,
) -> dict[str, Any]:
    now = _now()
    key = fact_key.strip().lower()
    value = fact_value.strip()
    conf = max(0.0, min(float(confidence), 1.0))

    async with db.execute(
        """
        SELECT id, confidence
        FROM user_profile_facts
        WHERE user_id = ? AND fact_key = ? AND fact_value = ?
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), key, value),
    ) as cur:
        row = await cur.fetchone()

    if row:
        fact_id = int(row[0])
        prior_conf = float(row[1] or 0.0)
        merged_conf = max(prior_conf, conf)
        await db.execute(
            """
            UPDATE user_profile_facts
            SET is_active = 1, source = ?, confidence = ?, updated_at = ?
            WHERE id = ?
            """,
            (source, merged_conf, now, fact_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO user_profile_facts (
                user_id, fact_key, fact_value, source, confidence, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (int(user_id), key, value, source, conf, now, now),
        )
    await db.commit()

    async with db.execute(
        """
        SELECT *
        FROM user_profile_facts
        WHERE user_id = ? AND fact_key = ? AND fact_value = ?
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), key, value),
    ) as cur:
        saved = await cur.fetchone()
    if not saved:
        raise ValueError("Unable to load saved fact.")
    return dict(saved)


async def list_profile_facts(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT * FROM user_profile_facts WHERE user_id = ? "
        + ("AND is_active = 1 " if active_only else "")
        + "ORDER BY updated_at DESC, id DESC"
    )
    async with db.execute(sql, (int(user_id),)) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def forget_profile_facts(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    key_or_text: str,
) -> int:
    needle = key_or_text.strip().lower()
    if not needle:
        return 0

    cur = await db.execute(
        """
        UPDATE user_profile_facts
        SET is_active = 0, updated_at = ?
        WHERE user_id = ? AND is_active = 1
          AND (lower(fact_key) = ? OR lower(fact_value) LIKE ?)
        """,
        (_now(), int(user_id), needle, f"%{needle}%"),
    )
    await db.commit()
    return int(cur.rowcount or 0)


async def add_user_conversation(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    role: str,
    content: str,
    chat_id: str = "",
    telegram_message_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    async with db.execute(
        """
        INSERT INTO user_conversations (
            user_id, role, content, chat_id, telegram_message_id, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            role,
            content,
            chat_id,
            telegram_message_id,
            json.dumps(metadata or {}),
            _now(),
        ),
    ) as cur:
        cid = int(cur.lastrowid)
    await db.commit()
    return cid


async def list_user_conversations(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    async with db.execute(
        """
        SELECT *
        FROM user_conversations
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), int(limit)),
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    rows.reverse()
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata", "{}"))
        except Exception:
            row["metadata"] = {}
    return rows


async def add_memory_audit_log(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    action: str,
    target_type: str = "",
    target_key: str = "",
    detail: str = "",
) -> int:
    async with db.execute(
        """
        INSERT INTO memory_audit_log (
            user_id, action, target_type, target_key, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (int(user_id), action, target_type, target_key, detail, _now()),
    ) as cur:
        audit_id = int(cur.lastrowid)
    await db.commit()
    return audit_id
