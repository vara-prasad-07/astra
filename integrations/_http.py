"""Shared HTTP helper. Every integration speaks plain REST via httpx — no vendor
SDKs, so the dependency surface stays small and the calls stay inspectable."""

from __future__ import annotations

from typing import Any

import httpx

TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class IntegrationError(RuntimeError):
    pass


async def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.request(
            method, url, headers=headers, params=params, json=json
        )
    if response.status_code >= 400:
        raise IntegrationError(
            f"{method} {url} -> {response.status_code}: {response.text[:300]}"
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}
