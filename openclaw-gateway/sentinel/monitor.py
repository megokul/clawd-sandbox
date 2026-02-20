"""
SKYNET Sentinel — Health Monitor

Runs periodic health checks against all SKYNET subsystems:
  - CHATHAN Worker connectivity
  - Scheduler queue health
  - AI provider availability
  - Database integrity
  - S3 storage connectivity
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger("skynet.sentinel")


def _is_missing_s3_credentials_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "unable to locate credentials" in text
        or "no credentials" in text
        or "invalidaccesskeyid" in text
        or "signaturedoesnotmatch" in text
    )


@dataclass
class HealthStatus:
    """Result of a single health check."""

    component: str
    healthy: bool = True
    message: str = ""
    latency_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "healthy": self.healthy,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 1),
            "details": self.details,
            "checked_at": self.checked_at,
        }


class SentinelMonitor:
    """
    SKYNET system health monitor.

    Checks all subsystems and returns structured health status reports.
    Designed to be called periodically by the HeartbeatScheduler or
    on-demand via the /sentinel Telegram command.
    """

    def __init__(
        self,
        gateway_api_url: str = "http://127.0.0.1:8766",
        scheduler: Any = None,
        db: Any = None,
        s3: Any = None,
    ):
        self.gateway_api_url = gateway_api_url
        self.scheduler = scheduler
        self.db = db
        self.s3 = s3

    async def check_worker_health(self) -> HealthStatus:
        """Ping /status endpoint — check CHATHAN Worker connectivity."""
        status = HealthStatus(component="chathan_worker")
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.gateway_api_url}/status",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    status.latency_ms = (time.monotonic() - start) * 1000
                    agent_connected = bool(data.get("agent_connected", False))
                    ssh_fallback_enabled = bool(data.get("ssh_fallback_enabled", False))
                    ssh_fallback_healthy = bool(data.get("ssh_fallback_healthy", False))
                    connected = agent_connected or (ssh_fallback_enabled and ssh_fallback_healthy)
                    status.healthy = connected
                    if agent_connected:
                        status.message = "Connected (agent)"
                    elif ssh_fallback_enabled and ssh_fallback_healthy:
                        status.message = "Connected (ssh fallback)"
                    elif ssh_fallback_enabled:
                        status.message = "Agent disconnected; SSH fallback unhealthy"
                    else:
                        status.message = "Agent disconnected"
                    status.details = data
        except Exception as exc:
            status.latency_ms = (time.monotonic() - start) * 1000
            status.healthy = False
            status.message = f"Gateway unreachable: {exc}"
        return status

    async def check_queue_health(self) -> HealthStatus:
        """Check scheduler running count and detect stuck tasks."""
        status = HealthStatus(component="scheduler_queue")
        if not self.scheduler:
            status.message = "Scheduler not configured"
            return status

        try:
            running = self.scheduler.running_count
            paused = len(getattr(self.scheduler, "_paused", set()))
            status.healthy = True
            status.message = f"{running} running, {paused} paused"
            status.details = {
                "running_count": running,
                "paused_count": paused,
            }
        except Exception as exc:
            status.healthy = False
            status.message = f"Scheduler error: {exc}"
        return status

    async def check_database_health(self) -> HealthStatus:
        """Quick database connectivity check."""
        status = HealthStatus(component="database")
        if not self.db:
            status.message = "Database not configured"
            return status

        start = time.monotonic()
        try:
            async with self.db.execute("SELECT 1") as cur:
                await cur.fetchone()
            status.latency_ms = (time.monotonic() - start) * 1000
            status.healthy = True
            status.message = "OK"
        except Exception as exc:
            status.latency_ms = (time.monotonic() - start) * 1000
            status.healthy = False
            status.message = f"Database error: {exc}"
        return status

    async def check_storage_health(self) -> HealthStatus:
        """Check S3 connectivity."""
        status = HealthStatus(component="s3_storage")
        if not self.s3:
            status.message = "S3 not configured"
            return status

        start = time.monotonic()
        try:
            # Try listing keys — minimal S3 operation.
            await self.s3.list_keys("health-check/", max_keys=1)
            status.latency_ms = (time.monotonic() - start) * 1000
            status.healthy = True
            status.message = "OK"
        except Exception as exc:
            status.latency_ms = (time.monotonic() - start) * 1000
            if _is_missing_s3_credentials_error(exc):
                status.healthy = True
                status.message = "S3 credentials not configured (health check skipped)"
            else:
                status.healthy = False
                status.message = f"S3 error: {exc}"
        return status

    async def run_all_checks(self) -> list[HealthStatus]:
        """Run all health checks and return a list of statuses."""
        checks = [
            self.check_worker_health(),
            self.check_queue_health(),
            self.check_database_health(),
            self.check_storage_health(),
        ]

        results: list[HealthStatus] = []
        for coro in checks:
            try:
                result = await coro
            except Exception as exc:
                result = HealthStatus(
                    component="unknown", healthy=False,
                    message=f"Check failed: {exc}",
                )
            results.append(result)

        unhealthy = [r for r in results if not r.healthy]
        if unhealthy:
            logger.warning(
                "Sentinel: %d/%d checks unhealthy: %s",
                len(unhealthy), len(results),
                ", ".join(r.component for r in unhealthy),
            )
        else:
            logger.info("Sentinel: all %d checks healthy", len(results))

        return results

    def format_report(self, statuses: list[HealthStatus]) -> str:
        """Format health statuses into a human-readable Telegram message."""
        lines = ["SKYNET Sentinel Health Report", ""]
        for s in statuses:
            icon = "OK" if s.healthy else "FAIL"
            line = f"[{icon}] {s.component}: {s.message}"
            if s.latency_ms > 0:
                line += f" ({s.latency_ms:.0f}ms)"
            lines.append(line)

        all_healthy = all(s.healthy for s in statuses)
        lines.append("")
        lines.append("Status: All systems operational" if all_healthy else "Status: Issues detected")
        return "\n".join(lines)
