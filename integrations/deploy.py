"""Deployment control: the one place that can change production state.

Three tiers, most-real first: if Render is configured, rollback means asking
Render to redeploy the build that was live before the bad one — a real
production rollback. Otherwise, if the breakable demo microservice is
reachable, rollback flips its in-process version flag — still a real HTTP
call and a real state change, just not a real redeploy. Otherwise the
rollback is simulated against fixtures. Either way the Operator's
verification sequence is identical.
"""

from __future__ import annotations

from typing import Any

import httpx

from config import settings
from demo import fixtures

from . import render

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


async def active_deployment(service: str) -> tuple[dict[str, Any] | None, str]:
    """What's really running, from the most authoritative source available.

    Preferred over GitHub's deployment fixtures/API: those carry a synthetic
    or unrelated deployed_at, which breaks the deploy-precedes-errors causality
    check against a *real* incident's real error timestamps.
    """
    if render.live():
        result, mode = await render.active_deployment(service)
        if result is not None:
            return result, mode

    stats = await _demo_service_call("/admin/stats")
    if stats is None or not stats.get("deployed_at"):
        return None, "unreachable"
    return (
        {
            "id": stats.get("deployment"),
            "service": service,
            "version": stats.get("version"),
            "previous_version": stats.get("previous_version") or "",
            "deployed_at": stats.get("deployed_at"),
            "pr_number": None,
            "sha": "",
            "status": "active",
        },
        "live-demo-service",
    )


async def rollback(deployment_id: str, service: str) -> dict[str, Any]:
    if render.live():
        # This is the real production path: Render redeploys the previous
        # build. Its result is authoritative — don't fall through to the
        # weaker tiers below even if it reports failure.
        return await render.rollback(deployment_id, service)

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
