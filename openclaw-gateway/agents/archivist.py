"""
SKYNET - Archivist Agent

Generates durable task/project reports and stores them as artifacts.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from db import store


class ArchivistAgent:
    """Generate markdown reports for completed runs."""

    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self.db = db

    async def record_task_report(
        self,
        *,
        project: dict[str, Any],
        task: dict[str, Any],
        agent_role: str,
        run_id: int,
        result_summary: str,
    ) -> int:
        report_md = self._build_task_report_markdown(
            project=project,
            task=task,
            agent_role=agent_role,
            run_id=run_id,
            result_summary=result_summary,
        )
        artifact_id = await store.add_task_artifact(
            self.db,
            project_id=str(project["id"]),
            task_id=int(task["id"]),
            artifact_type="task_report_markdown",
            title=f"Task Report: {task.get('title', 'Untitled task')}",
            content=report_md,
            metadata={"agent_role": agent_role, "run_id": run_id},
        )
        await store.add_event(
            self.db,
            str(project["id"]),
            "task_report_generated",
            f"Generated task report for: {task.get('title', 'Untitled task')}",
            detail=f"artifact_id={artifact_id}",
        )
        return artifact_id

    async def record_project_summary(
        self,
        *,
        project: dict[str, Any],
    ) -> int:
        tasks = await store.get_tasks(self.db, str(project["id"]))
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        total = len(tasks)
        artifacts = await store.list_task_artifacts(self.db, project_id=str(project["id"]), limit=500)

        body = [
            f"# Project Summary: {project.get('display_name', project.get('name', 'Project'))}",
            "",
            f"- Status: {project.get('status', 'unknown')}",
            f"- Progress: {completed}/{total}",
            f"- Generated artifacts: {len(artifacts)}",
            "",
            "## Latest Tasks",
        ]
        for task in tasks[-10:]:
            body.append(
                f"- {task.get('title', 'Untitled')} "
                f"({task.get('status', 'unknown')})"
            )
        summary_md = "\n".join(body)

        artifact_id = await store.add_task_artifact(
            self.db,
            project_id=str(project["id"]),
            task_id=None,
            artifact_type="project_summary_markdown",
            title="Project Summary",
            content=summary_md,
            metadata={"completed_tasks": completed, "total_tasks": total},
        )
        await store.add_event(
            self.db,
            str(project["id"]),
            "project_summary_generated",
            "Generated project summary artifact.",
            detail=f"artifact_id={artifact_id}",
        )
        return artifact_id

    @staticmethod
    def _build_task_report_markdown(
        *,
        project: dict[str, Any],
        task: dict[str, Any],
        agent_role: str,
        run_id: int,
        result_summary: str,
    ) -> str:
        milestone = task.get("milestone") or "General"
        lines = [
            f"# Task Report: {task.get('title', 'Untitled task')}",
            "",
            f"- Project: {project.get('display_name', project.get('name', 'project'))}",
            f"- Task ID: {task.get('id')}",
            f"- Milestone: {milestone}",
            f"- Agent Role: {agent_role}",
            f"- Run ID: {run_id}",
            "",
            "## Summary",
            result_summary.strip() or "No summary provided.",
        ]
        return "\n".join(lines)
