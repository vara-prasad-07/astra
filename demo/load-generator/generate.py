"""Steady traffic against payment-service, including unknown customers.

Unknown customers are harmless on v1.2.7 (402) and fatal on v1.3.0 (500), which
is what turns the deployment into a visible incident.

    python demo/load-generator/generate.py --seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import random

import httpx

CUSTOMERS = ["cus_001", "cus_002", "cus_003", "cus_404", "cus_999"]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8100")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--rps", type=int, default=10)
    args = parser.parse_args()

    sent = errors = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        deadline = asyncio.get_event_loop().time() + args.seconds
        while asyncio.get_event_loop().time() < deadline:
            payload = {
                "customer_id": random.choice(CUSTOMERS),
                "amount": random.randint(100, 40_000),
            }
            try:
                response = await client.post(f"{args.url}/v1/payments", json=payload)
                sent += 1
                if response.status_code >= 500:
                    errors += 1
            except Exception:
                errors += 1
            await asyncio.sleep(1 / max(1, args.rps))

    rate = (errors / sent * 100) if sent else 0.0
    print(f"sent={sent} errors={errors} error_rate={rate:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
