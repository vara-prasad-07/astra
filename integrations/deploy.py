"""Deployment control: the one place that can change production state.

If the breakable demo microservice is running, the rollback is genuinely real —
the service stops erroring and the health check observes it. Otherwise the
rollback is simulated against fixtures. Either way the Operator's verification
sequence is identical.
"""

from __future__ import annotations

from typing import Any

import httpx

from config import settings
from demo import fixtures

_simulated_rolled_back: set[str] = set()


async def _demo_service_call(path: str, method: str = "GET") -> dict[str, Any] | None:
    """Best-effort call to the local breakable service; None if it isn't running."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0)) as client:
            response = await client.request(method, f"{settings.demo_service_url}{path}")
            if response.status_code >= 400:
                return None
            return response.json()
    except Exception:
        return None


async def service_is_reachable() -> bool:
    return await _demo_service_call("/health") is not None


async def rollback(deployment_id: str, service: str) -> dict[str, Any]:
    live_result = await _demo_service_call("/admin/rollback", method="POST")
    if live_result is not None:
        return {
            "executed": True,
            "mode": "live-demo-service",
            "deployment_id": deployment_id,
            "now_running": live_result.get("version"),
            "detail": f"{service} rolled back to {live_result.get('version')}",
        }
    _simulated_rolled_back.add(deployment_id)
    previous = next(
        (
            item["previous_version"]
            for item in fixtures.deployments()
            if item["id"] == deployment_id
        ),
        "v1.2.7",
    )
    return {
        "executed": True,
        "mode": "simulated",
        "deployment_id": deployment_id,
        "now_running": previous,
        "detail": f"{service} rolled back to {previous} (simulated)",
    }


async def health(service: str, deployment_id: str | None = None) -> dict[str, Any]:
    """Post-remediation health: error rate should be back at baseline."""
    stats = await _demo_service_call("/admin/stats")
    if stats is not None:
        total = max(1, stats.get("requests", 0))
        error_rate = stats.get("errors", 0) / total * 100.0
        return {
            "mode": "live-demo-service",
            "error_rate": round(error_rate, 3),
            "baseline": 0.05,
            "healthy": error_rate < 1.0,
            "version": stats.get("version"),
        }

    rolled_back = deployment_id in _simulated_rolled_back
    metrics = (
        fixtures.post_rollback_metrics() if rolled_back else fixtures.datadog_metrics()
    )
    error_rate = metrics["series"]["http.5xx.rate"]["current"]
    return {
        "mode": "simulated",
        "error_rate": error_rate,
        "baseline": metrics["series"]["http.5xx.rate"]["baseline"],
        "healthy": error_rate < 1.0,
        "version": None,
    }


def reset() -> None:
    _simulated_rolled_back.clear()
