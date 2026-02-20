from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or f"project-{uuid.uuid4().hex[:8]}"


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write_text(p: Path, content: str) -> None:
    ensure_dir(p.parent)
    p.write_text(content, encoding="utf-8")


def read_yaml(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def write_yaml(p: Path, data: dict[str, Any]) -> None:
    ensure_dir(p.parent)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    shutil.copytree(src, dst)


@dataclass(frozen=True)
class ControlPlaneConfig:
    base_url: str
    api_key: str | None = None
    timeout_s: int = 30


class ControlPlaneClient:
    def __init__(self, cfg: ControlPlaneConfig):
        self.cfg = cfg
        if requests is None:
            raise RuntimeError("requests is required for control-plane integration")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def get(self, path: str) -> dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + path
        r = requests.get(url, headers=self._headers(), timeout=self.cfg.timeout_s)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + path
        r = requests.post(
            url,
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=self.cfg.timeout_s,
        )
        r.raise_for_status()
        return r.json()

    def enqueue_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/tasks/enqueue", payload)

    def list_tasks(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id:
            return self.get(f"/v1/tasks?project_id={project_id}")
        return self.get("/v1/tasks")

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.get(f"/v1/tasks/{task_id}")

    def claim_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/tasks/claim", payload)

    def complete_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/v1/tasks/{task_id}/complete", payload)

    def release_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/v1/tasks/{task_id}/release", payload)

    def list_file_ownership(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id:
            return self.get(f"/v1/files/ownership?project_id={project_id}")
        return self.get("/v1/files/ownership")


TASK_SECTION_RE = re.compile(
    r"^###\s+(?P<task_id>TASK-\d+)\s*:\s*(?P<title>.+?)\s*$",
    re.MULTILINE,
)


def parse_task_plan_md(plan_text: str) -> tuple[str, list[dict[str, Any]]]:
    status_match = re.search(r"^STATUS:\s*(DRAFT|FINALIZED)\s*$", plan_text, re.MULTILINE)
    status = status_match.group(1) if status_match else "DRAFT"

    tasks: list[dict[str, Any]] = []
    matches = list(TASK_SECTION_RE.finditer(plan_text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plan_text)
        block = plan_text[start:end]

        task_id = m.group("task_id").strip()
        title = m.group("title").strip()

        deps: list[str] = []
        dep_match = re.search(r"^Dependencies:\s*(.+?)\s*$", block, re.MULTILINE)
        if dep_match:
            deps = [d.strip() for d in dep_match.group(1).split(",") if d.strip()]

        outputs: list[str] = []
        out_match = re.search(r"^Outputs:\s*$", block, re.MULTILINE)
        if out_match:
            lines = block[out_match.end() :].splitlines()
            for ln in lines:
                if ln.strip().startswith("- "):
                    outputs.append(ln.strip()[2:].strip())
                elif ln.strip() == "":
                    continue
                else:
                    break

        tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "dependencies": deps,
                "outputs": outputs,
            }
        )

    return status, tasks


def load_policy(project_dir: Path) -> dict[str, Any]:
    return read_yaml(project_dir / "policy" / "POLICY.yaml")


def policy_gate_check(project_dir: Path, gate: str | None = None) -> tuple[bool, list[str]]:
    del gate
    errors: list[str] = []
    policy = load_policy(project_dir)

    def need(path_rel: str, msg: str) -> None:
        if not (project_dir / path_rel).exists():
            errors.append(msg)

    if policy.get("documentation", {}).get("require_prd", True):
        need("docs/product/PRD.md", "Missing docs/product/PRD.md (policy require_prd).")

    if policy.get("documentation", {}).get("require_task_plan", True):
        need("planning/task_plan.md", "Missing planning/task_plan.md (policy require_task_plan).")

    if policy.get("documentation", {}).get("require_finalized_plan", True):
        plan = project_dir / "planning" / "task_plan.md"
        if plan.exists():
            st, _ = parse_task_plan_md(read_text(plan))
            if st != "FINALIZED":
                errors.append("Plan is not FINALIZED (policy require_finalized_plan).")

    return (len(errors) == 0), errors


def audit_log(project_dir: Path, actor: str, violation_type: str, details: dict[str, Any]) -> None:
    p = project_dir / "policy" / "AUDIT_LOG.yaml"
    log = read_yaml(p)
    log.setdefault("audit_log", [])
    log["audit_log"].append(
        {
            "timestamp": utc_now_iso(),
            "actor": actor,
            "violation": {"type": violation_type, **details},
        }
    )
    write_yaml(p, log)


def create_project(
    project_name: str,
    project_id: str | None = None,
    root_dir: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    projects_root = Path(root_dir or os.getenv("SKYNET_PROJECTS_ROOT", "./projects")).resolve()
    ensure_dir(projects_root)

    pid = project_id or slugify(project_name)
    proj_dir = projects_root / pid
    ensure_dir(proj_dir)

    template_root = Path(__file__).parent / "templates"
    for item in ["docs", "planning", "control", "memory", "policy"]:
        src = template_root / item
        dst = proj_dir / item
        if src.exists():
            copy_tree(src, dst)

    for item in ["src", "tests", "infra", ".skynet"]:
        ensure_dir(proj_dir / item)

    project_yaml = read_yaml(template_root / "PROJECT.yaml")
    project_yaml.setdefault("project", {})
    project_yaml["project"]["id"] = pid
    project_yaml["project"]["name"] = project_name
    project_yaml["project"]["description"] = description or ""
    project_yaml["project"]["created_at"] = utc_now_iso()
    project_yaml["project"]["created_by"] = "skynet"
    write_yaml(proj_dir / "PROJECT.yaml", project_yaml)

    state_yaml = read_yaml(template_root / "PROJECT_STATE.yaml")
    state_yaml.setdefault("state", {})
    state_yaml["state"]["phase"] = "planning"
    state_yaml["state"]["last_updated"] = utc_now_iso()
    write_yaml(proj_dir / "PROJECT_STATE.yaml", state_yaml)

    manifest = {
        "manifest": {
            "project_id": pid,
            "scheduler_managed": True,
            "task_graph": "control/TASK_GRAPH.yaml",
            "execution_ledger": "control/EXECUTION_LEDGER.yaml",
            "state_file": "PROJECT_STATE.yaml",
            "policy_file": "policy/POLICY.yaml",
        }
    }
    write_yaml(proj_dir / ".skynet" / "manifest.yaml", manifest)

    changelog = read_yaml(proj_dir / "memory" / "changelog.yaml")
    changelog.setdefault("events", [])
    changelog["events"].append(
        {
            "timestamp": utc_now_iso(),
            "event": "project_created",
            "actor": "skynet",
            "project_id": pid,
        }
    )
    write_yaml(proj_dir / "memory" / "changelog.yaml", changelog)

    return {
        "ok": True,
        "project_id": pid,
        "project_dir": str(proj_dir),
        "message": f"Project created: {pid}",
    }


def generate_plan(project_dir: str) -> dict[str, Any]:
    proj = Path(project_dir).resolve()
    prd = proj / "docs" / "product" / "PRD.md"
    plan = proj / "planning" / "task_plan.md"

    prd_text = read_text(prd) if prd.exists() else ""
    task_plan = textwrap.dedent(
        f"""\
    STATUS: DRAFT

    # Project Plan

    ## Goal
    Derived from PRD (summary):
    {prd_text.strip()[:500]}

    ## Scope
    - In scope: define core deliverables
    - Out of scope: explicitly list exclusions

    ## Risks & Rollback
    - Risk: unclear requirements
      Rollback: revise PRD, re-finalize plan

    ## Milestones (Tasks)

    ### TASK-001: Bootstrap project skeleton + docs baseline
    Dependencies:
    Outputs:
      - docs/
      - planning/
      - control/
      - policy/
      - src/
      - tests/

    ### TASK-002: Architecture baseline (system-design + data-flow)
    Dependencies: TASK-001
    Outputs:
      - docs/architecture/system-design.md
      - docs/architecture/data-flow.md

    ### TASK-003: Implementation skeleton (src layout + entrypoint + tests scaffold)
    Dependencies: TASK-001
    Outputs:
      - src/
      - tests/

    ### TASK-004: Runbooks baseline (local-dev + deploy + recovery)
    Dependencies: TASK-001
    Outputs:
      - docs/runbooks/local-dev.md
      - docs/runbooks/deploy.md
      - docs/runbooks/recovery.md
    """
    )
    write_text(plan, task_plan)

    status, tasks = parse_task_plan_md(task_plan)
    graph = {"tasks": {}}
    for t in tasks:
        graph["tasks"][t["task_id"]] = {
            "title": t["title"],
            "dependencies": t["dependencies"],
            "outputs": t["outputs"],
        }
    write_yaml(proj / "control" / "TASK_GRAPH.yaml", graph)

    next_actions = {
        "next_actions": [
            {
                "task_id": t["task_id"],
                "title": t["title"],
                "priority": "high" if t["task_id"] == "TASK-001" else "medium",
                "eligible": (t["task_id"] == "TASK-001"),
                "dependencies_satisfied": (t["task_id"] == "TASK-001"),
                "safe_to_start": (t["task_id"] == "TASK-001"),
            }
            for t in tasks
        ]
    }
    write_yaml(proj / "control" / "NEXT_ACTIONS.yaml", next_actions)

    return {"ok": True, "status": status, "tasks": tasks, "message": "DRAFT plan generated. Finalize to enqueue."}


def finalize_plan_and_enqueue(project_dir: str, gateway_hint: str | None = None) -> dict[str, Any]:
    proj = Path(project_dir).resolve()
    plan_path = proj / "planning" / "task_plan.md"
    if not plan_path.exists():
        return {"ok": False, "error": "planning/task_plan.md not found"}

    plan_text = read_text(plan_path)
    status, tasks = parse_task_plan_md(plan_text)

    if status != "FINALIZED":
        if "STATUS:" in plan_text:
            plan_text = re.sub(
                r"^STATUS:\s*\w+\s*$",
                f"STATUS: FINALIZED\n\nFINALIZED_AT: {utc_now_iso()}",
                plan_text,
                flags=re.MULTILINE,
            )
        else:
            plan_text = f"STATUS: FINALIZED\nFINALIZED_AT: {utc_now_iso()}\n\n{plan_text}"
        write_text(plan_path, plan_text)
        status = "FINALIZED"

    ok, errors = policy_gate_check(proj)
    if not ok:
        audit_log(proj, actor="skynet", violation_type="policy_gate_failed", details={"errors": errors})
        return {"ok": False, "error": "Policy gate failed", "details": errors}

    base_url = os.getenv("SKYNET_CONTROL_PLANE_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("SKYNET_CONTROL_PLANE_API_KEY")
    client = ControlPlaneClient(ControlPlaneConfig(base_url=base_url, api_key=api_key))

    project_id = read_yaml(proj / "PROJECT.yaml").get("project", {}).get("id") or proj.name
    enqueued: list[dict[str, Any]] = []
    for t in tasks:
        payload = {
            "project_id": project_id,
            "task_id": t["task_id"],
            "title": t["title"],
            "dependencies": t["dependencies"],
            "outputs": t["outputs"],
            "gateway_hint": gateway_hint,
        }
        res = client.enqueue_task(payload)
        enqueued.append({"task_id": t["task_id"], "result": res})

    graph = {"tasks": {}}
    for t in tasks:
        graph["tasks"][t["task_id"]] = {
            "title": t["title"],
            "dependencies": t["dependencies"],
            "outputs": t["outputs"],
        }
    write_yaml(proj / "control" / "TASK_GRAPH.yaml", graph)

    changelog = read_yaml(proj / "memory" / "changelog.yaml")
    changelog.setdefault("events", [])
    changelog["events"].append(
        {
            "timestamp": utc_now_iso(),
            "event": "plan_finalized_and_enqueued",
            "actor": "skynet",
            "project_id": project_id,
            "count": len(tasks),
        }
    )
    write_yaml(proj / "memory" / "changelog.yaml", changelog)

    return {"ok": True, "status": status, "project_id": project_id, "enqueued": enqueued}


def sync_progress(project_dir: str) -> dict[str, Any]:
    proj = Path(project_dir).resolve()
    project_id = read_yaml(proj / "PROJECT.yaml").get("project", {}).get("id") or proj.name

    base_url = os.getenv("SKYNET_CONTROL_PLANE_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("SKYNET_CONTROL_PLANE_API_KEY")
    client = ControlPlaneClient(ControlPlaneConfig(base_url=base_url, api_key=api_key))

    tasks_res = client.list_tasks(project_id=project_id)
    tasks = tasks_res.get("tasks", tasks_res)

    normalized: list[dict[str, Any]] = []
    for t in tasks:
        normalized.append(
            {
                "task_id": t.get("task_id") or t.get("id"),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "locked_by": t.get("locked_by"),
                "locked_at": t.get("locked_at"),
                "dependencies": t.get("dependencies", []),
                "outputs": t.get("outputs", []),
                "updated_at": t.get("updated_at") or t.get("last_updated"),
            }
        )

    completed = [t for t in normalized if t["status"] in ("completed", "succeeded", "done")]
    active = [t for t in normalized if t["status"] in ("claimed", "running", "in_progress")]
    pending = [t for t in normalized if t["status"] in ("pending", "queued")]

    completed_ids = {t["task_id"] for t in completed}
    eligible: list[dict[str, Any]] = []
    for t in pending:
        deps = set(t.get("dependencies") or [])
        if deps.issubset(completed_ids):
            eligible.append(t)

    ledger = {
        "project": {"id": project_id},
        "execution": {
            "phase": "execution" if (active or completed) else "planning",
            "progress_percentage": int((len(completed) / max(len(normalized), 1)) * 100),
            "last_updated": utc_now_iso(),
        },
        "summary": {
            "total_tasks": len(normalized),
            "completed_tasks": len(completed),
            "active_tasks": len(active),
            "pending_tasks": len(pending),
        },
        "tasks": {t["task_id"]: t for t in normalized},
        "blockers": [],
        "next_eligible_tasks": [t["task_id"] for t in eligible],
    }
    write_yaml(proj / "control" / "EXECUTION_LEDGER.yaml", ledger)

    next_actions = {
        "next_actions": [
            {
                "task_id": t["task_id"],
                "title": t["title"],
                "priority": "high",
                "eligible": True,
                "dependencies_satisfied": True,
                "safe_to_start": True,
            }
            for t in eligible
        ]
    }
    write_yaml(proj / "control" / "NEXT_ACTIONS.yaml", next_actions)

    agents: dict[str, Any] = {}
    for t in active:
        a = t.get("locked_by") or "unknown"
        agents.setdefault(a, {})
        agents[a] = {
            "current_task": t["task_id"],
            "started_at": t.get("locked_at"),
            "heartbeat": utc_now_iso(),
        }
    write_yaml(proj / "control" / "AGENT_ACTIVITY.yaml", {"agents": agents})

    lines = []
    lines.append(f"Project Progress: {ledger['execution']['progress_percentage']}%")
    lines.append("")
    lines.append("Completed:")
    for t in completed:
        lines.append(f"- {t['task_id']}: {t['title']}")
    lines.append("")
    lines.append("In Progress:")
    for t in active:
        lines.append(f"- {t['task_id']}: {t['title']} ({t.get('locked_by')})")
    lines.append("")
    lines.append("Pending:")
    for t in pending:
        lines.append(f"- {t['task_id']}: {t['title']}")
    lines.append("")
    lines.append("Next Actions (eligible now):")
    for t in eligible:
        lines.append(f"- {t['task_id']}: {t['title']}")
    write_text(proj / "planning" / "progress.md", "\n".join(lines) + "\n")

    ps = read_yaml(proj / "PROJECT_STATE.yaml")
    ps.setdefault("state", {})
    ps["state"]["phase"] = ledger["execution"]["phase"]
    ps["state"]["total_tasks"] = ledger["summary"]["total_tasks"]
    ps["state"]["completed_tasks"] = ledger["summary"]["completed_tasks"]
    ps["state"]["active_tasks"] = ledger["summary"]["active_tasks"]
    ps["state"]["failed_tasks"] = 0
    ps["state"]["progress_percentage"] = ledger["execution"]["progress_percentage"]
    ps["state"]["last_updated"] = utc_now_iso()
    write_yaml(proj / "PROJECT_STATE.yaml", ps)

    return {
        "ok": True,
        "project_id": project_id,
        "total": len(normalized),
        "completed": len(completed),
        "active": len(active),
        "pending": len(pending),
        "eligible_next": [t["task_id"] for t in eligible],
    }


def create_adr(
    project_dir: str,
    title: str,
    decision: str,
    context: str,
    consequences: str,
    alternatives: str | None = None,
) -> dict[str, Any]:
    proj = Path(project_dir).resolve()
    decisions_dir = proj / "docs" / "decisions"
    ensure_dir(decisions_dir)

    existing = sorted(decisions_dir.glob("ADR-*.md"))
    next_num = 1
    if existing:
        nums: list[int] = []
        for f in existing:
            m = re.search(r"ADR-(\d+)", f.name)
            if m:
                nums.append(int(m.group(1)))
        next_num = (max(nums) + 1) if nums else 1

    adr_id = f"ADR-{next_num:03d}"
    fname = f"{adr_id}-{slugify(title)}.md"
    content = textwrap.dedent(
        f"""\
    # {adr_id}: {title}

    ## Status
    Accepted

    ## Context
    {context}

    ## Decision
    {decision}

    ## Consequences
    {consequences}

    ## Alternatives Considered
    {alternatives or "N/A"}
    """
    )
    write_text(decisions_dir / fname, content)

    mem = read_yaml(proj / "memory" / "decisions.yaml")
    mem.setdefault("decisions", [])
    mem["decisions"].append(
        {
            "id": adr_id,
            "title": title,
            "decision": decision,
            "context": context,
            "consequences": consequences,
            "alternatives": alternatives or "",
            "timestamp": utc_now_iso(),
        }
    )
    write_yaml(proj / "memory" / "decisions.yaml", mem)

    return {"ok": True, "adr_id": adr_id, "file": str(decisions_dir / fname)}


def check_policy_gate(project_dir: str, gate: str | None = None) -> dict[str, Any]:
    proj = Path(project_dir).resolve()
    ok, errors = policy_gate_check(proj, gate=gate)
    if not ok:
        audit_log(
            proj,
            actor="skynet",
            violation_type="policy_gate_failed",
            details={"errors": errors, "gate": gate or "all"},
        )
    return {"ok": ok, "errors": errors}


ACTION_MAP = {
    "create_project": create_project,
    "generate_plan": generate_plan,
    "finalize_plan_and_enqueue": finalize_plan_and_enqueue,
    "sync_progress": sync_progress,
    "create_adr": create_adr,
    "check_policy_gate": check_policy_gate,
}


def handle(action: str, inputs: dict[str, Any]) -> dict[str, Any]:
    fn = ACTION_MAP.get(action)
    if fn is None:
        return {"ok": False, "error": f"Unknown action: {action}"}
    return fn(**(inputs or {}))
