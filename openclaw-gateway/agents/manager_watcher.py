"""
SKYNET - Manager/Watcher Agent

Supervises long-running worker task execution and run records.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

import aiosqlite

from db import store

logger = logging.getLogger("skynet.agents.manager")


class ManagerWatcherAgent:
    """Track agent runs, emit heartbeats, and apply nudge policies."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        on_progress: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.on_progress = on_progress

    async def start_run(
        self,
        *,
        project_id: str,
        task_id: int | None,
        task_title: str,
        agent_id: str,
        agent_role: str,
    ) -> int:
        run_id = await store.create_agent_run(
            self.db,
            project_id=project_id,
            task_id=task_id,
            agent_id=agent_id,
            agent_role=agent_role,
            metadata={"task_title": task_title},
        )
        await store.add_event(
            self.db,
            project_id,
            "agent_run_started",
            f"{agent_role} started task: {task_title}",
            detail=f"run_id={run_id}",
        )
        return run_id

    async def heartbeat(self, *, run_id: int) -> None:
        await store.heartbeat_agent_run(self.db, run_id=run_id)

    async def heartbeat_loop(
        self,
        *,
        project_id: str,
        run_id: int,
        task_title: str,
        stop_event: asyncio.Event,
        interval_seconds: float = 20.0,
        nudge_after_seconds: float = 120.0,
    ) -> None:
        started = time.monotonic()
        nudged = False

        while not stop_event.is_set():
            try:
                await self.heartbeat(run_id=run_id)
                elapsed = time.monotonic() - started
                if not nudged and elapsed >= nudge_after_seconds:
                    nudged = True
                    summary = (
                        f"Manager watcher nudge: task '{task_title}' is still running "
                        f"after {int(elapsed)}s."
                    )
                    await store.add_event(self.db, project_id, "manager_nudge", summary)
                    if self.on_progress is not None:
                        try:
                            await self.on_progress(project_id, "manager_nudge", summary)
                        except Exception:
                            logger.exception("Manager nudge callback failed")
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("Manager heartbeat loop failure (run_id=%s)", run_id)
                await asyncio.sleep(max(interval_seconds, 2.0))

    async def finish_run_success(
        self,
        *,
        project_id: str,
        run_id: int,
        summary: str,
    ) -> None:
        await store.finish_agent_run(
            self.db,
            run_id=run_id,
            status="succeeded",
            metadata_patch={"summary": summary[:500]},
        )
        await store.add_event(
            self.db,
            project_id,
            "agent_run_succeeded",
            "Manager watcher marked run succeeded.",
            detail=f"run_id={run_id}",
        )

    async def finish_run_failed(
        self,
        *,
        project_id: str,
        run_id: int,
        error_message: str,
    ) -> None:
        await store.finish_agent_run(
            self.db,
            run_id=run_id,
            status="failed",
            error_message=error_message,
        )
        await store.add_event(
            self.db,
            project_id,
            "agent_run_failed",
            "Manager watcher marked run failed.",
            detail=f"run_id={run_id}; error={error_message[:500]}",
        )
