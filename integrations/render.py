"""Render: real deploy history and a real rollback.

Rollback here means what it means in production — asking Render to redeploy
the build that was live before the bad one, via its official rollback
endpoint (https://api-docs.render.com/reference/rollback-deploy) — not
flipping a flag inside the running process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import settings

from ._http import request

API = "https://api.render.com/v1"

# Deploys whose build artifact is actually still there to roll back to.
SUCCESSFUL_STATUSES = {"live", "deactivated"}
TERMINAL_STATUSES = {
    "live",
    "deactivated",
    "build_failed",
    "update_failed",
    "canceled",
    "pre_deploy_failed",
}


def live() -> bool:
    return settings.is_live(settings.render)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.render.required['RENDER_API_KEY']}"}


def _service_id() -> str:
    return settings.render.required["RENDER_SERVICE_ID"]


async def _list_deploys(limit: int = 20) -> list[dict[str, Any]]:
    data = await request(
        "GET",
        f"{API}/services/{_service_id()}/deploys",
        headers=_headers(),
        params={"limit": limit},
    )
    items = data if isinstance(data, list) else []
    # List responses wrap each entry as {"deploy": {...}, "cursor": "..."}.
    return [item.get("deploy", item) if isinstance(item, dict) else item for item in items]


def _sorted_desc(deploys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(deploys, key=lambda d: d.get("createdAt") or "", reverse=True)


def _predecessor(ordered: list[dict[str, Any]], of: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent successful deploy strictly before `of`."""
    of_created = of.get("createdAt") or ""
    return next(
        (
            d
            for d in ordered
            if d.get("id") != of.get("id")
            and d.get("status") in SUCCESSFUL_STATUSES
            and (d.get("createdAt") or "") < of_created
        ),
        None,
    )


async def active_deployment(service: str) -> tuple[dict[str, Any] | None, str]:
    """What Render itself says is live right now, and what came before it."""
    if not live():
        return None, "unconfigured"

    deploys = _sorted_desc(await _list_deploys(limit=20))
    if not deploys:
        return None, "live"

    current = next((d for d in deploys if d.get("status") == "live"), deploys[0])
    previous = _predecessor(deploys, current)

    commit = current.get("commit") or {}
    prev_commit = (previous or {}).get("commit") or {}

    return (
        {
            "id": current.get("id"),
            "service": service,
            "version": (commit.get("id") or current.get("id") or "")[:7],
            "previous_version": (prev_commit.get("id") or "")[:7],
            "deployed_at": current.get("createdAt"),
            "pr_number": None,
            "sha": commit.get("id", ""),
            "commit_message": commit.get("message", ""),
            "status": "active",
        },
        "live",
    )


async def _wait_until_terminal(
    deploy_id: str, timeout_seconds: float = 240.0, interval: float = 5.0
) -> dict[str, Any]:
    elapsed = 0.0
    deploy: dict[str, Any] = {}
    while elapsed < timeout_seconds:
        data = await request(
            "GET", f"{API}/services/{_service_id()}/deploys/{deploy_id}", headers=_headers()
        )
        deploy = data.get("deploy", data) if isinstance(data, dict) else {}
        if deploy.get("status") in TERMINAL_STATUSES:
            return deploy
        await asyncio.sleep(interval)
        elapsed += interval
    return deploy


async def rollback(bad_deployment_id: str, service: str) -> dict[str, Any]:
    """Roll back to the deploy immediately before `bad_deployment_id`.

    Blocks until Render reports the rollback deploy has reached a terminal
    state (live, or failed) — the caller gets a real, verified answer rather
    than an optimistic "accepted".
    """
    deploys = _sorted_desc(await _list_deploys(limit=20))
    bad = next((d for d in deploys if d.get("id") == bad_deployment_id), None)
    if bad is None:
        # The analysed deploy has scrolled out of recent history; act on
        # whatever is live now instead of guessing.
        bad = next((d for d in deploys if d.get("status") == "live"), deploys[0] if deploys else None)
    if bad is None:
        return {"executed": False, "mode": "render", "deployment_id": bad_deployment_id,
                 "detail": "no deploy history available from Render"}

    target = _predecessor(deploys, bad)
    if target is None:
        return {
            "executed": False,
            "mode": "render",
            "deployment_id": bad_deployment_id,
            "detail": "no earlier successful deploy found to roll back to",
        }

    result = await request(
        "POST",
        f"{API}/services/{_service_id()}/rollback",
        headers=_headers(),
        json={"deployId": target["id"]},
    )
    new_deploy = result.get("deploy", result) if isinstance(result, dict) else {}
    final = await _wait_until_terminal(new_deploy.get("id") or target["id"])

    version = ((target.get("commit") or {}).get("id") or target.get("id", ""))[:7]
    healthy = final.get("status") == "live"
    return {
        "executed": healthy,
        "mode": "render",
        "deployment_id": bad_deployment_id,
        "now_running": version,
        "detail": (
            f"{service} rolled back to Render deploy {target['id']} ({version}); "
            f"final status={final.get('status', 'unknown')}"
        ),
    }
