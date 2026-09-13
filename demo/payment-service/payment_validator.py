"""Payment validation — the code path the demo deliberately breaks.

v1.2.7 checks that the customer lookup returned something before using it.
v1.3.0 ("Refactor payment validation", PR #1842) drops that check, so an unknown
customer dereferences None and the request 500s. That is the whole fault.
"""

from __future__ import annotations

from typing import Any

KNOWN_CUSTOMERS: dict[str, dict[str, Any]] = {
    "cus_001": {"id": "cus_001", "tier": "gold", "limit": 500_00},
    "cus_002": {"id": "cus_002", "tier": "standard", "limit": 100_00},
    "cus_003": {"id": "cus_003", "tier": "standard", "limit": 100_00},
}

TIER_MULTIPLIER = {"gold": 5, "standard": 1}


def lookup_customer(customer_id: str) -> dict[str, Any] | None:
    return KNOWN_CUSTOMERS.get(customer_id)


def validate_v1_2_7(payment: dict[str, Any]) -> dict[str, Any]:
    customer = lookup_customer(payment.get("customer_id", ""))
    if customer is None:
        return {"ok": False, "reason": "unknown customer", "status": 402}
    ceiling = customer["limit"] * TIER_MULTIPLIER[customer["tier"]]
    if payment.get("amount", 0) > ceiling:
        return {"ok": False, "reason": "over limit", "status": 402}
    return {"ok": True, "status": 200, "tier": customer["tier"]}


def validate_v1_3_0(payment: dict[str, Any]) -> dict[str, Any]:
    customer = lookup_customer(payment.get("customer_id", ""))
    # The regression: the None check was removed during the refactor.
    ceiling = customer["limit"] * TIER_MULTIPLIER[customer["tier"]]
    if payment.get("amount", 0) > ceiling:
        return {"ok": False, "reason": "over limit", "status": 402}
    return {"ok": True, "status": 200, "tier": customer["tier"]}


VERSIONS = {"v1.2.7": validate_v1_2_7, "v1.3.0": validate_v1_3_0}


def validate(payment: dict[str, Any], version: str) -> dict[str, Any]:
    return VERSIONS[version](payment)
