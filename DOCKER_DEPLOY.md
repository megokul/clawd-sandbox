# Docker Deployment

## What You Need

- OpenClaw gateway running (execution plane)
- SKYNET API running (control plane)
- Shared network path: SKYNET must reach OpenClaw HTTP API (`/status`, `/action`)
- Persistent volume for SKYNET DB (`/app/data/skynet.db`)

Contract: `docs/SKYNET_OPENCLAW_CONTRACT.md`.

## Mode A: Full Stack From This Repo

Use when you want this repo to run both SKYNET and OpenClaw gateway.

```bash
cp .env.example .env
docker compose build
docker compose up -d skynet-api openclaw-gateway
```

Notes:
- `docker-compose.yml` now keeps gateway HTTP `8766` internal to Docker network.
- Gateway WebSocket `8765` remains published for worker connectivity.
- Gateway HTTP bind is configurable via `OPENCLAW_HTTP_HOST`/`OPENCLAW_HTTP_PORT`.

## Mode B: SKYNET Only (OpenClaw Already Running)

Use when OpenClaw is already deployed separately (your EC2 setup).

1. Ensure your existing OpenClaw gateway exposes reachable HTTP API:
- either on host: `http://host.docker.internal:8766`
- or by container DNS on a shared Docker network

2. Configure `.env`:
- `OPENCLAW_GATEWAY_URL=<reachable-openclaw-url>`
- `SKYNET_API_KEY=<strong-secret>`

3. Start SKYNET only:

```bash
docker compose -f docker-compose.skynet-only.yml build
docker compose -f docker-compose.skynet-only.yml up -d
```

## Verify

```bash
curl http://localhost:8000/v1/health
curl -H "X-API-Key: $SKYNET_API_KEY" http://localhost:8000/v1/system-state
curl -X POST http://localhost:8000/v1/route-task \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $SKYNET_API_KEY" \
  -d '{"action":"git_status","params":{"working_dir":"."},"confirmed":true}'
```

## EC2 Security Checklist

- Expose only what you need:
  - `8000` (SKYNET API) to trusted CIDRs/VPN/ALB
  - `8765` (OpenClaw WS) if remote workers connect directly
- Do not expose gateway HTTP `8766` publicly.
