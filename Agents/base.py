"""Agent base class.

Every agent is a LangGraph node: it takes shared state, does its own work against
its own source system, and returns a partial state update. Timing, tracing and
failure isolation live here so one dead integration cannot take down the swarm.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from LLM_Providers import get_provider
from models import Evidence
from orchestrator.events import bus
from orchestrator.state import SwarmState


def describe_change(detail: dict[str, Any]) -> str:
    """Percentages stop being readable once a near-zero baseline explodes them."""
    change = float(detail.get("change_pct") or 0.0)
    if abs(change) >= 1000:
        return f"went from {detail.get('baseline')} to {detail.get('current')}"
    return f"moved {change:+.1f}%"


class Agent(ABC):
    name: str = "Agent"
    codename: str = "agent"
    source: str = "internal"

    def __init__(self) -> None:
        self.llm = get_provider()

    @abstractmethod
    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        """Return a partial state update. Raise to signal failure."""

    def evidence(self, kind: str, summary: str, **detail: Any) -> Evidence:
        return Evidence(
            agent=self.name,
            codename=self.codename,
            source=self.source,
            kind=kind,
            summary=summary,
            detail=detail,
            confidence=float(detail.pop("_confidence", 1.0)),
        )

    async def __call__(self, state: SwarmState) -> dict[str, Any]:
        incident_id = state.get("incident_id", "")
        bus.publish(
            "agent",
            {
                "incident_id": incident_id,
                "agent": self.name,
                "codename": self.codename,
                "source": self.source,
                "status": "started",
            },
        )
        started = time.perf_counter()
        try:
            update = await self.investigate(state)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            bus.publish(
                "agent",
                {
                    "incident_id": incident_id,
                    "agent": self.name,
                    "codename": self.codename,
                    "source": self.source,
                    "status": "failed",
                    "detail": str(exc)[:300],
                    "duration_ms": round(duration, 1),
                },
            )
            # A source system being down degrades the evidence pool; it does not
            # abort the investigation.
            return {
                "traces": [
                    {
                        "agent": self.name,
                        "codename": self.codename,
                        "status": "failed",
                        "detail": str(exc)[:300],
                        "duration_ms": round(duration, 1),
                    }
                ],
                "evidence": [],
            }

        duration = (time.perf_counter() - started) * 1000
        mode = (update.get("modes") or {}).get(self.codename, "demo")
        found = len(update.get("evidence", []) or [])
        bus.publish(
            "agent",
            {
                "incident_id": incident_id,
                "agent": self.name,
                "codename": self.codename,
                "source": self.source,
                "status": "finished",
                "mode": mode,
                "evidence_count": found,
                "detail": update.pop("_detail", ""),
                "duration_ms": round(duration, 1),
            },
        )
        update.setdefault("traces", []).append(
            {
                "agent": self.name,
                "codename": self.codename,
                "status": "finished",
                "mode": mode,
                "duration_ms": round(duration, 1),
            }
        )
        return update
