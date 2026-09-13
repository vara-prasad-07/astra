"""Shared graph state.

The three investigation agents run concurrently and all append to `evidence`,
`traces` and `modes`, so those keys carry reducers — LangGraph merges the
parallel branches instead of having the last writer win.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_modes(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class SwarmState(TypedDict, total=False):
    incident_id: str
    raw_alert: dict[str, Any]
    incident: dict[str, Any]
    trigger_time: str
    search_start: str

    evidence: Annotated[list[dict[str, Any]], operator.add]
    traces: Annotated[list[dict[str, Any]], operator.add]
    modes: Annotated[dict[str, str], merge_modes]

    timeline: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    recommendation: dict[str, Any]
    slack_ref: dict[str, Any]
    approval: dict[str, Any]
    action_result: dict[str, Any]
    status: str


def new_state(incident_id: str, raw_alert: dict[str, Any]) -> SwarmState:
    return {
        "incident_id": incident_id,
        "raw_alert": raw_alert,
        "evidence": [],
        "traces": [],
        "modes": {},
        "timeline": [],
        "hypotheses": [],
        "status": "triggered",
    }
