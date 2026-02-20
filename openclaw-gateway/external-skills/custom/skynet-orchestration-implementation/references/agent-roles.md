# Agent Roles and Responsibilities

Main Persona Agent:
- Interface for Telegram user.
- Loads profile + recent context.
- Decides direct reply vs delegated task.
- Never performs long task execution inline.

Planner Agent:
- Converts request into goals, subtasks, dependencies, deliverables, and success criteria.
- Produces queue-ready structured plan.

Manager/Watcher Agent:
- Monitors active runs, heartbeats, error patterns, and stall signals.
- Applies policies: retry, split, escalate, clarification gate.
- Does not execute specialist work directly.

Worker Agents:
- Execute specialist tasks through gateway/worker tooling.
- Stream progress, logs, and artifacts.

Archivist Agent:
- Creates task record, decision log, sources list, and final report.
- Ensures each task has complete documentation output.

Boundary rules:
- Control-plane authority in `skynet/`.
- Execution runtime in `openclaw-gateway/` and `openclaw-agent/`.
- Keep interfaces explicit and idempotent.
