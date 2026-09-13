"""Slack Notifier — posts the report and opens the approval gate.

This node is the hard stop. It never routes onward to the Operator; the graph
ends here and only an inbound human decision starts the remediation phase.
"""

from __future__ import annotations

from typing import Any

from integrations import slack
from models import Recommendation
from orchestrator.state import SwarmState

from .base import Agent


class SlackNotifier(Agent):
    name = "Slack Notifier"
    codename = "notify"
    source = "slack"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        incident = state.get("incident") or {}
        recommendation = Recommendation(**(state.get("recommendation") or {}))
        incident_id = state.get("incident_id", "")

        timeline = [
            f"{event['clock']}  {event['description']}"
            for event in (state.get("timeline") or [])
        ]
        ranked = [
            (h.get("name", ""), float(h.get("score", 0.0)))
            for h in (state.get("hypotheses") or [])
        ]

        blocks = slack.build_report_blocks(
            incident_id=incident_id,
            service=incident.get("service", "unknown-service"),
            recommendation=recommendation,
            timeline=timeline,
            hypotheses=ranked,
        )
        text = (
            f"NIGHTWATCH: {recommendation.root_cause} "
            f"({recommendation.confidence:.0%} confidence) — "
            f"{recommendation.action_type} awaiting approval"
        )
        ref, mode = await slack.post_report(incident_id, text, blocks)

        return {
            "slack_ref": ref,
            "status": "awaiting_approval",
            "modes": {self.codename: mode},
            "_detail": f"posted to {ref.get('channel', 'slack')}",
        }
