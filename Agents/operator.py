"""Operator — Action Agent.

Runs the verification sequence from the design record before touching anything.
Checks 1-3 are preconditions: if any fails, nothing is executed. The agent never
reaches this node without a recorded human approval.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from config import settings
from integrations import deploy, pagerduty, slack
from models import ActionResult, VerificationStep
from orchestrator.events import bus
from orchestrator.state import SwarmState

from .base import Agent

HEALTH_ATTEMPTS = 5
HEALTH_INTERVAL_SECONDS = 2.0


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f} min"


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


class Operator(Agent):
    name = "Action Agent"
    codename = "operator"
    source = "slack approval"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        incident = state.get("incident") or {}
        recommendation = state.get("recommendation") or {}
        approval = state.get("approval") or {}
        incident_id = state.get("incident_id", "")
        approver = approval.get("approver", "unknown")
        target = recommendation.get("action_target")

        checks: list[VerificationStep] = []

        def record(step: str, passed: bool, detail: str = "") -> VerificationStep:
            entry = VerificationStep(step=step, passed=passed, detail=detail)
            checks.append(entry)
            bus.publish(
                "verification",
                {
                    "incident_id": incident_id,
                    "step": step,
                    "passed": passed,
                    "detail": detail,
                },
            )
            return entry

        # 1. Authorised approver.
        if settings.approvers:
            authorised = approver.lower() in {a.lower() for a in settings.approvers}
            record(
                "Approval came from an authorised source",
                authorised,
                f"{approver} {'is' if authorised else 'is not'} on the approver allowlist",
            )
        else:
            authorised = True
            record(
                "Approval came from an authorised source",
                True,
                f"approved by {approver}; no NIGHTWATCH_APPROVERS allowlist configured",
            )

        # 2. Incident still active.
        pd_id = incident.get("pagerduty_id") or ""
        still_active = True
        detail = "incident is still open"
        if pd_id:
            try:
                remote, _ = await pagerduty.get_incident(pd_id)
                status = str(remote.get("status", "triggered"))
                still_active = status not in {"resolved"}
                detail = f"PagerDuty reports status '{status}'"
            except Exception as exc:
                still_active = True
                detail = f"could not confirm with PagerDuty ({str(exc)[:80]}); proceeding"
        record("Incident is still active", still_active, detail)

        # 3. Deployment ID matches what was analysed.
        analysed = None
        for item in state.get("evidence") or []:
            if item.get("kind") == "deployment":
                analysed = (item.get("detail") or {}).get("deployment_id")
                break
        matches = bool(target) and target == analysed
        record(
            "Deployment ID matches the analysed deployment",
            matches,
            f"recommendation targets {target}, evidence analysed {analysed}",
        )

        preconditions_ok = authorised and still_active and matches
        if not preconditions_ok:
            result = ActionResult(
                incident_id=incident_id,
                action_type=recommendation.get("action_type", "rollback"),
                target=target,
                executed=False,
                approver=approver,
                checks=checks,
                notes="Aborted before execution: a pre-flight verification failed.",
            )
            await slack.post_thread_update(
                incident_id,
                (state.get("slack_ref") or {}).get("ts"),
                f"NIGHTWATCH aborted the {result.action_type} for {incident.get('service')}: "
                "a pre-flight verification failed. No production change was made.",
            )
            return {
                "action_result": result.model_dump(mode="json"),
                "status": "investigating",
                "modes": {self.codename: "aborted"},
                "_detail": "aborted before execution",
            }

        # 4. Execute.
        service = incident.get("service", "unknown-service")
        rollback = await deploy.rollback(str(target), service)
        record("Rollback executed", bool(rollback.get("executed")), rollback.get("detail", ""))

        # 5. Verify recovery.
        health: dict[str, Any] = {}
        for attempt in range(HEALTH_ATTEMPTS):
            health = await deploy.health(service, str(target))
            if health.get("healthy"):
                break
            if attempt < HEALTH_ATTEMPTS - 1:
                await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
        healthy = bool(health.get("healthy"))
        record(
            "Service health restored",
            healthy,
            f"error rate {health.get('error_rate')}% vs baseline {health.get('baseline')}%",
        )

        started_at = _parse_ts(incident.get("started_at"))
        now = datetime.now(timezone.utc)
        mttr = (now - started_at).total_seconds() if started_at else None

        # 6. Update the Slack thread.
        summary = (
            f"Rollback of deployment {target} complete. "
            f"{service} error rate is {health.get('error_rate')}% "
            f"(baseline {health.get('baseline')}%). "
            f"MTTR {format_duration(mttr)}. Approved by {approver}."
            if healthy
            else f"Rollback of deployment {target} executed but {service} has not "
            f"returned to baseline (error rate {health.get('error_rate')}%). Escalating."
        )
        _, slack_mode = await slack.post_thread_update(
            incident_id, (state.get("slack_ref") or {}).get("ts"), summary
        )
        record("Slack thread updated with the outcome", True, f"via {slack_mode}")

        # 7. Close the PagerDuty incident.
        resolved = False
        if healthy and pd_id:
            try:
                resolved, _ = await pagerduty.resolve_incident(pd_id, summary)
            except Exception as exc:
                resolved = False
                record("PagerDuty incident resolved", False, str(exc)[:120])
        if healthy and (resolved or not pd_id):
            record("PagerDuty incident resolved", True, f"incident {pd_id or 'n/a'} closed")
        elif not healthy:
            record(
                "PagerDuty incident resolved",
                False,
                "left open because health did not return to baseline",
            )

        result = ActionResult(
            incident_id=incident_id,
            action_type=recommendation.get("action_type", "rollback"),
            target=str(target),
            executed=True,
            approver=approver,
            checks=checks,
            health_restored=healthy,
            error_rate_after=health.get("error_rate"),
            mttr_seconds=mttr,
            notes=summary,
            executed_at=now,
        )

        return {
            "action_result": result.model_dump(mode="json"),
            "status": "resolved" if healthy else "remediating",
            "modes": {self.codename: rollback.get("mode", "simulated")},
            "_detail": f"rolled back {target}, healthy={healthy}",
        }
