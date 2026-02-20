# skynet-project-documentation

This skill creates a full SKYNET-compatible project template and keeps documentation plus execution state synchronized for:
- humans
- multiple parallel agents
- people joining mid-project and resuming safely

## What it does

1) Creates a project skeleton with:
- Human docs: PRD, ADRs, architecture, runbooks
- Execution plan files (PlanSuite): task_plan.md, progress.md, findings.md
- Machine control: TASK_GRAPH.yaml, file ownership, execution ledger, next actions
- Policy enforcement: POLICY.yaml plus enforcement plus audit

2) Integrates with SKYNET control plane:
- POST /v1/tasks/enqueue
- POST /v1/tasks/claim
- POST /v1/tasks/{id}/complete
- POST /v1/tasks/{id}/release
- GET /v1/tasks, GET /v1/files/ownership

3) Enables safe continuation:
- NEXT_ACTIONS.yaml tells any agent or human what to do next
- EXECUTION_LEDGER.yaml mirrors DB state
- AGENT_ACTIVITY.yaml shows who is doing what

## Environment Variables

- SKYNET_CONTROL_PLANE_BASE_URL (default: http://localhost:8000)
- SKYNET_CONTROL_PLANE_API_KEY (optional)
- SKYNET_PROJECTS_ROOT (default: ./projects)

## Key rules enforced

- No execution until plan is FINALIZED (policy gate)
- Tasks must be enqueued and claimed via control plane
- File ownership conflicts are prevented via control plane registry
- Progress must be synced and written after state changes
