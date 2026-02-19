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
