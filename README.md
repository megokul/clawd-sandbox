# SKYNET Control Plane

Authoritative contract: `docs/SKYNET_OPENCLAW_CONTRACT.md`.

- OpenClaw executes tasks.
- SKYNET orchestrates OpenClaw gateways.
- SKYNET does not run agent runtime/tool execution logic.

## Active Scope

`skynet/` now contains control-plane code only:
- Gateway/worker registry
- Health-aware gateway routing
- System topology state

## API Endpoints

- `POST /v1/register-gateway`
- `POST /v1/register-worker`
- `POST /v1/route-task`
- `GET /v1/system-state`
- `GET /v1/health`

## Run

```bash
# SKYNET API
make run-api

# OpenClaw runtime (separate)
make run-bot
```

## Docker

```bash
# Full stack (SKYNET + OpenClaw gateway)
docker compose up -d skynet-api openclaw-gateway

# SKYNET only (when OpenClaw already runs elsewhere)
docker compose -f docker-compose.skynet-only.yml up -d
```

## Tests

```bash
make test
make smoke
```

Primary API tests:
- `tests/test_api_lifespan.py`
- `tests/test_api_provider_config.py`
- `tests/test_api_control_plane.py`

## Manual Integration

```bash
make manual-check-api
make manual-check-e2e
make manual-check-delegate
```

## External OpenClaw Skills (SKILL.md)

The gateway can load community OpenClaw `SKILL.md` files as prompt guidance.

- Local directory (recursive): `SKYNET_EXTERNAL_SKILLS_DIR`
- Optional remote URLs (comma-separated): `SKYNET_EXTERNAL_SKILL_URLS`

Example:

```bash
export SKYNET_EXTERNAL_SKILL_URLS="https://github.com/openclaw/skills/tree/main/skills/steipete/coding-agent/SKILL.md"
```

This is a prompt-level integration only. Execution remains constrained to built-in allowlisted tools/actions.

## OpenRouter Free Model Priority

When `OPENROUTER_API_KEY` is set, the gateway uses a priority chain and automatically moves to the next model when the current one is unavailable, rate-limited, or quota-exhausted.

| Priority | Model |
| --- | --- |
| 1 | `qwen/qwen3-next-80b-a3b-instruct:free` |
| 2 | `qwen/qwen3-vl-235b-a22b-thinking` |
| 3 | `openai/gpt-oss-120b:free` |
| 4 | `qwen/qwen3-coder:free` |
| 5 | `google/gemma-3n-e2b-it:free` |
| 6 | `deepseek/deepseek-r1-0528:free` |
| 7 | `meta-llama/llama-3.3-70b-instruct:free` |

You can override this chain with `OPENROUTER_MODEL` (primary) and `OPENROUTER_FALLBACK_MODELS` (comma-separated fallback list).

## Multi-API Provider Priority

The router can use all configured APIs and select providers by priority plus task needs.

Default provider priority:
1. `gemini`
2. `groq`
3. `openrouter`
4. `deepseek`
5. `openai`
6. `claude`
7. `ollama`

Override via `AI_PROVIDER_PRIORITY` (comma-separated).

## Autonomous Project Startup

Default behavior now supports end-to-end autonomous project execution:

1. New projects are created under `SKYNET_PROJECT_BASE_DIR` (default `E:\MyProjects`).
2. On project creation, the gateway bootstraps:
   - project directory
   - `README.md`
   - `git init` + initial commit
   - optional `gh_create_repo`
3. After enough idea detail (`AUTO_PLAN_MIN_IDEAS`, default `3`), the system can auto:
   - generate plan
   - approve plan
   - start execution
4. Progress updates are posted at milestone boundaries (`milestone_started`, `milestone_review`).

Key env flags:
- `SKYNET_PROJECT_BASE_DIR`
- `AUTO_BOOTSTRAP_PROJECT`
- `AUTO_CREATE_GITHUB_REPO`
- `AUTO_CREATE_GITHUB_PRIVATE`
- `AUTO_APPROVE_AND_START`
- `AUTO_PLAN_MIN_IDEAS`
- `AUTO_APPROVE_GIT_ACTIONS`
