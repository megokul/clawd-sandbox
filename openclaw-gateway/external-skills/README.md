# External OpenClaw Skills

Drop community `SKILL.md` folders here to make SKYNET/OpenClaw load them as
prompt guidance at startup.

Loaded sources:
- Local scan: every `SKILL.md` under this directory (recursive)
- Optional remote sync: `SKYNET_EXTERNAL_SKILL_URLS` (comma-separated GitHub URLs)

Example URL from awesome-openclaw-skills:

`https://github.com/openclaw/skills/tree/main/skills/steipete/coding-agent/SKILL.md`

Notes:
- These are prompt-only integrations (no auto-execution of arbitrary external code).
- Tool execution still uses the gateway's allowlisted built-in actions.
