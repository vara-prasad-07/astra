"""Sentinel — Incident Investigator.

Parses the inbound PagerDuty alert into the facts every other agent needs:
affected service, start time, error type, search window, suspected deployment.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from integrations import pagerduty
from models import Incident
from orchestrator.state import SwarmState

from .base import Agent

DEPLOY_RE = re.compile(r"deploy(?:ment)?[\s#:]*([a-zA-Z0-9._-]{2,40})", re.IGNORECASE)
ERROR_RE = re.compile(r"\b([A-Z][A-Za-z]*(?:Exception|Error))\b")


class Sentinel(Agent):
    name = "Incident Investigator"
    codename = "sentinel"
    source = "pagerduty"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        parsed = pagerduty.parse_webhook(state.get("raw_alert", {}))
        blob = f"{parsed['title']} {parsed['details']}"

        deploy_match = DEPLOY_RE.search(blob)
        suspected = deploy_match.group(1) if deploy_match else None
        error_match = ERROR_RE.search(blob)
        error_signature = error_match.group(1) if error_match else ""

        # The model helps on messy free-text alerts; regex already covers the
        # common shape, so its answer is only accepted when ours came up empty.
        enriched = await self.llm.analyze(
            system=(
                "You are an incident intake analyst. Extract structured fields from a "
                "production alert. Keys: service, error_signature, suspected_deployment, "
                "window_minutes (integer)."
            ),
            prompt=f"Alert title: {parsed['title']}\nDetails: {parsed['details']}",
            fallback={
                "service": parsed["service"],
                "error_signature": error_signature,
                "suspected_deployment": suspected,
                "window_minutes": 30,
            },
        )

        service = parsed["service"] or str(enriched.get("service") or "unknown-service")
        if not error_signature:
            error_signature = str(enriched.get("error_signature") or "")
        if not suspected:
            candidate = enriched.get("suspected_deployment")
            suspected = str(candidate) if candidate else None
        try:
            window = int(enriched.get("window_minutes") or 30)
        except (TypeError, ValueError):
            window = 30

        incident = Incident(
            id=state["incident_id"],
            service=service,
            severity=parsed["severity"],
            status="investigating",
            title=parsed["title"],
            started_at=parsed["started_at"],
            error_signature=error_signature,
            suspected_deployment=suspected,
            window_minutes=max(5, min(window, 180)),
            pagerduty_id=parsed["pagerduty_id"],
        )

        evidence = self.evidence(
            "incident_intake",
            f"{incident.severity} on {incident.service}: {incident.title}",
            service=incident.service,
            severity=incident.severity,
            started_at=incident.started_at.isoformat(),
            suspected_deployment=suspected,
            pagerduty_id=incident.pagerduty_id,
            html_url=parsed["html_url"],
        )

        search_start = incident.started_at - timedelta(minutes=incident.window_minutes)
        return {
            "incident": incident.model_dump(mode="json"),
            "trigger_time": incident.started_at.isoformat(),
            "search_start": search_start.isoformat(),
            "evidence": [evidence.model_dump(mode="json")],
            "modes": {"sentinel": "live" if pagerduty.live() else "demo"},
            "status": "investigating",
            "_detail": f"{incident.service} / {incident.severity}",
        }
