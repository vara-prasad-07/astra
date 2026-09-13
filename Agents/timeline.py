"""Timeline — deploy-to-incident causality.

Aligns every dated fact the other agents produced onto one clock, then derives
the before/after relationships the scorer needs: did the deployment precede the
errors, and was this error signature absent before it?
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.state import SwarmState

from .base import Agent, describe_change


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _find(evidence: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for item in evidence:
        if item.get("kind") == kind:
            return item
    return None


class Timeline(Agent):
    name = "Timeline Detective"
    codename = "timeline"
    source = "correlated"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        evidence = state.get("evidence") or []
        trigger = _parse_ts(state.get("trigger_time")) or datetime.now(timezone.utc)

        events: list[dict[str, Any]] = []

        deployment = _find(evidence, "deployment")
        deploy_at = None
        if deployment:
            detail = deployment.get("detail") or {}
            deploy_at = _parse_ts(detail.get("deployed_at"))
            if deploy_at:
                events.append(
                    {
                        "at": deploy_at,
                        "kind": "deploy",
                        "source": "github",
                        "description": f"Deployment {detail.get('deployment_id')} "
                        f"({detail.get('version')}) shipped",
                    }
                )

        code_match = _find(evidence, "code_change_match")
        if code_match and (code_match.get("detail") or {}).get("pr_number"):
            detail = code_match["detail"]
            merged_at = _parse_ts(detail.get("merged_at"))
            if merged_at:
                events.append(
                    {
                        "at": merged_at,
                        "kind": "code",
                        "source": "github",
                        "description": f"PR #{detail['pr_number']} \"{detail.get('title','')}\" "
                        f"merged by {detail.get('author','unknown')}",
                    }
                )

        for item in evidence:
            if item.get("kind") != "metric_shift":
                continue
            detail = item.get("detail") or {}
            shifted = _parse_ts(detail.get("shifted_at"))
            if shifted:
                events.append(
                    {
                        "at": shifted,
                        "kind": "metric",
                        "source": "datadog",
                        "description": f"{detail.get('metric')} "
                        f"{describe_change(detail)} vs baseline",
                    }
                )

        error_pattern = _find(evidence, "error_pattern")
        first_error = None
        window_start = None
        if error_pattern:
            detail = error_pattern.get("detail") or {}
            first_error = _parse_ts(detail.get("first_seen"))
            window_start = _parse_ts(detail.get("window_start"))
            if first_error:
                events.append(
                    {
                        "at": first_error,
                        "kind": "error",
                        "source": "datadog",
                        "description": f"first {detail.get('error_type')} "
                        f"({detail.get('count')} total in window)",
                    }
                )

        events.append(
            {
                "at": trigger,
                "kind": "alert",
                "source": "pagerduty",
                "description": "PagerDuty incident triggered",
            }
        )
        events.sort(key=lambda item: item["at"])

        deploy_precedes_errors = bool(
            deploy_at and first_error and deploy_at < first_error
        )
        gap_seconds = (
            (first_error - deploy_at).total_seconds()
            if deploy_at and first_error
            else None
        )
        signature_new = bool(
            deploy_at
            and first_error
            and window_start
            and window_start < deploy_at < first_error
        )

        timeline = [
            {
                "at": item["at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "clock": item["at"].astimezone(timezone.utc).strftime("%H:%M:%S"),
                "kind": item["kind"],
                "source": item["source"],
                "description": item["description"],
            }
            for item in events
        ]

        summary = (
            f"Deployment preceded first error by {gap_seconds / 60:.1f} min"
            if deploy_precedes_errors and gap_seconds is not None
            else "No deployment/error ordering could be established"
        )

        causality = self.evidence(
            "causality",
            summary,
            deploy_precedes_errors=deploy_precedes_errors,
            deploy_to_first_error_seconds=gap_seconds,
            signature_new_since_deploy=signature_new,
            deployed_at=deploy_at.isoformat() if deploy_at else None,
            first_error_at=first_error.isoformat() if first_error else None,
            searched_from=window_start.isoformat() if window_start else None,
            events=timeline,
        ).model_dump(mode="json")

        return {
            "timeline": timeline,
            "evidence": [causality],
            "modes": {self.codename: "correlated"},
            "_detail": summary,
        }
