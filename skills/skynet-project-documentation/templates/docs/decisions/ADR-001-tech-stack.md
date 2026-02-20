# ADR-001: Tech Stack

## Status
Accepted

## Context
Need a reliable stack for control plane plus gateway plus workers.

## Decision
- FastAPI for control plane
- OpenClaw gateway runtime
- SQLite now, PostgreSQL later

## Consequences
- Fast iteration now
- Clear scaling roadmap

## Alternatives Considered
- Flask
- Node-only stack
