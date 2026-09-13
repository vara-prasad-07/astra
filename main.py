"""NIGHTWATCH — Autonomous Incident Commander Swarm.

Entry point for everything:

    python main.py serve      # webhooks + live dashboard on :8000
    python main.py demo       # scripted terminal demo, no browser needed
    python main.py validate   # the section 10 pass/fail checks
    python main.py status     # which integrations are live vs demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from config import ROOT, settings
from demo import fixtures
from integrations import pagerduty, slack
from orchestrator.events import bus, to_sse
from orchestrator.runner import decide, investigate, new_incident_id
from storage import db

DASHBOARD = ROOT / "static" / "dashboard.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="NIGHTWATCH", version="1.0.0", lifespan=lifespan)


# --- dashboard ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    if not DASHBOARD.exists():
        return "<h1>NIGHTWATCH</h1><p>static/dashboard.html is missing.</p>"
    return DASHBOARD.read_text(encoding="utf-8")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "nightwatch"}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "integrations": [
            {"name": name, "mode": mode, "missing": missing}
            for name, mode, missing in settings.summary()
        ],
        "llm": settings.llm_mode,
        "approvers": settings.approvers or ["(anyone — no allowlist configured)"],
    }


@app.get("/api/incidents")
async def api_incidents() -> list[dict[str, Any]]:
    return db.list_incidents()


@app.get("/api/incidents/{incident_id}")
async def api_incident(incident_id: str) -> dict[str, Any]:
    state = db.load_state(incident_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")
    state["approvals"] = db.approvals_for(incident_id)
    return state


@app.get("/api/slack/outbox")
async def api_slack_outbox() -> list[dict[str, Any]]:
    """What NIGHTWATCH posted to Slack (captured locally when Slack is in demo mode)."""
    return slack.demo_outbox


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    async def stream():
        async for event in bus.subscribe():
            yield to_sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- webhooks ----------------------------------------------------------------


@app.post("/webhooks/pagerduty")
async def pagerduty_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    body = await request.body()
    if not pagerduty.verify_signature(body, request.headers.get("x-pagerduty-signature")):
        raise HTTPException(status_code=401, detail="invalid PagerDuty signature")

    payload = json.loads(body or b"{}")
    event_type = (payload.get("event") or {}).get("event_type", "")
    if event_type and event_type != "incident.triggered":
        return JSONResponse({"ignored": event_type})

    incident_id = new_incident_id()
    # PagerDuty expects a fast ack; the swarm runs behind it.
    background.add_task(investigate, payload, incident_id)
    return JSONResponse({"accepted": True, "incident_id": incident_id}, status_code=202)


async def _run_slack_decision(incident_id: str, decision: str, approver: str) -> None:
    """A real rollback (Render) can take well over Slack's ~3s ack window, so
    the decision runs off the request thread; failures are reported into the
    incident's own Slack thread instead of the (already-sent) HTTP response."""
    try:
        await decide(incident_id, decision, approver, source="slack")
    except (KeyError, ValueError) as exc:
        state = db.load_state(incident_id)
        thread_ts = (state or {}).get("slack_ref", {}).get("ts")
        await slack.post_thread_update(incident_id, thread_ts, f"NIGHTWATCH: {exc}")


@app.post("/webhooks/slack")
async def slack_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    body = await request.body()
    if not slack.verify_signature(
        body,
        request.headers.get("x-slack-request-timestamp"),
        request.headers.get("x-slack-signature"),
    ):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    form = await request.form()
    raw = form.get("payload")
    payload = json.loads(raw) if raw else json.loads(body or b"{}")

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    interaction = slack.parse_interaction(payload)
    if not interaction:
        return JSONResponse({"ignored": True})

    # Ack immediately: a real rollback (Render) can run well past Slack's
    # retry timeout, and a retry landing here mid-rollback would double-fire it.
    background.add_task(
        _run_slack_decision,
        interaction["incident_id"],
        interaction["decision"],
        interaction["approver"],
    )
    return JSONResponse({"text": f"NIGHTWATCH recorded: {interaction['decision']}"})


# --- local control (dashboard buttons / demo without a Slack workspace) -------


@app.post("/api/demo/trigger")
async def api_demo_trigger(background: BackgroundTasks) -> dict[str, Any]:
    incident_id = new_incident_id()
    background.add_task(investigate, fixtures.pagerduty_webhook(), incident_id)
    return {"incident_id": incident_id, "status": "investigating"}


@app.post("/api/incidents/{incident_id}/decision")
async def api_decision(incident_id: str, request: Request) -> dict[str, Any]:
    payload = await request.json()
    decision = payload.get("decision", "")
    if decision not in {"approve", "reject", "investigate"}:
        raise HTTPException(status_code=400, detail="decision must be approve/reject/investigate")
    try:
        result = await decide(
            incident_id, decision, payload.get("approver", "dashboard"), source="dashboard"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": result.get("status"),
        "action_result": result.get("action_result"),
    }


# --- CLI ---------------------------------------------------------------------


def print_status() -> None:
    print("\nNIGHTWATCH integration status")
    print("-" * 58)
    for name, mode, missing in settings.summary():
        note = f"  (missing: {', '.join(missing)})" if missing else ""
        print(f"  {name:<12} {mode:<6}{note}")
    print("-" * 58)
    print("  demo mode uses deterministic fixtures — the full workflow still runs.")
    print("  run `python setup.py` to add real API keys.\n")


async def run_terminal_demo() -> int:
    """The scripted demo from section 9, without needing a browser or Slack."""
    from Agents.operator import format_duration

    db.init()
    slack.demo_outbox.clear()

    print("\n" + "=" * 66)
    print("  NIGHTWATCH — Autonomous Incident Commander Swarm")
    print("  Your AI incident commander for the 2 AM production failure.")
    print("=" * 66)

    print("\n[Scene 1] Normal world: payment-service v1.2.7, error rate 0.04%.")
    print("[Scene 2] Injecting failure: deployment 8421 ships PR #1842...")
    print("          PagerDuty fires a P1 alert.\n")

    print("[Scene 3] Swarm activates.")
    result = await investigate(fixtures.pagerduty_webhook())
    for trace in result.get("traces") or []:
        print(f"          {trace['agent']:<22} {trace['status']:<9} "
              f"{trace.get('duration_ms', 0):>7.1f}ms  [{trace.get('mode', '-')}]")

    print("\n[Scene 4] Evidence correlates.")
    for hypothesis in result.get("hypotheses") or []:
        bar = "#" * int(hypothesis["score"] * 40)
        print(f"          {hypothesis['score']:>5.0%} |{bar:<40}| {hypothesis['name']}")

    print("\n          Timeline:")
    for event in result.get("timeline") or []:
        print(f"            {event['clock']}  {event['description']}")

    recommendation = result.get("recommendation") or {}
    print("\n[Scene 5] Slack report posted.")
    print("-" * 66)
    print(f"  Root cause: {recommendation.get('root_cause')}")
    print(f"  Confidence: {recommendation.get('confidence', 0):.0%}")
    print(f"  Risk:       {recommendation.get('risk')} — {recommendation.get('risk_notes')}")
    print("  Evidence:")
    for bullet in recommendation.get("evidence_bullets") or []:
        print(f"    - {bullet}")
    print(f"\n  Recommended action: {recommendation.get('action_type')} deployment "
          f"{recommendation.get('action_target')}")
    print("  [ Approve Rollback ]   [ Reject ]   [ Investigate Further ]")
    print("-" * 66)

    print("\n[Scene 6] Human approval required. Nothing has been executed.")
    try:
        answer = input("          Approve the rollback? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    incident_id = result["incident_id"]
    if answer not in {"y", "yes"}:
        await decide(incident_id, "reject", "operator", source="terminal")
        print("\n          Rejected. No production change was made.\n")
        return 0

    print("\n[Scene 7] Verifying, acting, confirming recovery.")
    remediated = await decide(incident_id, "approve", "operator", source="terminal")
    action = remediated.get("action_result") or {}
    for check in action.get("checks") or []:
        mark = "ok  " if check["passed"] else "FAIL"
        print(f"          [{mark}] {check['step']}")
        if check.get("detail"):
            print(f"                 {check['detail']}")

    print(f"\n          Health restored: {action.get('health_restored')}")
    print(f"          Error rate after rollback: {action.get('error_rate_after')}%")
    print(f"          MTTR: {format_duration(action.get('mttr_seconds'))}")
    print(f"\n          Incident {incident_id} closed.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nightwatch", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "demo", "validate", "status"],
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()

    if args.command == "status":
        print_status()
        return 0

    if args.command == "validate":
        import validate

        return asyncio.run(validate.main())

    if args.command == "demo":
        return asyncio.run(run_terminal_demo())

    import uvicorn

    print_status()
    print(f"  dashboard:  http://{args.host}:{args.port}/")
    print(f"  pagerduty:  POST http://{args.host}:{args.port}/webhooks/pagerduty")
    print(f"  slack:      POST http://{args.host}:{args.port}/webhooks/slack\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
