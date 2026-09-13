"""PagerDuty: the entry point (V3 webhook) and the close-out (resolve)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from config import settings
from demo import fixtures

from ._http import request

API = "https://api.pagerduty.com"


def live() -> bool:
    return settings.is_live(settings.pagerduty)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Token token={settings.pagerduty.required['PAGERDUTY_API_KEY']}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "From": settings.pagerduty_from_email,
        "Content-Type": "application/json",
    }


def verify_signature(body: bytes, header: str | None) -> bool:
    """PagerDuty signs V3 webhooks as `v1=<hex>`; unset secret means unverified dev."""
    secret = settings.pagerduty_webhook_secret
    if not secret:
        return True
    if not header:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(digest, part.strip().removeprefix("v1="))
               for part in header.split(",") if part.strip().startswith("v1="))


def parse_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a V3 webhook into the fields the Sentinel agent needs."""
    event = payload.get("event", payload)
    data = event.get("data", {}) or {}
    service = (data.get("service") or {}).get("summary", "unknown-service")
    priority = (data.get("priority") or {}).get("summary") or (
        "P1" if data.get("urgency") == "high" else "P3"
    )
    created = data.get("created_at") or event.get("occurred_at")
    try:
        started_at = datetime.fromisoformat((created or "").replace("Z", "+00:00"))
    except ValueError:
        started_at = datetime.now(timezone.utc)
    return {
        "pagerduty_id": data.get("id", ""),
        "number": data.get("number"),
        "event_type": event.get("event_type", "incident.triggered"),
        "title": data.get("title", ""),
        "service": service,
        "severity": priority,
        "started_at": started_at,
        "details": ((data.get("body") or {}).get("details") or ""),
        "html_url": data.get("html_url", ""),
    }


def demo_webhook(trigger: datetime | None = None) -> dict[str, Any]:
    return fixtures.pagerduty_webhook(trigger)


async def get_incident(incident_id: str) -> tuple[dict[str, Any], str]:
    if not live():
        return {"id": incident_id, "status": "triggered"}, "demo"
    data = await request("GET", f"{API}/incidents/{incident_id}", headers=_headers())
    return data.get("incident", {}), "live"


async def resolve_incident(incident_id: str, note: str = "") -> tuple[bool, str]:
    if not live():
        return True, "demo"
    await request(
        "PUT",
        f"{API}/incidents/{incident_id}",
        headers=_headers(),
        json={"incident": {"type": "incident_reference", "status": "resolved"}},
    )
    if note:
        try:
            await request(
                "POST",
                f"{API}/incidents/{incident_id}/notes",
                headers=_headers(),
                json={"note": {"content": note}},
            )
        except Exception:
            pass
    return True, "live"
