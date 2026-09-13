"""GitHub: recent merged PRs and their changed files.

Scoped deliberately to recent pull requests on the affected service rather than
scanning the repository — the design record calls this out as the difference
between an investigation and a brute-force search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import settings
from demo import fixtures

from ._http import request


def live() -> bool:
    return settings.is_live(settings.github)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github.required['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _infer_service(files: list[dict[str, Any]], service: str) -> str:
    """A PR belongs to the affected service if it touched a path naming it."""
    token = service.replace("-service", "").replace("_", "-").lower()
    for item in files:
        path = item.get("filename", "").lower()
        if service.lower() in path or (token and token in path):
            return service
    return "unknown"


async def recent_pull_requests(
    service: str, since: datetime, limit: int = 15
) -> tuple[list[dict[str, Any]], str]:
    if not live():
        return fixtures.github_pull_requests(since, service), "demo"

    repo = settings.github_repo
    pulls = await request(
        "GET",
        f"https://api.github.com/repos/{repo}/pulls",
        headers=_headers(),
        params={
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": limit,
        },
    )
    results: list[dict[str, Any]] = []
    for pull in pulls if isinstance(pulls, list) else []:
        merged_at = pull.get("merged_at")
        if not merged_at:
            continue
        merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        if merged_dt < since.astimezone(timezone.utc):
            continue
        try:
            files = await request(
                "GET",
                f"https://api.github.com/repos/{repo}/pulls/{pull['number']}/files",
                headers=_headers(),
                params={"per_page": 100},
            )
        except Exception:
            files = []
        files = files if isinstance(files, list) else []
        results.append(
            {
                "number": pull["number"],
                "title": pull.get("title", ""),
                "user": {"login": (pull.get("user") or {}).get("login", "unknown")},
                "merged_at": merged_at,
                "html_url": pull.get("html_url", ""),
                "merge_commit_sha": pull.get("merge_commit_sha", ""),
                "service": _infer_service(files, service),
                "files": [
                    {
                        "filename": f.get("filename", ""),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                    }
                    for f in files
                ],
            }
        )
    return results, "live"


async def deployments(service: str, trigger: datetime) -> tuple[list[dict[str, Any]], str]:
    """Deployment history. GitHub Deployments API when live, fixtures otherwise."""
    if not live():
        return fixtures.deployments(trigger, service), "demo"

    repo = settings.github_repo
    try:
        raw = await request(
            "GET",
            f"https://api.github.com/repos/{repo}/deployments",
            headers=_headers(),
            params={"environment": "production", "per_page": 10},
        )
    except Exception:
        return [], "live"
    items: list[dict[str, Any]] = []
    for index, dep in enumerate(raw if isinstance(raw, list) else []):
        items.append(
            {
                "id": str(dep.get("id", "")),
                "service": service,
                "version": dep.get("ref", ""),
                "previous_version": "",
                "deployed_at": dep.get("created_at", ""),
                "pr_number": None,
                "sha": dep.get("sha", ""),
                "status": "active" if index == 0 else "superseded",
            }
        )
    for i in range(1, len(items)):
        items[i - 1]["previous_version"] = items[i]["version"]
    return items, "live"
