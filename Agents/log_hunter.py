"""Log Hunter — Log Detective.

Queries Datadog for the affected service and window, then deduplicates into
error *patterns* before anything reaches the model. Passing thousands of raw log
lines to an LLM is the anti-pattern this design exists to avoid.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from integrations import datadog
from orchestrator.state import SwarmState

from .base import Agent

JAVA_FRAME = re.compile(r"\(([A-Za-z0-9_$]+\.java):(\d+)\)")
PY_FRAME = re.compile(r'File "([^"]+\.py)", line (\d+)')
EXCEPTION = re.compile(r"\b((?:[a-z0-9_]+\.)*[A-Z][A-Za-z0-9_]*(?:Exception|Error))\b")


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _signature(row: dict[str, Any]) -> str:
    attrs = row.get("attributes") or {}
    kind = attrs.get("error.kind")
    if kind:
        return str(kind).rsplit(".", 1)[-1]
    match = EXCEPTION.search(row.get("message", "") or "")
    return match.group(1).rsplit(".", 1)[-1] if match else "UnclassifiedError"


def _code_refs(text: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in (JAVA_FRAME, PY_FRAME):
        for filename, line in pattern.findall(text or ""):
            key = (filename, int(line))
            if key not in seen:
                seen.add(key)
                refs.append({"file": filename, "line": int(line)})
    return refs


class LogHunter(Agent):
    name = "Log Detective"
    codename = "log_hunter"
    source = "datadog"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        incident = state.get("incident") or {}
        service = incident.get("service", "unknown-service")
        trigger = _parse_ts(state.get("trigger_time")) or datetime.now(timezone.utc)
        window = int(incident.get("window_minutes", 30))
        start = trigger - timedelta(minutes=window)

        rows, mode = await datadog.search_logs(service, start, trigger)
        errors = [r for r in rows if str(r.get("status", "")).lower() in {"error", "critical"}]

        by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in errors:
            by_signature[_signature(row)].append(row)

        if not by_signature:
            return {
                "evidence": [
                    self.evidence(
                        "log_search",
                        f"No errors found for {service} in the {window}m window",
                        service=service,
                        window_minutes=window,
                        total_logs=len(rows),
                    ).model_dump(mode="json")
                ],
                "modes": {self.codename: mode},
                "_detail": "no errors found",
            }

        dominant, matching = max(by_signature.items(), key=lambda kv: len(kv[1]))
        timestamps = sorted(
            ts for ts in (_parse_ts(r.get("timestamp")) for r in matching) if ts
        )
        first_seen = timestamps[0] if timestamps else trigger
        last_seen = timestamps[-1] if timestamps else trigger

        stack_trace = ""
        for row in matching:
            candidate = (row.get("attributes") or {}).get("error.stack") or row.get("message", "")
            if len(candidate) > len(stack_trace):
                stack_trace = candidate

        endpoints = Counter(
            (r.get("attributes") or {}).get("endpoint", "unknown") for r in matching
        )
        refs = _code_refs(stack_trace)

        evidence = [
            self.evidence(
                "error_pattern",
                f"{dominant} x{len(matching)}, first seen "
                f"{first_seen.strftime('%H:%M:%S')} UTC",
                error_type=dominant,
                count=len(matching),
                first_seen=first_seen.isoformat(),
                last_seen=last_seen.isoformat(),
                window_start=start.isoformat(),
                stack_trace=stack_trace.strip(),
                code_refs=refs,
                endpoints=dict(endpoints),
                service=service,
            ).model_dump(mode="json")
        ]

        other = {k: len(v) for k, v in by_signature.items() if k != dominant}
        if other:
            evidence.append(
                self.evidence(
                    "secondary_errors",
                    "Other error signatures present but not dominant: "
                    + ", ".join(f"{k} x{v}" for k, v in sorted(other.items())),
                    signatures=other,
                ).model_dump(mode="json")
            )

        db_errors = [
            r
            for r in errors
            if re.search(
                r"(connection refused|deadlock|timeout expired|too many connections|"
                r"SQLException|OperationalError)",
                r.get("message", "") or "",
                re.IGNORECASE,
            )
        ]
        evidence.append(
            self.evidence(
                "database_errors",
                f"{len(db_errors)} database-related error lines in window",
                count=len(db_errors),
            ).model_dump(mode="json")
        )

        return {
            "evidence": evidence,
            "modes": {self.codename: mode},
            "_detail": f"{dominant} x{len(matching)}",
        }
