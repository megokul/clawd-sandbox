# SKYNET Rollout Plan

Implement in this order:

1. User profile memory
- Add schema and APIs for profile facts, preferences, and memory policy controls.
- Add commands for show, forget, and memory on/off controls.
- Add extraction hook in Telegram main agent.

2. Agent role boundaries
- Keep explicit modules for main persona, planner, manager/watcher, workers, archivist.
- Route long-running requests to planner + queue, not inline chat execution.

3. Task events and heartbeats
- Emit lifecycle events for all state changes.
- Add heartbeat updates and stale/timeout rules.
- Manager/watcher handles retry, split, escalate policies.

4. Report generation
- Generate one report per completed task.
- Persist report metadata and artifact pointers.
- Return short summary to Telegram plus detailed artifact link/path.

5. Proactive loop
- Poll for due goals, stale tasks, alerts, and open loops.
- Auto-create tasks from triggers.
- Apply anti-spam gating and user notification thresholds.

Acceptance gate per phase:
- Schema/data access implemented
- Runtime logic implemented
- API/command surface implemented
- Tests passing
