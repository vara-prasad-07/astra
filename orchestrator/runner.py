"""Runs the two phases of an incident and persists the result of each."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from integrations import slack
from models import Approval
from orchestrator.events import bus
from orchestrator.graph import build_graph
from orchestrator.state import new_state
from storage import db


def new_incident_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"NW-{stamp}-{uuid.uuid4().hex[:6].upper()}"


async def investigate(alert: dict[str, Any], incident_id: str | None = None) -> dict[str, Any]:
    """Phase one: detect, investigate, correlate, recommend, ask a human."""
    incident_id = incident_id or new_incident_id()
    db.init()

    bus.publish("incident", {"incident_id": incident_id, "status": "triggered"})
    started = time.perf_counter()

    result = await build_graph().ainvoke(new_state(incident_id, alert))
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)

    db.save_state(result)
    bus.publish(
        "incident",
        {
            "incident_id": incident_id,
            "status": result.get("status", "awaiting_approval"),
            "recommendation": result.get("recommendation"),
            "hypotheses": result.get("hypotheses"),
            "elapsed_seconds": result["elapsed_seconds"],
        },
    )
    return result


async def decide(
    incident_id: str, decision: str, approver: str, source: str = "slack"
) -> dict[str, Any]:
    """Phase two: act on a human decision. Only 'approve' reaches the Operator."""
    state = db.load_state(incident_id)
    if state is None:
        raise KeyError(f"unknown incident {incident_id}")

    if state.get("action_result", {}).get("executed"):
        raise ValueError(f"incident {incident_id} has already been remediated")

    approval = Approval(
        incident_id=incident_id, decision=decision, approver=approver, source=source
    )
    db.record_approval(incident_id, decision, approver, source)
    bus.publish(
        "approval",
        {"incident_id": incident_id, "decision": decision, "approver": approver},
    )

    state["approval"] = approval.model_dump(mode="json")

    if decision != "approve":
        note = (
            f"{approver} chose to {decision}. No production change was made; "
            "NIGHTWATCH is standing down and the incident remains open."
        )
        await slack.post_thread_update(
            incident_id, (state.get("slack_ref") or {}).get("ts"), note
        )
        state["status"] = "rejected" if decision == "reject" else "investigating"
        db.save_state(state)
        bus.publish(
            "incident", {"incident_id": incident_id, "status": state["status"], "note": note}
        )
        return state

    started = time.perf_counter()
    result = await build_graph().ainvoke(state)
    result["remediation_seconds"] = round(time.perf_counter() - started, 2)

    db.save_state(result)
    bus.publish(
        "incident",
        {
            "incident_id": incident_id,
            "status": result.get("status"),
            "action_result": result.get("action_result"),
        },
    )
    return result
