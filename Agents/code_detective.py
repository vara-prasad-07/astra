"""Code Detective — Code Archaeologist.

Runs after the Log Detective because the dependency is real: you cannot match
changed files against a stack trace you do not have yet. It stays concurrent
with the Metrics agent, which needs nothing from either.

Scoped to recent merged PRs on the affected service, never a repository scan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from integrations import deploy, github
from orchestrator.state import SwarmState

from .base import Agent


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _stack_refs(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in evidence:
        if item.get("kind") == "error_pattern":
            return (item.get("detail") or {}).get("code_refs") or []
    return []


class CodeDetective(Agent):
    name = "Code Archaeologist"
    codename = "code_detective"
    source = "github"

    async def investigate(self, state: SwarmState) -> dict[str, Any]:
        incident = state.get("incident") or {}
        service = incident.get("service", "unknown-service")
        trigger = _parse_ts(state.get("trigger_time")) or datetime.now(timezone.utc)
        lookback = trigger - timedelta(hours=24)

        pulls, mode = await github.recent_pull_requests(service, lookback)

        active, deploy_mode = await deploy.active_deployment(service)
        if active is None:
            deploys, deploy_mode = await github.deployments(service, trigger)
            active = next((d for d in deploys if d.get("status") == "active"), None)

        refs = _stack_refs(state.get("evidence") or [])
        failing_files = {PurePosixPath(ref["file"]).name for ref in refs}

        scored: list[dict[str, Any]] = []
        for pull in pulls:
            changed = [f.get("filename", "") for f in pull.get("files", [])]
            matched = [
                path for path in changed if PurePosixPath(path).name in failing_files
            ]
            scored.append(
                {
                    "number": pull.get("number"),
                    "title": pull.get("title", ""),
                    "author": (pull.get("user") or {}).get("login", "unknown"),
                    "merged_at": pull.get("merged_at"),
                    "url": pull.get("html_url", ""),
                    "sha": pull.get("merge_commit_sha", ""),
                    "touches_service": pull.get("service") == service,
                    "changed_files": changed,
                    "matched_files": matched,
                }
            )

        scored.sort(
            key=lambda item: (
                len(item["matched_files"]),
                item["touches_service"],
                item["merged_at"] or "",
            ),
            reverse=True,
        )

        evidence: list[dict[str, Any]] = []
        culprit = scored[0] if scored and scored[0]["matched_files"] else None

        if culprit:
            merged = _parse_ts(culprit["merged_at"])
            evidence.append(
                self.evidence(
                    "code_change_match",
                    f"PR #{culprit['number']} \"{culprit['title']}\" ({culprit['author']}) "
                    f"changed {', '.join(culprit['matched_files'])}",
                    pr_number=culprit["number"],
                    title=culprit["title"],
                    author=culprit["author"],
                    merged_at=culprit["merged_at"],
                    url=culprit["url"],
                    sha=culprit["sha"],
                    matched_files=culprit["matched_files"],
                    touches_service=culprit["touches_service"],
                    minutes_before_incident=(
                        round((trigger - merged).total_seconds() / 60, 1) if merged else None
                    ),
                ).model_dump(mode="json")
            )
        else:
            evidence.append(
                self.evidence(
                    "code_change_match",
                    "No recent PR touched a file named in the stack trace",
                    pr_number=None,
                    matched_files=[],
                ).model_dump(mode="json")
            )

        evidence.append(
            self.evidence(
                "code_change_candidates",
                f"Considered {len(scored)} merged PRs in the last 24h; "
                f"{sum(1 for s in scored if s['matched_files'])} touched the failing path",
                candidates=[
                    {
                        "number": item["number"],
                        "title": item["title"],
                        "matched": bool(item["matched_files"]),
                        "touches_service": item["touches_service"],
                    }
                    for item in scored
                ],
            ).model_dump(mode="json")
        )

        if active:
            evidence.append(
                self.evidence(
                    "deployment",
                    f"Deployment {active['id']} ({active['version']}) shipped at "
                    f"{active['deployed_at']}",
                    deployment_id=active["id"],
                    version=active["version"],
                    previous_version=active.get("previous_version", ""),
                    deployed_at=active["deployed_at"],
                    pr_number=active.get("pr_number"),
                    sha=active.get("sha", ""),
                    service=service,
                ).model_dump(mode="json")
            )

        return {
            "evidence": evidence,
            "modes": {self.codename: mode, "deployments": deploy_mode},
            "_detail": f"PR #{culprit['number']}" if culprit else "no code match",
        }
