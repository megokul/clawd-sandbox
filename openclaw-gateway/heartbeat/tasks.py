"""
SKYNET Heartbeat — Built-in Periodic Tasks

Default heartbeat tasks that ship with SKYNET.  Each function is an
async handler that receives a context dict containing references to
shared services (sentinel, memory_manager, s3, scheduler, db).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("skynet.heartbeat.tasks")


def _is_missing_s3_credentials_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "unable to locate credentials" in text
        or "no credentials" in text
        or "invalidaccesskeyid" in text
        or "signaturedoesnotmatch" in text
    )


async def health_check(ctx: Any) -> None:
    """
    Every 5 min: Run Sentinel health checks and alert on issues.

    Context requires: sentinel, alert_dispatcher
    """
    if not ctx or not hasattr(ctx, "sentinel"):
        return

    statuses = await ctx.sentinel.run_all_checks()

    if hasattr(ctx, "alert_dispatcher"):
        await ctx.alert_dispatcher.process_health_results(statuses)


async def memory_sync(ctx: Any) -> None:
    """
    Every 30 min: Sync active project memory files to S3.

    Context requires: memory_manager, db, active_project_ids
    """
    if not ctx or not hasattr(ctx, "memory_manager"):
        return

    project_ids = getattr(ctx, "active_project_ids", [])
    for pid in project_ids:
        try:
            await ctx.memory_manager.sync_to_s3(pid)
        except Exception as exc:
            logger.warning("Memory sync failed for project %s: %s", pid, exc)


async def provider_usage_snapshot(ctx: Any) -> None:
    """
    Every 6h: Snapshot AI provider usage stats to S3.

    Context requires: s3, db
    """
    if not ctx or not hasattr(ctx, "s3") or not ctx.s3:
        return

    try:
        from db import store
        usage = await store.get_provider_usage_summary(ctx.db)
        if usage:
            await ctx.s3.snapshot_provider_usage(usage)
            logger.info("Provider usage snapshot saved (%d entries)", len(usage))
    except Exception as exc:
        if _is_missing_s3_credentials_error(exc):
            logger.info("Provider usage snapshot skipped: S3 credentials are not configured.")
            return
        logger.warning("Provider usage snapshot failed: %s", exc)


async def daily_backup(ctx: Any) -> None:
    """
    Every 24h: Create a zip backup of active projects and upload to S3.

    Context requires: s3, gateway_api_url, db
    """
    if not ctx or not hasattr(ctx, "s3") or not ctx.s3:
        return

    logger.info("Daily backup task started")
    # This delegates to the background storage module which already
    # handles project zipping and S3 upload.
    try:
        from storage.background import BackgroundStorage
        bg = BackgroundStorage(ctx.db, ctx.s3, ctx.gateway_api_url)
        await bg.backup_active_projects()
    except Exception as exc:
        if _is_missing_s3_credentials_error(exc):
            logger.info("Daily backup skipped: S3 credentials are not configured.")
            return
        logger.warning("Daily backup failed: %s", exc)


# Registry of default heartbeat tasks with their intervals.
DEFAULT_TASKS = [
    {
        "name": "health_check",
        "description": "Run Sentinel health checks",
        "interval_seconds": 300,       # 5 minutes
        "handler": health_check,
    },
    {
        "name": "memory_sync",
        "description": "Sync project memory to S3",
        "interval_seconds": 1800,      # 30 minutes
        "handler": memory_sync,
    },
    {
        "name": "provider_usage_snapshot",
        "description": "Snapshot AI provider usage to S3",
        "interval_seconds": 21600,     # 6 hours
        "handler": provider_usage_snapshot,
    },
    {
        "name": "daily_backup",
        "description": "Backup active projects to S3",
        "interval_seconds": 86400,     # 24 hours
        "handler": daily_backup,
    },
]
