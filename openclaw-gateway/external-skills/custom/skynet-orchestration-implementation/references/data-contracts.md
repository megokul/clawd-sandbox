# Data and Contract Requirements

Core tables:
- users
- user_profile_facts
- user_preferences
- user_conversations
- memory_audit_log
- control_tasks
- control_task_events
- task_artifacts
- agent_runs
- proactive_rules

Task lifecycle states:
- queued
- claimed
- running
- succeeded
- failed
- released
- failed_timeout

Legal transition policy:
- queued -> claimed
- released -> claimed
- claimed -> running | released | failed | failed_timeout
- running -> succeeded | failed | released | failed_timeout
- Reject all other transitions.

Idempotency contract:
- Control plane sends `task_id` + `claim_token` as idempotency key on dispatch.
- Gateway caches `(task_id, idempotency_key) -> execution_result`.
- Worker deduplicates execution by same key.

Read model endpoints:
- `GET /v1/tasks/next?agent_id=...`
- `GET /v1/agents`
- `GET /v1/events`

Memory policy commands:
- show profile state
- forget specific facts
- disable/enable memory capture
- skip storing current message/chat when requested
