"""Domain model for the incident lifecycle.

Mirrors the relational schema in the design record: an incident, the evidence
rows each agent contributes, the scored hypotheses, and the action finally taken.
Every recommendation stays traceable back to the evidence that produced it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Incident(BaseModel):
    id: str
    service: str
    severity: str = "P1"
    status: Literal["triggered", "investigating", "awaiting_approval", "remediating", "resolved", "rejected"] = "triggered"
    title: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    error_signature: str = ""
    suspected_deployment: str | None = None
    window_minutes: int = 30
    pagerduty_id: str | None = None
    source: str = "pagerduty"


class Evidence(BaseModel):
    """One auditable finding from exactly one agent against one source system."""

    agent: str
    codename: str
    source: str
    kind: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    collected_at: datetime = Field(default_factory=utcnow)


class Signal(BaseModel):
    """A single named check inside hypothesis scoring — the inspectable unit."""

    name: str
    description: str
    weight: float
    matched: bool
    rationale: str = ""

    @property
    def contribution(self) -> float:
        return self.weight if self.matched else 0.0


class Hypothesis(BaseModel):
    name: str
    description: str
    score: float = 0.0
    signals: list[Signal] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)

    @property
    def matched_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.matched]


class TimelineEvent(BaseModel):
    at: datetime
    kind: str
    source: str
    description: str


class Recommendation(BaseModel):
    hypothesis: str
    root_cause: str
    confidence: float
    action_type: Literal["rollback", "investigate", "no_action"]
    action_target: str | None = None
    risk: Literal["low", "medium", "high"] = "medium"
    risk_notes: str = ""
    evidence_bullets: list[str] = Field(default_factory=list)
    requires_approval: bool = True


class Approval(BaseModel):
    incident_id: str
    decision: Literal["approve", "reject", "investigate"]
    approver: str
    at: datetime = Field(default_factory=utcnow)
    source: str = "slack"


class VerificationStep(BaseModel):
    step: str
    passed: bool
    detail: str = ""


class ActionResult(BaseModel):
    incident_id: str
    action_type: str
    target: str | None = None
    executed: bool = False
    approver: str = ""
    checks: list[VerificationStep] = Field(default_factory=list)
    health_restored: bool = False
    error_rate_after: float | None = None
    mttr_seconds: float | None = None
    notes: str = ""
    executed_at: datetime | None = None


class AgentTrace(BaseModel):
    """Emitted for the live dashboard so parallel agent work is visible."""

    agent: str
    codename: str
    status: Literal["started", "finished", "failed", "skipped"]
    at: datetime = Field(default_factory=utcnow)
    mode: str = "demo"
    detail: str = ""
    duration_ms: float | None = None
