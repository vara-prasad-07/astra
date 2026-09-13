"""The breakable payment-service.

Run it and NIGHTWATCH's rollback becomes real: /admin/deploy ships the bad
version, the error rate climbs, and the Operator's rollback actually restores it.

    python demo/payment-service/app.py        # serves on :8100
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))
import datadog_logs  # noqa: E402
import payment_validator  # noqa: E402

GOOD_VERSION = "v1.2.7"
BAD_VERSION = "v1.3.0"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s payment-service %(message)s"
)
log = logging.getLogger("payment-service")

if datadog_logs.install(log):
    log.info("shipping logs to Datadog (service=%s)", os.environ.get("DD_SERVICE", "payment-service"))

app = FastAPI(title="payment-service")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


state: dict[str, Any] = {
    "version": GOOD_VERSION,
    "deployment_id": "8419",
    "previous_version": None,
    "previous_deployment_id": None,
    "deployed_at": _now(),
    "requests": 0,
    "errors": 0,
    "recent_errors": [],
}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": state["version"], "deployment": state["deployment_id"]}


@app.get("/admin/stats")
async def stats() -> dict[str, Any]:
    return {
        "version": state["version"],
        "deployment": state["deployment_id"],
        "previous_version": state["previous_version"],
        "previous_deployment": state["previous_deployment_id"],
        "deployed_at": state["deployed_at"],
        "requests": state["requests"],
        "errors": state["errors"],
        "recent_errors": state["recent_errors"][-5:],
    }


@app.post("/admin/deploy")
async def deploy() -> dict[str, Any]:
    """Ship deployment 8421 — the one that carries the regression."""
    state.update(
        previous_version=state["version"],
        previous_deployment_id=state["deployment_id"],
        version=BAD_VERSION,
        deployment_id="8421",
        deployed_at=_now(),
        requests=0,
        errors=0,
    )
    state["recent_errors"].clear()
    log.warning("deployed 8421 (%s)", BAD_VERSION)
    return {"version": state["version"], "deployment": state["deployment_id"]}


@app.post("/admin/rollback")
async def rollback() -> dict[str, Any]:
    state.update(
        previous_version=state["version"],
        previous_deployment_id=state["deployment_id"],
        version=GOOD_VERSION,
        deployment_id="8419",
        deployed_at=_now(),
        requests=0,
        errors=0,
    )
    state["recent_errors"].clear()
    log.warning("rolled back to %s", GOOD_VERSION)
    return {"version": state["version"], "deployment": state["deployment_id"]}


@app.post("/v1/payments")
async def charge(request: Request) -> JSONResponse:
    payload = await request.json()
    state["requests"] += 1
    try:
        payload["currency"] = payload["currency"].upper()
        print(f"charge request: {payload}")
        result = payment_validator.validate(payload, state["version"])
    except Exception as exc:
        state["errors"] += 1
        trace = traceback.format_exc()
        state["recent_errors"].append(
            {"kind": type(exc).__name__, "message": str(exc), "stack": trace}
        )
        log.error(
            "Unhandled exception processing charge version=%s deployment=%s kind=%s message=%s\n%s",
            state["version"], state["deployment_id"], type(exc).__name__, str(exc), trace,
        )
        return JSONResponse(
            {"error": type(exc).__name__, "message": str(exc)}, status_code=500
        )
    return JSONResponse(result, status_code=result.get("status", 200))


if __name__ == "__main__":
    import uvicorn

    # Render (and most PaaS hosts) assign the port via $PORT and expect a bind
    # on 0.0.0.0; local runs keep the old 127.0.0.1:8100 defaults.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run(app, host=host, port=port, log_level="warning")
