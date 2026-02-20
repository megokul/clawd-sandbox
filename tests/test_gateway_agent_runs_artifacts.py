"""Gateway agent run and artifact storage tests."""

from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_agent_runs_and_task_artifacts_roundtrip() -> None:
    repo_root = Path(__file__).parent.parent
    schema_path = repo_root / "openclaw-gateway" / "db" / "schema.py"
    store_path = repo_root / "openclaw-gateway" / "db" / "store.py"

    schema = _load_module(schema_path, "oc_gateway_schema_agent_runs")
    store = _load_module(store_path, "oc_gateway_store_agent_runs")

    db = await schema.init_db(":memory:")
    try:
        project = await store.create_project(
            db,
            "agent-run-project",
            "Agent Run Project",
            "E:/tmp/agent-run-project",
        )
        plan_id = await store.create_plan(
            db,
            project["id"],
            summary="test plan",
            timeline=[],
            milestones=[],
        )
        task_ids = await store.create_tasks(
            db,
            project["id"],
            plan_id,
            tasks=[{
                "title": "Implement endpoint",
                "description": "Add read model endpoint",
                "milestone": "Control Plane",
            }],
        )
        assert len(task_ids) == 1
        task = (await store.get_tasks(db, project["id"], plan_id))[0]

        run_id = await store.create_agent_run(
            db,
            project_id=project["id"],
            task_id=int(task["id"]),
            agent_id="agent-1",
            agent_role="backend",
            metadata={"task_title": task["title"]},
        )
        assert run_id > 0

        await store.heartbeat_agent_run(
            db,
            run_id=run_id,
            metadata_patch={"heartbeat_count": 1},
        )
        await store.finish_agent_run(
            db,
            run_id=run_id,
            status="succeeded",
            metadata_patch={"summary": "done"},
        )

        runs = await store.list_agent_runs(db, project_id=project["id"], limit=10)
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["metadata"]["task_title"] == task["title"]
        assert runs[0]["metadata"]["heartbeat_count"] == 1
        assert runs[0]["metadata"]["summary"] == "done"

        artifact_id = await store.add_task_artifact(
            db,
            project_id=project["id"],
            task_id=int(task["id"]),
            artifact_type="task_report_markdown",
            title="Task report",
            content="# Task report",
            metadata={"run_id": run_id},
        )
        assert artifact_id > 0

        artifacts_for_task = await store.list_task_artifacts(
            db,
            project_id=project["id"],
            task_id=int(task["id"]),
            limit=10,
        )
        assert len(artifacts_for_task) == 1
        assert artifacts_for_task[0]["id"] == artifact_id
        assert artifacts_for_task[0]["artifact_type"] == "task_report_markdown"
        assert artifacts_for_task[0]["metadata"]["run_id"] == run_id

        all_artifacts = await store.list_task_artifacts(
            db,
            project_id=project["id"],
            limit=10,
        )
        assert len(all_artifacts) == 1
        assert all_artifacts[0]["id"] == artifact_id
    finally:
        await db.close()
