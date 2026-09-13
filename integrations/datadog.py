"""Datadog: log search and metric series for the affected service/window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings
from demo import fixtures

from ._http import request


def _headers() -> dict[str, str]:
    return {
        "DD-API-KEY": settings.datadog.required["DD_API_KEY"],
        "DD-APPLICATION-KEY": settings.datadog.required["DD_APP_KEY"],
        "Content-Type": "application/json",
    }


def live() -> bool:
    return settings.is_live(settings.datadog)


async def search_logs(
    service: str, start: datetime, end: datetime, limit: int = 500
) -> tuple[list[dict[str, Any]], str]:
    """Return (log rows, mode). Rows are normalised to a flat dict shape."""
    if not live():
        return fixtures.datadog_logs(end, service), "demo"

    payload = {
        "filter": {
            "query": f"service:{service}",
            "from": start.astimezone(timezone.utc).isoformat(),
            "to": end.astimezone(timezone.utc).isoformat(),
        },
        "sort": "timestamp",
        "page": {"limit": min(limit, 1000)},
    }
    data = await request(
        "POST",
        f"https://api.{settings.dd_site}/api/v2/logs/events/search",
        headers=_headers(),
        json=payload,
    )
    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}) or {}
        inner = attrs.get("attributes", {}) or {}
        rows.append(
            {
                "timestamp": attrs.get("timestamp"),
                "status": attrs.get("status", "info"),
                "service": attrs.get("service", service),
                "message": attrs.get("message", ""),
                "attributes": inner,
            }
        )
    return rows, "live"


def _series_from_points(points: list[list[float]], window_start_ms: float) -> dict[str, Any]:
    before = [p[1] for p in points if p[0] < window_start_ms and p[1] is not None]
    during = [p[1] for p in points if p[0] >= window_start_ms and p[1] is not None]
    baseline = sum(before) / len(before) if before else 0.0
    current = sum(during) / len(during) if during else baseline
    change = ((current - baseline) / baseline * 100.0) if baseline else 0.0
    shifted_at = None
    if baseline and during:
        for ts, value in points:
            if value is not None and ts >= window_start_ms and value > baseline * 1.2:
                shifted_at = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                break
    return {
        "baseline": round(baseline, 2),
        "current": round(current, 2),
        "change_pct": round(change, 2),
        "shifted_at": shifted_at,
    }


METRIC_QUERIES = {
    "payment.latency.p95": "p95:trace.servlet.request.duration{{service:{service}}}",
    "http.5xx.rate": "sum:trace.servlet.request.errors{{service:{service}}}.as_rate()",
    "http.requests.rate": "sum:trace.servlet.request.hits{{service:{service}}}.as_rate()",
    "db.query.latency": "avg:postgresql.queries.duration{{service:{service}}}",
}


async def get_metrics(
    service: str, start: datetime, end: datetime
) -> tuple[dict[str, Any], str]:
    if not live():
        return fixtures.datadog_metrics(end, service), "demo"

    lookback = start - timedelta(hours=1)
    window_start_ms = start.timestamp() * 1000
    series: dict[str, Any] = {}
    for name, template in METRIC_QUERIES.items():
        try:
            data = await request(
                "GET",
                f"https://api.{settings.dd_site}/api/v1/query",
                headers=_headers(),
                params={
                    "from": int(lookback.timestamp()),
                    "to": int(end.timestamp()),
                    "query": template.format(service=service),
                },
            )
        except Exception as exc:
            series[name] = {"error": str(exc)[:160]}
            continue
        points: list[list[float]] = []
        for entry in data.get("series", []) or []:
            points.extend(entry.get("pointlist", []) or [])
        series[name] = (
            _series_from_points(points, window_start_ms)
            if points
            else {"baseline": 0.0, "current": 0.0, "change_pct": 0.0, "shifted_at": None}
        )
    return {"service": service, "series": series}, "live"
