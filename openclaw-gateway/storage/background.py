"""
SKYNET — Background Storage Tasks

Runs scheduled tasks for S3 archival and maintenance:
  - Nightly project backups
  - Daily provider usage snapshots
  - Conversation archive (offload old messages from SQLite)

All tasks respect AWS free-tier limits by batching operations
and compressing data before upload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from db import store
from .s3_client import S3Storage

logger = logging.getLogger("skynet.storage.background")


class BackgroundScheduler:
    """Manages periodic S3 storage tasks."""

    def __init__(
        self,
        s3: S3Storage,
        db: aiosqlite.Connection,
        gateway_api_url: str,
    ):
        self.s3 = s3
        self.db = db
        self.gateway_url = gateway_api_url
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background task loop."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="storage-scheduler")
        logger.info("Background storage scheduler started.")

    async def stop(self) -> None:
        """Stop the background task loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background storage scheduler stopped.")

    async def _loop(self) -> None:
        """Main loop — runs tasks at scheduled intervals."""
        last_usage_snapshot = ""
        last_archive = ""

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")

                # Daily provider usage snapshot (run once per day after midnight UTC).
                if today != last_usage_snapshot:
                    await self._daily_usage_snapshot()
                    last_usage_snapshot = today

                # Archive old conversations (run once per day).
                if today != last_archive:
                    await self._archive_old_conversations()
                    last_archive = today

            except Exception:
                logger.exception("Error in background storage task.")

            # Check every 30 minutes.
            await asyncio.sleep(1800)

    async def _daily_usage_snapshot(self) -> None:
        """Export today's provider usage to S3."""
        try:
            usage = await store.get_all_provider_usage_today(self.db)
            if not usage:
                return

            usage_dicts = [dict(row) for row in usage]
            key = await self.s3.snapshot_provider_usage(usage_dicts)
            logger.info("Saved provider usage snapshot: %s", key)
        except Exception:
            logger.exception("Failed to snapshot provider usage.")

    async def _archive_old_conversations(self) -> None:
        """
        Move conversations older than 7 days to S3.

        Keeps recent messages in SQLite for fast access,
        offloads old ones to S3 to prevent DB bloat.
        """
        try:
            # Get projects with old conversations.
            projects = await store.list_projects(self.db)

            for project in projects:
                project_id = project["id"]
                # Get all messages (we'll archive the older ones).
                all_msgs = await store.get_conversation(
                    self.db, project_id, limit=500,
                )
                if len(all_msgs) <= 30:
                    continue  # Not enough to archive.

                # Keep the 30 most recent, archive the rest.
                to_archive = all_msgs[:-30]
                archive_dicts = [dict(m) for m in to_archive]

                key = await self.s3.archive_conversations(project_id, archive_dicts)
                logger.info(
                    "Archived %d messages for project %s → %s",
                    len(to_archive), project_id, key,
                )

                # TODO: Delete archived messages from SQLite to reclaim space.
                # For now, we keep them as a safety net.

        except Exception:
            logger.exception("Failed to archive old conversations.")

    async def backup_project(
        self,
        project_slug: str,
        version: int,
        *,
        working_dir: str | None = None,
    ) -> str | None:
        """
        Trigger a project backup by zipping the project on the laptop
        and uploading to S3.

        Returns the S3 key on success, or None on failure.
        """
        import aiohttp

        try:
            target_dir = working_dir or f"E:\\MyProjects\\{project_slug}"

            # Request the agent to zip the project.
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.gateway_url}/action",
                    json={
                        "action": "zip_project",
                        "params": {"working_dir": target_dir},
                        "confirmed": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    result = await resp.json()

            if result.get("status") not in {"ok", "success"}:
                logger.error("Failed to zip project %s: %s", project_slug, result.get("error"))
                return None

            import base64
            inner = result.get("result", {})
            rc = int(inner.get("returncode", 0))
            if rc != 0:
                err = (inner.get("stderr") or inner.get("stdout") or result.get("error") or "").strip()
                logger.error("Zip action failed for project %s: %s", project_slug, err or f"exit code {rc}")
                return None
            zip_b64 = inner.get("stdout", "")
            if not zip_b64:
                logger.error("Empty zip data for project %s", project_slug)
                return None

            zip_bytes = base64.b64decode(zip_b64)
            key = await self.s3.upload_project_snapshot(project_slug, version, zip_bytes)
            logger.info("Backed up project %s v%d → %s", project_slug, version, key)
            return key

        except Exception:
            logger.exception("Failed to backup project %s", project_slug)
            return None


class BackgroundStorage:
    """
    Compatibility adapter used by heartbeat.daily_backup.

    Keeps the existing heartbeat call signature stable while delegating
    actual backup logic to BackgroundScheduler.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        s3: S3Storage,
        gateway_api_url: str,
    ) -> None:
        self.db = db
        self.s3 = s3
        self.gateway_api_url = gateway_api_url
        self._scheduler = BackgroundScheduler(s3=s3, db=db, gateway_api_url=gateway_api_url)

    async def backup_active_projects(self) -> None:
        projects = await store.list_projects(self.db)
        active = [
            p for p in projects
            if str(p.get("status", "")).lower() not in {"cancelled"}
        ]
        if not active:
            logger.info("Daily backup: no projects to back up.")
            return

        # Stable daily version marker.
        version = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        backed_up = 0
        attempted = 0
        for project in active:
            slug = str(project.get("name") or "").strip()
            if not slug:
                continue
            attempted += 1
            key = await self._scheduler.backup_project(
                slug,
                version,
                working_dir=str(project.get("local_path") or "").strip() or None,
            )
            if key:
                backed_up += 1

        logger.info("Daily backup finished: %d/%d projects backed up.", backed_up, attempted)
