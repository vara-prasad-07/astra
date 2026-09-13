"""The deterministic incident world used whenever an integration has no keys.

Timings reproduce the causal chain from the design record exactly:

    T-305s  deployment #8421 (PR #1842) ships
    T-236s  payment latency +12%
    T-184s  5xx rate begins rising
    T-46s   first NullPointerException
    T       PagerDuty triggers the incident

Two decoy PRs and two mild infrastructure anomalies (slightly elevated DB
latency, slightly elevated traffic) are included on purpose: the correlation
scorer has to genuinely discriminate rather than pick the only candidate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SERVICE = "payment-service"
DEPLOYMENT_ID = "8421"
PR_NUMBER = 1842
FAILING_FILE = "src/main/java/com/acme/payments/PaymentValidator.java"
ERROR_TYPE = "NullPointerException"
ERROR_COUNT = 87

STACK_TRACE = (
    'java.lang.NullPointerException: Cannot invoke "com.acme.payments.Customer.getTier()" '
    'because "customer" is null\n'
    "\tat com.acme.payments.PaymentValidator.validate(PaymentValidator.java:142)\n"
    "\tat com.acme.payments.PaymentService.processPayment(PaymentService.java:88)\n"
    "\tat com.acme.payments.PaymentController.charge(PaymentController.java:54)"
)

OFFSETS = {
    "deploy": -305,
    "latency_shift": -236,
    "error_rate_rising": -184,
    "first_error": -46,
    "triggered": 0,
}


def anchor(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(microsecond=0)


def at(trigger: datetime, key: str) -> datetime:
    return trigger + timedelta(seconds=OFFSETS[key])


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def pagerduty_webhook(trigger: datetime | None = None) -> dict[str, Any]:
    """A PagerDuty V3 `incident.triggered` webhook payload."""
    trigger = anchor(trigger)
    return {
        "event": {
            "id": "01DMX7TGJEO0RQFVJLUNMT3IYU",
            "event_type": "incident.triggered",
            "resource_type": "incident",
            "occurred_at": _iso(trigger),
            "agent": {"type": "service_reference", "summary": "Datadog monitor"},
            "data": {
                "id": "PNW8421",
                "type": "incident",
                "number": 8421,
                "status": "triggered",
                "title": f"High 5xx error rate on {SERVICE}",
                "html_url": "https://acme.pagerduty.com/incidents/PNW8421",
                "created_at": _iso(trigger),
                "urgency": "high",
                "priority": {"id": "P1", "summary": "P1"},
                "service": {
                    "id": "PSVCPAY",
                    "type": "service_reference",
                    "summary": SERVICE,
                },
                "body": {
                    "details": (
                        f"Monitor 'payment 5xx rate' triggered. Error rate 12.4% "
                        f"(threshold 1%). Recent deployment: {DEPLOYMENT_ID}."
                    )
                },
            },
        }
    }


def datadog_logs(
    trigger: datetime | None = None, service: str = SERVICE
) -> list[dict[str, Any]]:
    """Raw-ish log rows: 87 matching exceptions plus realistic surrounding noise.

    The fixture world only knows about payment-service. Asking about any other
    service correctly returns nothing, which is what lets the confidence floor be
    exercised rather than assumed.
    """
    if service != SERVICE:
        return []
    trigger = anchor(trigger)
    first_error = at(trigger, "first_error")
    deploy = at(trigger, "deploy")
    logs: list[dict[str, Any]] = []

    # Healthy traffic before the deployment — proves the signature is new.
    for i in range(40):
        logs.append(
            {
                "timestamp": _iso(deploy - timedelta(seconds=600 - i * 14)),
                "status": "info",
                "service": SERVICE,
                "message": f"POST /v1/payments 200 in {28 + (i % 9)}ms",
                "attributes": {"http.status_code": 200, "endpoint": "/v1/payments"},
            }
        )

    # Pre-existing, unrelated warning noise.
    for i in range(6):
        logs.append(
            {
                "timestamp": _iso(deploy - timedelta(seconds=500 - i * 60)),
                "status": "warn",
                "service": SERVICE,
                "message": "Slow query: SELECT * FROM customers WHERE id = ? took 210ms",
                "attributes": {"db.instance": "payments-primary"},
            }
        )

    # The incident itself.
    span = max(1, int((trigger - first_error).total_seconds()))
    for i in range(ERROR_COUNT):
        ts = first_error + timedelta(seconds=(i * span) // ERROR_COUNT)
        logs.append(
            {
                "timestamp": _iso(ts),
                "status": "error",
                "service": SERVICE,
                "message": f"Unhandled exception processing charge\n{STACK_TRACE}",
                "attributes": {
                    "error.kind": f"java.lang.{ERROR_TYPE}",
                    "error.stack": STACK_TRACE,
                    "http.status_code": 500,
                    "endpoint": "/v1/payments",
                    "version": DEPLOYMENT_ID,
                },
            }
        )

    # A different, low-volume error that must not win the "dominant pattern" vote.
    for i in range(4):
        logs.append(
            {
                "timestamp": _iso(first_error + timedelta(seconds=i * 9)),
                "status": "error",
                "service": SERVICE,
                "message": "ValidationError: unsupported currency 'XBT'",
                "attributes": {
                    "error.kind": "ValidationError",
                    "http.status_code": 400,
                    "endpoint": "/v1/payments",
                },
            }
        )

    logs.sort(key=lambda row: row["timestamp"])
    return logs


def datadog_metrics(
    trigger: datetime | None = None, service: str = SERVICE
) -> dict[str, Any]:
    """Metric series with the step changes the Timeline agent looks for.

    `db.query.latency` is elevated but *flat* across the error onset, and traffic
    is only marginally above baseline — both are deliberately weak signals.
    """
    if service != SERVICE:
        return {"service": service, "series": {}}
    trigger = anchor(trigger)
    return {
        "service": SERVICE,
        "series": {
            "payment.latency.p95": {
                "unit": "ms",
                "baseline": 240.0,
                "current": 269.0,
                "change_pct": 12.1,
                "shifted_at": _iso(at(trigger, "latency_shift")),
            },
            "http.5xx.rate": {
                "unit": "%",
                "baseline": 0.04,
                "current": 12.4,
                "change_pct": 30900.0,
                "shifted_at": _iso(at(trigger, "error_rate_rising")),
            },
            "http.requests.rate": {
                "unit": "req/s",
                "baseline": 415.0,
                "current": 440.0,
                "change_pct": 6.0,
                "shifted_at": None,
            },
            "db.query.latency": {
                "unit": "ms",
                "baseline": 180.0,
                "current": 206.0,
                "change_pct": 14.4,
                "shifted_at": _iso(trigger - timedelta(hours=3)),
            },
        },
    }


def github_pull_requests(
    trigger: datetime | None = None, service: str = SERVICE
) -> list[dict[str, Any]]:
    """Recent merged PRs. Only one touches the file in the stack trace."""
    if service != SERVICE:
        return []
    trigger = anchor(trigger)
    deploy = at(trigger, "deploy")
    return [
        {
            "number": PR_NUMBER,
            "title": "Refactor payment validation",
            "user": {"login": "alex"},
            "merged_at": _iso(deploy - timedelta(seconds=12)),
            "html_url": f"https://github.com/acme/platform/pull/{PR_NUMBER}",
            "merge_commit_sha": "9f2c41b8ad5e7c30b19ee4417d0a2f88c61d3a7e",
            "service": SERVICE,
            "files": [
                {"filename": FAILING_FILE, "additions": 34, "deletions": 21},
                {
                    "filename": "src/test/java/com/acme/payments/PaymentValidatorTest.java",
                    "additions": 12,
                    "deletions": 3,
                },
            ],
        },
        {
            "number": 1840,
            "title": "Bump checkout button contrast for accessibility",
            "user": {"login": "priya"},
            "merged_at": _iso(deploy - timedelta(hours=2)),
            "html_url": "https://github.com/acme/platform/pull/1840",
            "merge_commit_sha": "1a7de90c4b2f8813ac5e6d2200b91f4477cc0e15",
            "service": "web-frontend",
            "files": [{"filename": "web/src/components/CheckoutButton.tsx", "additions": 4, "deletions": 4}],
        },
        {
            "number": 1839,
            "title": "Document payment retry semantics",
            "user": {"login": "sam"},
            "merged_at": _iso(deploy - timedelta(hours=6)),
            "html_url": "https://github.com/acme/platform/pull/1839",
            "merge_commit_sha": "c0ffee1234567890abcdef1234567890abcdef12",
            "service": SERVICE,
            "files": [{"filename": "docs/payments/retries.md", "additions": 60, "deletions": 2}],
        },
    ]


def deployments(
    trigger: datetime | None = None, service: str = SERVICE
) -> list[dict[str, Any]]:
    if service != SERVICE:
        return []
    trigger = anchor(trigger)
    return [
        {
            "id": DEPLOYMENT_ID,
            "service": SERVICE,
            "version": "v1.3.0",
            "previous_version": "v1.2.7",
            "deployed_at": _iso(at(trigger, "deploy")),
            "pr_number": PR_NUMBER,
            "sha": "9f2c41b8ad5e7c30b19ee4417d0a2f88c61d3a7e",
            "status": "active",
        },
        {
            "id": "8419",
            "service": SERVICE,
            "version": "v1.2.7",
            "previous_version": "v1.2.6",
            "deployed_at": _iso(trigger - timedelta(days=2)),
            "pr_number": 1831,
            "sha": "5d8b1c0e9a4f7362d15ab8c7709e4411bb2f6d03",
            "status": "superseded",
        },
    ]


def post_rollback_metrics(trigger: datetime | None = None) -> dict[str, Any]:
    """What Datadog reports once the rollback has taken effect."""
    metrics = datadog_metrics(trigger)
    metrics["series"]["http.5xx.rate"]["current"] = 0.03
    metrics["series"]["http.5xx.rate"]["change_pct"] = -25.0
    metrics["series"]["payment.latency.p95"]["current"] = 241.0
    metrics["series"]["payment.latency.p95"]["change_pct"] = 0.4
    return metrics
