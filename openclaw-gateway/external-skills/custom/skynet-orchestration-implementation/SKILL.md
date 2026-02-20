---
name: skynet-orchestration-implementation
description: Implement SKYNET multi-agent orchestration features in this repository. Use when building or refactoring the Telegram main persona agent, planner/decomposer, manager/watcher supervision, worker lifecycle control, archivist reporting, persistent user memory/profile, proactive automation loops, and related database schema/API contracts.
---

# Skynet Orchestration Implementation

## Overview

Implement the orchestration architecture end-to-end with strict control-plane boundaries.
Preserve existing contract rules: SKYNET schedules and routes; OpenClaw gateway/worker executes.

## Workflow

1. Confirm scope and implementation phase before editing.
2. Load only the references needed for the current phase:
   - `references/rollout-plan.md` for sequence and acceptance gates
   - `references/agent-roles.md` for boundaries and responsibilities
   - `references/data-contracts.md` for schema/API/state-machine contracts
3. Implement in small vertical slices:
   - schema/migrations
   - storage/data access
   - service/controller logic
   - API/commands
   - tests
4. Enforce invariants in code:
   - legal state transitions only
   - idempotency on dispatch and execution
   - explicit heartbeat and timeout handling
   - auditable event stream for every lifecycle mutation
5. Validate with tests and summarize residual risks.

## Implementation Rules

- Keep layer boundaries explicit:
  - `skynet/`: control plane state, scheduler, routing, health, read models
  - `openclaw-gateway/`: transport and execution bridge, orchestration runtime, Telegram persona
  - `openclaw-agent/`: tool execution and local runtime behavior
- Prefer additive migrations and backward-compatible API changes.
- Add tests for:
  - legal/illegal transitions
  - retries and idempotent replay behavior
  - timeout/recovery paths
  - profile memory commands and policy controls
- Always produce operator-visible status artifacts:
  - task events
  - reports or summaries
  - audit entries for memory changes

## Output Checklist

- Updated schema and data access layer
- Updated API or command handlers
- Updated lifecycle docs/contracts
- Passing tests for new behavior
- Short implementation summary with next-step options

## References

- `references/rollout-plan.md`
- `references/agent-roles.md`
- `references/data-contracts.md`
