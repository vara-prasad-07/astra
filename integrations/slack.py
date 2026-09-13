"""Slack: the approval surface. Block Kit report + interactive buttons.

In demo mode the identical Block Kit payload is captured in memory and rendered
by the local dashboard, so the approval step is real even without a workspace.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from config import settings
from models import Recommendation

from ._http import request

API = "https://slack.com/api"

# Block Kit payloads posted while in demo mode, newest last.
demo_outbox: list[dict[str, Any]] = []


def live() -> bool:
    return settings.is_live(settings.slack)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.slack.required['SLACK_BOT_TOKEN']}",
        "Content-Type": "application/json; charset=utf-8",
    }


def verify_signature(body: bytes, timestamp: str | None, signature: str | None) -> bool:
    secret = settings.slack_signing_secret
    if not secret:
        return True
    if not timestamp or not signature:
        return False
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    base = f"v0:{timestamp}:{body.decode('utf-8', 'replace')}".encode()
    digest = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def build_report_blocks(
    incident_id: str,
    service: str,
    recommendation: Recommendation,
    timeline: list[str],
    hypotheses: list[tuple[str, float]],
) -> list[dict[str, Any]]:
    confidence_pct = round(recommendation.confidence * 100)
    evidence = "\n".join(f"• {bullet}" for bullet in recommendation.evidence_bullets)
    ranked = "\n".join(
        f"• `{round(score * 100):>3}%`  {name}" for name, score in hypotheses
    )
    clock = "\n".join(f"`{line}`" for line in timeline)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"NIGHTWATCH — {service} incident"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Root cause:* {recommendation.root_cause}\n"
                    f"*Confidence:* {confidence_pct}%\n"
                    f"*Risk of proposed action:* {recommendation.risk} — {recommendation.risk_notes}"
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence*\n{evidence}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Timeline*\n{clock}"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Hypotheses considered*\n{ranked}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Recommended action:* {recommendation.action_type} "
                f"`{recommendation.action_target or 'n/a'}`",
            },
        },
        {
            "type": "actions",
            "block_id": f"nightwatch::{incident_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve Rollback"},
                    "value": f"approve::{incident_id}",
                    "action_id": "nightwatch_approve",
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": f"reject::{incident_id}",
                    "action_id": "nightwatch_reject",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Investigate Further"},
                    "value": f"investigate::{incident_id}",
                    "action_id": "nightwatch_investigate",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "No action is taken until a human approves. "
                    f"Incident `{incident_id}`",
                }
            ],
        },
    ]
    return blocks


async def post_report(
    incident_id: str, text: str, blocks: list[dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    if not live():
        entry = {
            "incident_id": incident_id,
            "channel": settings.slack_channel or "#demo-incidents",
            "ts": f"demo-{incident_id}",
            "text": text,
            "blocks": blocks,
        }
        demo_outbox.append(entry)
        return entry, "demo"

    data = await request(
        "POST",
        f"{API}/chat.postMessage",
        headers=_headers(),
        json={
            "channel": settings.slack_channel,
            "text": text,
            "blocks": blocks,
        },
    )
    if not data.get("ok"):
        raise RuntimeError(f"slack chat.postMessage failed: {data.get('error')}")
    return {"ts": data.get("ts"), "channel": data.get("channel")}, "live"


async def post_thread_update(
    incident_id: str, thread_ts: str | None, text: str
) -> tuple[bool, str]:
    if not live():
        demo_outbox.append(
            {
                "incident_id": incident_id,
                "channel": settings.slack_channel or "#demo-incidents",
                "thread_ts": thread_ts,
                "text": text,
                "blocks": [],
            }
        )
        return True, "demo"
    await request(
        "POST",
        f"{API}/chat.postMessage",
        headers=_headers(),
        json={
            "channel": settings.slack_channel,
            "thread_ts": thread_ts,
            "text": text,
        },
    )
    return True, "live"


def parse_interaction(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull (decision, incident_id, approver) out of a Block Kit interaction."""
    actions = payload.get("actions") or []
    if not actions:
        return None
    value = actions[0].get("value", "")
    if "::" not in value:
        return None
    decision, incident_id = value.split("::", 1)
    user = payload.get("user") or {}
    return {
        "decision": decision,
        "incident_id": incident_id,
        "approver": user.get("username") or user.get("id") or "unknown",
        "thread_ts": (payload.get("message") or {}).get("ts"),
    }
