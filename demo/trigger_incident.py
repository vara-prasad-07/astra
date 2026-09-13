"""Scene 2 of the demo: ship the bad deployment and page NIGHTWATCH.

    python demo/trigger_incident.py

Ships deployment 8421 to the local payment-service if it is running, drives
enough traffic to make the error rate real, then posts a PagerDuty-shaped
incident.triggered webhook at NIGHTWATCH exactly as PagerDuty would.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo import fixtures  # noqa: E402

CUSTOMERS = ["cus_001", "cus_002", "cus_003", "cus_404", "cus_999"]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nightwatch", default="http://127.0.0.1:8000")
    parser.add_argument("--service", default="http://127.0.0.1:8100")
    parser.add_argument("--requests", type=int, default=40)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            deployed = await client.post(f"{args.service}/admin/deploy")
            print(f"  shipped deployment {deployed.json()['deployment']} "
                  f"({deployed.json()['version']}) to payment-service")

            for _ in range(args.requests):
                await client.post(
                    f"{args.service}/v1/payments",
                    json={
                        "customer_id": random.choice(CUSTOMERS),
                        "amount": random.randint(100, 40_000),
                    },
                )
            stats = (await client.get(f"{args.service}/admin/stats")).json()
            rate = stats["errors"] / max(1, stats["requests"]) * 100
            print(f"  traffic driven: {stats['requests']} requests, "
                  f"{stats['errors']} errors ({rate:.0f}%)")
        except Exception:
            print("  payment-service is not running — firing the alert anyway")
            print("  (start it with: python demo/payment-service/app.py)")

        print("  paging NIGHTWATCH...")
        try:
            response = await client.post(
                f"{args.nightwatch}/webhooks/pagerduty",
                json=fixtures.pagerduty_webhook(),
            )
        except Exception as exc:
            print(f"  could not reach NIGHTWATCH at {args.nightwatch}: {exc}")
            print("  start it with: python main.py serve")
            return 1

    if response.status_code != 202:
        print(f"  NIGHTWATCH returned {response.status_code}: {response.text[:200]}")
        return 1

    incident_id = response.json().get("incident_id")
    print(f"  incident {incident_id} accepted")
    print(f"  watch the swarm at {args.nightwatch}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
