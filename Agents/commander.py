"""Commander — Incident Commander.

Takes the ranked hypotheses, assesses the risk of acting, and issues exactly one
recommendation. Below the confidence floor it recommends investigation rather
than a production change. The model phrases the summary; the thresholds decide it.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from models import Recommendation
from orchestrator.state import SwarmState

from .base import Agent, describe_change
from .correlation import EvidencePool

ACTION_CONFIDENCE_FLOOR = 0.60
LOW_RISK_CONFIDENCE = 0.80
LOW_RISK_MARGIN = 0.30


class Commander(Agent):
    name = "Incident Commander"
    codename = "commander"
    source = "correlation"

    def _bullets(self, pool: EvidencePool) -> list[str]:
        bullets: list[str] = []

        causality = pool.first("causality")
        gap = causality.get("deploy_to_first_error_seconds")
        if gap is not None:
            bullets.append(f"Errors began {gap / 60:.1f} min after the deployment")

        error = pool.first("error_pattern")
        if error:
            bullets.append(
                f"{error.get('count')} matching {error.get('error_type')} errors found "
                f"in logs for {error.get('service')}"
            )

        match = pool.first("code_change_match")
        if match.get("pr_number"):
            files = ", ".join(
                PurePosixPath(path).name for path in match.get("matched_files", [])
            )
            bullets.append(
                f"PR #{match['pr_number']} by {match.get('author')} modified {files}, "
                "which appears in the failing stack trace"
            )

        if causality.get("signature_new_since_deploy"):
            bullets.append(
                "The exception does not appear in logs from before the deployment"
            )

        for detail in pool.all("metric_shift"):
            bullets.append(
                f"{detail.get('metric')} {describe_change(detail)} inside the "
                "incident window"
            )

        for detail in pool.all("metric_elevated"):
            bullets.append(
                f"Ruled out: {detail.get('metric')} is elevated but stepped before the "
                "window, so it did not change when errors began"
            )

        db_errors = pool.first("database_errors").get("count", 0)
        if not db_errors:
            bullets.append("Ruled out: no database errors in the incident window")

        return bullets

    def _fallback_root_cause(self, pool: EvidencePool) -> str:
        error = pool.first("error_pattern")
        match = pool.first("code_change_match")
        deployment = pool.first("deployment")
        service = pool.incident.get("service", "the service")
        refs = error.get("code_refs") or []
        location = PurePosixPath(refs[0]["file"]).name if refs else "the failing code path"
        deploy_id = deployment.get("deployment_id", "unknown")
        pr = f" (PR #{match['pr_number']})" if match.get("pr_number") else ""
        return (
            f"{service} deployment {deploy_id}{pr} introduced "
            f"{'a ' + error.get('error_type', 'an error') if error else 'a fault'} "
            f"in {location}"
        )

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        hypotheses = state.get("hypotheses") or []
        pool = EvidencePool(state.get("evidence") or [], state.get("incident") or {})
        if not hypotheses:
            raise RuntimeError("commander invoked with no scored hypotheses")

        top = hypotheses[0]
        runner_up = hypotheses[1] if len(hypotheses) > 1 else None
        confidence = float(top.get("score", 0.0))
        margin = confidence - float(runner_up.get("score", 0.0)) if runner_up else confidence

        deployment = pool.first("deployment")
        deployment_id = deployment.get("deployment_id")
        previous_version = deployment.get("previous_version")

        can_roll_back = bool(
            top.get("name") == "Bad deployment"
            and deployment_id
            and confidence >= ACTION_CONFIDENCE_FLOOR
        )
        action_type = "rollback" if can_roll_back else "investigate"

        if not can_roll_back:
            risk, risk_notes = (
                "high",
                f"Confidence {confidence:.0%} is below the {ACTION_CONFIDENCE_FLOOR:.0%} "
                "floor for an automated production change, or no rollback target exists",
            )
        elif confidence >= LOW_RISK_CONFIDENCE and margin >= LOW_RISK_MARGIN and previous_version:
            risk, risk_notes = (
                "low",
                f"Rolling back to known-good {previous_version}; next hypothesis is "
                f"{margin:.0%} behind",
            )
        else:
            risk, risk_notes = (
                "medium",
                f"Confidence {confidence:.0%} with a {margin:.0%} margin over the next "
                "hypothesis",
            )

        bullets = self._bullets(pool)
        fallback_root_cause = self._fallback_root_cause(pool)

        narrative = await self.llm.analyze(
            system=(
                "You are an incident commander writing the one-line root cause for an "
                "on-call engineer. Be specific and factual, use only the supplied "
                "evidence, and never invent identifiers. Keys: root_cause (one "
                "sentence), risk_notes (one short sentence)."
            ),
            prompt=(
                f"Service: {pool.incident.get('service')}\n"
                f"Leading hypothesis: {top.get('name')} at {confidence:.0%}\n"
                f"Evidence:\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
                f"Proposed action: {action_type} {deployment_id or ''}"
            ),
            fallback={"root_cause": fallback_root_cause, "risk_notes": risk_notes},
        )

        recommendation = Recommendation(
            hypothesis=top.get("name", "unknown"),
            root_cause=str(narrative.get("root_cause") or fallback_root_cause).strip(),
            confidence=confidence,
            action_type=action_type,
            action_target=deployment_id if action_type == "rollback" else None,
            risk=risk,
            risk_notes=str(narrative.get("risk_notes") or risk_notes).strip(),
            evidence_bullets=bullets,
            requires_approval=True,
        )

        return {
            "recommendation": recommendation.model_dump(mode="json"),
            "evidence": [
                self.evidence(
                    "recommendation",
                    f"{action_type} recommended at {confidence:.0%} confidence "
                    f"({risk} risk)",
                    action_type=action_type,
                    target=recommendation.action_target,
                    confidence=confidence,
                    risk=risk,
                ).model_dump(mode="json")
            ],
            "status": "awaiting_approval",
            "modes": {self.codename: self.llm.name},
            "_detail": f"{action_type} @ {confidence:.0%}",
        }
