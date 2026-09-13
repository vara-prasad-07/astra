"""Metrics Agent — the quantitative half of the Datadog investigation.

Distinguishes a metric that *stepped* inside the incident window from one that is
merely elevated. That difference is what stops "the database looked slow" from
being mistaken for a cause.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from integrations import datadog
from orchestrator.state import SwarmState

from .base import Agent

SIGNIFICANT_CHANGE_PCT = 10.0


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


class MetricsAgent(Agent):
    name = "Metrics Analyst"
    codename = "metrics"
    source = "datadog"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        incident = state.get("incident") or {}
        service = incident.get("service", "unknown-service")
        trigger = _parse_ts(state.get("trigger_time")) or datetime.now(timezone.utc)
        window = int(incident.get("window_minutes", 30))
        start = trigger - timedelta(minutes=window)

        metrics, mode = await datadog.get_metrics(service, start, trigger)
        series = metrics.get("series", {})

        evidence: list[dict[str, Any]] = []
        shifted: list[str] = []

        for name, values in series.items():
            if "error" in values:
                continue
            change = float(values.get("change_pct") or 0.0)
            shift_at = _parse_ts(values.get("shifted_at"))
            in_window = bool(shift_at and shift_at >= start)
            significant = abs(change) >= SIGNIFICANT_CHANGE_PCT

            if significant and in_window:
                shifted.append(name)
                kind, note = "metric_shift", "stepped inside the incident window"
            elif significant:
                kind, note = (
                    "metric_elevated",
                    "elevated but the step predates the incident window",
                )
            else:
                kind, note = "metric_stable", "within normal range"

            delta = f"({change:+.1f}%) " if abs(change) < 1000 else ""
            evidence.append(
                self.evidence(
                    kind,
                    f"{name}: {values.get('baseline')} -> {values.get('current')} "
                    f"{values.get('unit', '')} {delta}- {note}",
                    metric=name,
                    baseline=values.get("baseline"),
                    current=values.get("current"),
                    change_pct=change,
                    shifted_at=values.get("shifted_at"),
                    shifted_in_window=in_window,
                    significant=significant,
                ).model_dump(mode="json")
            )

        return {
            "evidence": evidence,
            "modes": {self.codename: mode},
            "_detail": f"{len(shifted)} metric(s) stepped in window",
        }
