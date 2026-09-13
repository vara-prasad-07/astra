"""Deterministic validation of the known incident-response workflow.

Implements section 10 of the design record. The point is to replace "our AI is
smart" with a specific, checkable claim: given a known incident, the swarm
identifies the right service, the right error, the right PR, recommends the
right action, refuses to act without a human, and restores health once approved.

Run:  python validate.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable

from Agents.operator import format_duration
from config import settings
from demo import fixtures
from integrations import deploy, slack
from orchestrator.runner import decide, investigate
from storage import db

# This scenario is built entirely around fixture data (PR #1842, deployment
# 8421, payment-service). It must run on demo fixtures even when a developer's
# .env has real, unrelated credentials configured — otherwise this validates
# nothing.
settings.force_demo = True

PASS = "[PASS]"
FAIL = "[FAIL]"


class Check:
    def __init__(self, label: str, fn: Callable[[], tuple[bool, str]]) -> None:
        self.label = label
        self.fn = fn

    def run(self) -> tuple[bool, str]:
        try:
            return self.fn()
        except Exception as exc:
            return False, f"raised {type(exc).__name__}: {exc}"


def _find(evidence: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    for item in evidence:
        if item.get("kind") == kind:
            return item.get("detail") or {}
    return {}


async def run_scenario() -> tuple[list[Check], dict[str, Any]]:
    db.init()
    db.reset()
    deploy.reset()
    slack.demo_outbox.clear()

    investigated = await investigate(fixtures.pagerduty_webhook())
    incident_id = investigated["incident_id"]

    # The gate must hold: nothing may be executed before a human decides.
    pre_approval_action = investigated.get("action_result")

    remediated = await decide(incident_id, "approve", "vara", source="validation")
    # The graph only returns declared state keys, so timings are carried across.
    remediated["elapsed_seconds"] = investigated.get("elapsed_seconds")

    incident = remediated.get("incident") or {}
    evidence = remediated.get("evidence") or []
    hypotheses = remediated.get("hypotheses") or []
    recommendation = remediated.get("recommendation") or {}
    action = remediated.get("action_result") or {}
    error_pattern = _find(evidence, "error_pattern")
    code_match = _find(evidence, "code_change_match")

    checks = [
        Check(
            "Incident detected",
            lambda: (
                bool(incident.get("id")),
                f"incident {incident.get('id')} created from the PagerDuty webhook",
            ),
        ),
        Check(
            "Correct service identified",
            lambda: (
                incident.get("service") == fixtures.SERVICE,
                f"identified {incident.get('service')!r}",
            ),
        ),
        Check(
            "Correct error identified",
            lambda: (
                error_pattern.get("error_type") == fixtures.ERROR_TYPE,
                f"{error_pattern.get('error_type')} x{error_pattern.get('count')}",
            ),
        ),
        Check(
            f"PR #{fixtures.PR_NUMBER} identified",
            lambda: (
                code_match.get("pr_number") == fixtures.PR_NUMBER,
                f"matched PR #{code_match.get('pr_number')} on "
                f"{', '.join(code_match.get('matched_files') or []) or 'nothing'}",
            ),
        ),
        Check(
            "Root cause = deployment",
            lambda: (
                bool(hypotheses) and hypotheses[0].get("name") == "Bad deployment",
                f"top hypothesis {hypotheses[0].get('name')!r}" if hypotheses else "none",
            ),
        ),
        Check(
            "Confidence > 80%",
            lambda: (
                float(recommendation.get("confidence", 0)) > 0.80,
                f"{float(recommendation.get('confidence', 0)):.0%}",
            ),
        ),
        Check(
            "Rollback recommended",
            lambda: (
                recommendation.get("action_type") == "rollback"
                and recommendation.get("action_target") == fixtures.DEPLOYMENT_ID,
                f"{recommendation.get('action_type')} of deployment "
                f"{recommendation.get('action_target')}",
            ),
        ),
        Check(
            "Human approval required",
            lambda: (
                bool(recommendation.get("requires_approval")) and not pre_approval_action,
                "no action existed until a human decided"
                if not pre_approval_action
                else "an action was executed before approval",
            ),
        ),
        Check(
            "Rollback executed",
            lambda: (
                bool(action.get("executed")) and action.get("target") == fixtures.DEPLOYMENT_ID,
                f"deployment {action.get('target')} rolled back, approved by "
                f"{action.get('approver')}",
            ),
        ),
        Check(
            "Health restored",
            lambda: (
                bool(action.get("health_restored")),
                f"error rate {action.get('error_rate_after')}% after rollback, "
                f"MTTR {format_duration(action.get('mttr_seconds'))}",
            ),
        ),
    ]
    return checks, remediated


async def verify_rejection_path() -> tuple[bool, str]:
    """A rejected recommendation must leave production untouched."""
    db.reset()
    deploy.reset()
    investigated = await investigate(fixtures.pagerduty_webhook())
    rejected = await decide(investigated["incident_id"], "reject", "vara", source="validation")
    action = rejected.get("action_result")
    return (
        not action and rejected.get("status") == "rejected",
        f"status {rejected.get('status')!r}, no action executed",
    )


async def main() -> int:
    checks, result = await run_scenario()

    print()
    print("Scenario: Bad payment deployment")
    print("=" * 62)
    failures = 0
    for index, check in enumerate(checks, start=1):
        ok, detail = check.run()
        if not ok:
            failures += 1
        print(f"{index:>3}. {check.label:<34} {PASS if ok else FAIL}  {detail}")

    ok, detail = await verify_rejection_path()
    if not ok:
        failures += 1
    print(f"{len(checks) + 1:>3}. {'Rejection leaves prod untouched':<34} "
          f"{PASS if ok else FAIL}  {detail}")

    print("=" * 62)
    total = len(checks) + 1
    print(f"{total - failures}/{total} checks passed")
    print(f"investigation took {result.get('elapsed_seconds')}s, "
          f"remediation {result.get('remediation_seconds')}s")

    modes = result.get("modes") or {}
    demo_sources = sorted(k for k, v in modes.items() if v == "demo")
    if demo_sources:
        print(f"note: running on demo fixtures for {', '.join(demo_sources)} "
              f"(run `python setup.py` to add real API keys)")

    if failures:
        print("\nRESULT: FAILED")
        return 1
    print("\nRESULT: the swarm completed a known incident-response workflow end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
