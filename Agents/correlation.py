"""Correlation Agent — the reasoning core.

Scoring here is deliberately deterministic and weighted, not model-generated.
Each hypothesis is a set of named signals evaluated against the evidence pool,
so the verdict can be audited line by line and reproduced exactly. The model
writes the narrative later; it never decides the outcome.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from models import Hypothesis, Signal
from orchestrator.state import SwarmState

from .base import Agent

Evaluator = Callable[["EvidencePool"], tuple[bool, str]]


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


class EvidencePool:
    """Typed read access to everything the investigation agents produced."""

    def __init__(self, evidence: list[dict[str, Any]], incident: dict[str, Any]) -> None:
        self.items = evidence
        self.incident = incident

    def first(self, kind: str) -> dict[str, Any]:
        for item in self.items:
            if item.get("kind") == kind:
                return item.get("detail") or {}
        return {}

    def all(self, kind: str) -> list[dict[str, Any]]:
        return [
            item.get("detail") or {} for item in self.items if item.get("kind") == kind
        ]

    def summary_for(self, kind: str) -> str:
        for item in self.items:
            if item.get("kind") == kind:
                return item.get("summary", "")
        return ""

    def metric(self, needle: str) -> dict[str, Any]:
        for kind in ("metric_shift", "metric_elevated", "metric_stable"):
            for detail in self.all(kind):
                if needle in str(detail.get("metric", "")):
                    return detail
        return {}


# --- signal evaluators -------------------------------------------------------


def deploy_precedes_errors(pool: EvidencePool) -> tuple[bool, str]:
    causality = pool.first("causality")
    gap = causality.get("deploy_to_first_error_seconds")
    if causality.get("deploy_precedes_errors") and gap is not None:
        return True, f"deployment shipped {gap / 60:.1f} min before the first error"
    return False, "no deployment precedes the first error in the window"


def deploy_touched_service(pool: EvidencePool) -> tuple[bool, str]:
    deployment = pool.first("deployment")
    service = pool.incident.get("service")
    if deployment and deployment.get("service") == service:
        return True, f"deployment {deployment.get('deployment_id')} targeted {service}"
    return False, "no deployment to the affected service found"


def changed_file_in_stack_trace(pool: EvidencePool) -> tuple[bool, str]:
    match = pool.first("code_change_match")
    files = match.get("matched_files") or []
    if files:
        return True, f"PR #{match.get('pr_number')} changed {', '.join(files)}"
    return False, "no recent PR touched a file named in the stack trace"


def signature_new_since_deploy(pool: EvidencePool) -> tuple[bool, str]:
    causality = pool.first("causality")
    if causality.get("signature_new_since_deploy"):
        error = pool.first("error_pattern")
        return True, (
            f"{error.get('error_type', 'the error')} is absent from logs before the "
            "deployment despite being searched for"
        )
    return False, "error signature was already present before the deployment"


def metric_degradation_after_deploy(pool: EvidencePool) -> tuple[bool, str]:
    causality = pool.first("causality")
    deployed_at = _parse_ts(causality.get("deployed_at"))
    if not deployed_at:
        return False, "no deployment timestamp to compare metrics against"
    after = [
        detail["metric"]
        for detail in pool.all("metric_shift")
        if (shifted := _parse_ts(detail.get("shifted_at"))) and shifted >= deployed_at
    ]
    if after:
        return True, f"{', '.join(after)} degraded after the deployment"
    return False, "no metric stepped after the deployment"


def no_competing_infra_signal(pool: EvidencePool) -> tuple[bool, str]:
    observed = (
        pool.all("metric_shift") + pool.all("metric_elevated") + pool.all("metric_stable")
    )
    if not observed:
        # Absence of data is not evidence of absence — we never looked.
        return False, "no metrics were collected, so nothing can be ruled out"
    elevated = [detail.get("metric") for detail in pool.all("metric_elevated")]
    db_errors = pool.first("database_errors").get("count", 0)
    if not elevated and not db_errors:
        return True, "no competing infrastructure anomaly in the window"
    parts = []
    if elevated:
        parts.append(f"elevated {', '.join(str(m) for m in elevated)}")
    if db_errors:
        parts.append(f"{db_errors} database errors")
    return False, "competing signal present: " + "; ".join(parts)


def db_errors_present(pool: EvidencePool) -> tuple[bool, str]:
    count = pool.first("database_errors").get("count", 0)
    if count:
        return True, f"{count} database error lines in the window"
    return False, "no database connection or query errors in logs"


def db_latency_elevated(pool: EvidencePool) -> tuple[bool, str]:
    detail = pool.metric("db")
    if detail and detail.get("significant"):
        return True, (
            f"{detail.get('metric')} is {detail.get('change_pct', 0):+.1f}% vs baseline"
        )
    return False, "database latency within normal range"


def db_shift_coincides(pool: EvidencePool) -> tuple[bool, str]:
    detail = pool.metric("db")
    if detail and detail.get("shifted_in_window"):
        return True, "database latency stepped inside the incident window"
    return False, (
        "database latency was already elevated before the window — it did not change "
        "when the errors began"
    )


def request_volume_spike(pool: EvidencePool) -> tuple[bool, str]:
    detail = pool.metric("requests")
    change = float(detail.get("change_pct") or 0.0)
    if change >= 50.0:
        return True, f"request rate {change:+.1f}% vs baseline"
    return False, f"request rate only {change:+.1f}% vs baseline"


def errors_scale_with_volume(pool: EvidencePool) -> tuple[bool, str]:
    detail = pool.metric("requests")
    if detail.get("significant") and detail.get("shifted_in_window"):
        return True, "request volume stepped alongside the errors"
    return False, "error rate rose without a matching change in request volume"


def volume_above_baseline(pool: EvidencePool) -> tuple[bool, str]:
    detail = pool.metric("requests")
    change = float(detail.get("change_pct") or 0.0)
    if change > 0:
        return True, f"request rate marginally above baseline ({change:+.1f}%)"
    return False, "request rate at or below baseline"


HYPOTHESES: list[dict[str, Any]] = [
    {
        "name": "Bad deployment",
        "description": "A recent deployment introduced the failing code path",
        "action": "rollback",
        "signals": [
            ("deploy_precedes_errors", "Deployment shipped before the first error", 0.25, deploy_precedes_errors),
            ("deploy_touched_affected_service", "Deployment targeted the affected service", 0.15, deploy_touched_service),
            ("changed_file_in_stack_trace", "A changed file appears in the failing stack trace", 0.25, changed_file_in_stack_trace),
            ("error_signature_new_since_deploy", "Error signature is new since the deployment", 0.18, signature_new_since_deploy),
            ("metric_degradation_after_deploy", "Service metrics degraded after the deployment", 0.08, metric_degradation_after_deploy),
            ("no_competing_infra_signal", "No competing infrastructure anomaly", 0.09, no_competing_infra_signal),
        ],
    },
    {
        "name": "Database failure",
        "description": "A datastore fault is causing the service errors",
        "action": "investigate",
        "signals": [
            ("db_errors_present", "Database errors appear in logs", 0.45, db_errors_present),
            ("db_latency_elevated", "Database latency above baseline", 0.23, db_latency_elevated),
            ("db_degradation_coincides_with_errors", "Database latency stepped when errors began", 0.32, db_shift_coincides),
        ],
    },
    {
        "name": "Traffic spike",
        "description": "Load exceeded capacity and the service shed requests",
        "action": "investigate",
        "signals": [
            ("request_volume_spike", "Request volume spiked well above baseline", 0.50, request_volume_spike),
            ("errors_scale_with_volume", "Errors track request volume", 0.33, errors_scale_with_volume),
            ("volume_above_baseline", "Request volume above baseline at all", 0.17, volume_above_baseline),
        ],
    },
]


class CorrelationAgent(Agent):
    name = "Correlation Agent"
    codename = "correlation"
    source = "all agents"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        pool = EvidencePool(state.get("evidence") or [], state.get("incident") or {})

        scored: list[Hypothesis] = []
        for spec in HYPOTHESES:
            signals: list[Signal] = []
            support: list[str] = []
            for key, description, weight, evaluator in spec["signals"]:
                matched, rationale = evaluator(pool)
                signals.append(
                    Signal(
                        name=key,
                        description=description,
                        weight=weight,
                        matched=matched,
                        rationale=rationale,
                    )
                )
                if matched:
                    support.append(rationale)
            hypothesis = Hypothesis(
                name=spec["name"],
                description=spec["description"],
                score=round(sum(s.contribution for s in signals), 4),
                signals=signals,
                supporting_evidence=support,
            )
            scored.append(hypothesis)

        scored.sort(key=lambda h: h.score, reverse=True)
        top = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        margin = top.score - runner_up.score if runner_up else top.score

        return {
            "hypotheses": [h.model_dump(mode="json") for h in scored],
            "evidence": [
                self.evidence(
                    "hypothesis_ranking",
                    f"Top hypothesis '{top.name}' at {top.score:.0%} "
                    f"(margin {margin:.0%} over '{runner_up.name if runner_up else 'n/a'}')",
                    ranking=[{"name": h.name, "score": h.score} for h in scored],
                    margin=round(margin, 4),
                ).model_dump(mode="json")
            ],
            "modes": {self.codename: "deterministic"},
            "_detail": f"{top.name} {top.score:.0%}",
        }
