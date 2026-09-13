"""Tests for the properties that make NIGHTWATCH defensible.

The happy path is covered by validate.py. These focus on the claims that are
easy to assert and easy to get wrong: the scorer genuinely discriminates, the
confidence floor holds, and the Operator refuses to act when a precondition fails.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from Agents.correlation import HYPOTHESES  # noqa: E402
from config import settings  # noqa: E402
from demo import fixtures  # noqa: E402
from integrations import deploy, pagerduty, slack  # noqa: E402
from orchestrator.runner import decide, investigate  # noqa: E402
from storage import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    # Tests assert against the fixture world (PR #1842, deployment 8421, ...), so
    # they must run on demo fixtures regardless of whatever real credentials
    # happen to be sitting in a developer's .env — otherwise a configured
    # PagerDuty/Datadog/GitHub account makes these tests query real, unrelated
    # data and fail non-deterministically.
    original_force_demo = settings.force_demo
    settings.force_demo = True
    db.init()
    db.reset()
    deploy.reset()
    slack.demo_outbox.clear()
    yield
    db.reset()
    settings.force_demo = original_force_demo


def detail(state, kind):
    for item in state.get("evidence") or []:
        if item.get("kind") == kind:
            return item.get("detail") or {}
    return {}


# --- scoring -----------------------------------------------------------------


def test_hypothesis_weights_sum_to_one():
    for spec in HYPOTHESES:
        total = sum(weight for _, _, weight, _ in spec["signals"])
        assert total == pytest.approx(1.0), f"{spec['name']} weights sum to {total}"


async def test_scorer_ranks_bad_deployment_first():
    state = await investigate(fixtures.pagerduty_webhook())
    names = [h["name"] for h in state["hypotheses"]]
    assert names[0] == "Bad deployment"
    assert state["hypotheses"][0]["score"] > 0.80
    # The competing hypotheses must be scored, not ignored.
    assert all(h["score"] > 0 for h in state["hypotheses"][1:])


async def test_every_signal_carries_a_rationale():
    state = await investigate(fixtures.pagerduty_webhook())
    for hypothesis in state["hypotheses"]:
        for signal in hypothesis["signals"]:
            assert signal["rationale"], f"{hypothesis['name']}/{signal['name']} has no rationale"


async def test_database_hypothesis_rejected_for_the_right_reason():
    """Elevated DB latency alone must not implicate the database."""
    state = await investigate(fixtures.pagerduty_webhook())
    db_hypothesis = next(h for h in state["hypotheses"] if h["name"] == "Database failure")
    signals = {s["name"]: s for s in db_hypothesis["signals"]}
    assert signals["db_latency_elevated"]["matched"] is True
    assert signals["db_errors_present"]["matched"] is False
    assert signals["db_degradation_coincides_with_errors"]["matched"] is False


# --- investigation quality ---------------------------------------------------


async def test_code_detective_rejects_decoy_prs():
    state = await investigate(fixtures.pagerduty_webhook())
    match = detail(state, "code_change_match")
    assert match["pr_number"] == fixtures.PR_NUMBER
    candidates = detail(state, "code_change_candidates")["candidates"]
    assert len(candidates) > 1, "decoy PRs should still be considered"
    assert sum(1 for c in candidates if c["matched"]) == 1


async def test_log_hunter_picks_the_dominant_pattern_not_the_loudest_noise():
    state = await investigate(fixtures.pagerduty_webhook())
    pattern = detail(state, "error_pattern")
    assert pattern["error_type"] == fixtures.ERROR_TYPE
    assert pattern["count"] == fixtures.ERROR_COUNT
    assert "PaymentValidator.java" in pattern["stack_trace"]


async def test_timeline_establishes_deploy_before_errors():
    state = await investigate(fixtures.pagerduty_webhook())
    causality = detail(state, "causality")
    assert causality["deploy_precedes_errors"] is True
    assert causality["signature_new_since_deploy"] is True
    assert 0 < causality["deploy_to_first_error_seconds"] < 900


# --- the human gate ----------------------------------------------------------


async def test_no_action_is_executed_before_approval():
    state = await investigate(fixtures.pagerduty_webhook())
    assert state["status"] == "awaiting_approval"
    assert state.get("action_result") is None
    assert state["recommendation"]["requires_approval"] is True


async def test_rejection_executes_nothing():
    state = await investigate(fixtures.pagerduty_webhook())
    rejected = await decide(state["incident_id"], "reject", "vara")
    assert rejected["status"] == "rejected"
    assert rejected.get("action_result") is None


async def test_double_remediation_is_refused():
    state = await investigate(fixtures.pagerduty_webhook())
    await decide(state["incident_id"], "approve", "vara")
    with pytest.raises(ValueError):
        await decide(state["incident_id"], "approve", "vara")


async def test_unauthorised_approver_aborts_before_execution(monkeypatch):
    monkeypatch.setattr(settings, "approvers", ["alice", "bob"])
    state = await investigate(fixtures.pagerduty_webhook())
    result = await decide(state["incident_id"], "approve", "mallory")
    action = result["action_result"]
    assert action["executed"] is False
    assert action["checks"][0]["passed"] is False
    assert "Aborted" in action["notes"]


async def test_deployment_id_mismatch_aborts_before_execution():
    state = await investigate(fixtures.pagerduty_webhook())
    tampered = copy.deepcopy(state)
    tampered["recommendation"]["action_target"] = "9999"
    db.save_state(tampered)

    result = await decide(state["incident_id"], "approve", "vara")
    action = result["action_result"]
    assert action["executed"] is False
    assert any(
        c["step"].startswith("Deployment ID matches") and not c["passed"]
        for c in action["checks"]
    )


async def test_approved_rollback_runs_the_full_verification_sequence():
    state = await investigate(fixtures.pagerduty_webhook())
    result = await decide(state["incident_id"], "approve", "vara")
    action = result["action_result"]
    assert action["executed"] is True
    assert action["health_restored"] is True
    steps = [c["step"] for c in action["checks"]]
    for expected in (
        "Approval came from an authorised source",
        "Incident is still active",
        "Deployment ID matches the analysed deployment",
        "Rollback executed",
        "Service health restored",
        "Slack thread updated with the outcome",
        "PagerDuty incident resolved",
    ):
        assert expected in steps, f"missing verification step: {expected}"
    assert all(c["passed"] for c in action["checks"])


# --- degradation -------------------------------------------------------------


async def test_weak_evidence_recommends_investigation_not_a_rollback():
    """No deployment and no code match must fall below the action floor."""
    alert = fixtures.pagerduty_webhook()
    alert["event"]["data"]["service"]["summary"] = "search-service"
    alert["event"]["data"]["title"] = "Elevated latency on search-service"
    alert["event"]["data"]["body"]["details"] = "Latency above threshold."

    state = await investigate(alert)
    recommendation = state["recommendation"]
    assert recommendation["action_type"] == "investigate"
    assert recommendation["action_target"] is None
    assert recommendation["risk"] == "high"
    assert recommendation["requires_approval"] is True


async def test_webhook_parsing_extracts_the_core_fields():
    parsed = pagerduty.parse_webhook(fixtures.pagerduty_webhook())
    assert parsed["service"] == fixtures.SERVICE
    assert parsed["severity"] == "P1"
    assert parsed["pagerduty_id"] == "PNW8421"
    assert isinstance(parsed["started_at"], datetime)


async def test_slack_report_carries_the_three_decision_buttons():
    state = await investigate(fixtures.pagerduty_webhook())
    posted = slack.demo_outbox[0]
    actions = [b for b in posted["blocks"] if b["type"] == "actions"][0]
    values = {e["value"].split("::")[0] for e in actions["elements"]}
    assert values == {"approve", "reject", "investigate"}
    assert all(state["incident_id"] in e["value"] for e in actions["elements"])


async def test_evidence_is_persisted_and_reloadable():
    state = await investigate(fixtures.pagerduty_webhook())
    reloaded = db.load_state(state["incident_id"])
    assert reloaded is not None
    assert len(reloaded["evidence"]) == len(state["evidence"])
    assert reloaded["recommendation"]["confidence"] == state["recommendation"]["confidence"]


def test_fixture_timeline_is_internally_consistent():
    trigger = datetime.now(timezone.utc).replace(microsecond=0)
    deploy_at = fixtures.at(trigger, "deploy")
    first_error = fixtures.at(trigger, "first_error")
    assert deploy_at < first_error < trigger
    logs = fixtures.datadog_logs(trigger)
    npes = [
        row
        for row in logs
        if fixtures.ERROR_TYPE in str((row.get("attributes") or {}).get("error.kind", ""))
    ]
    assert len(npes) == fixtures.ERROR_COUNT
    earliest = min(
        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) for row in npes
    )
    assert earliest >= deploy_at, "the regression must not predate its own deployment"
    assert earliest <= first_error + timedelta(seconds=1)
