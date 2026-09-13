"""NIGHTWATCH setup wizard — tells you exactly which API keys are needed, where
to get each one, and writes them to .env.

This is not a setuptools build script. Run it directly:

    python setup.py            # interactive wizard
    python setup.py --check    # report what is configured, change nothing
    python setup.py --verify   # call each API to prove the credentials work

Nothing here is mandatory. Every integration NIGHTWATCH cannot reach falls back
to deterministic demo fixtures, and the full workflow still runs end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"


@dataclass
class Key:
    name: str
    prompt: str
    required: bool = True
    default: str = ""
    secret: bool = True


@dataclass
class Service:
    name: str
    purpose: str
    where: str
    free: str
    steps: list[str]
    keys: list[Key]
    note: str = ""
    scopes: list[str] = field(default_factory=list)


SERVICES = [
    Service(
        name="PagerDuty",
        purpose="Incident trigger — the entry point that wakes the swarm",
        where="https://developer.pagerduty.com  (free developer account)",
        free="Free developer account; 14-day trial for full features",
        steps=[
            "Sign up, then create a Service (Services -> New Service).",
            "User icon -> My Profile -> User Settings -> Create API User Token.",
            "Integrations -> Generic Webhooks (v3) -> add a webhook subscription",
            "  pointing at  https://<your-ngrok>.ngrok.io/webhooks/pagerduty",
            "Subscribe to the incident.triggered event.",
        ],
        keys=[
            Key("PAGERDUTY_API_KEY", "PagerDuty REST API token"),
            Key(
                "PAGERDUTY_FROM_EMAIL",
                "Email of the PagerDuty user the token belongs to",
                secret=False,
            ),
            Key(
                "PAGERDUTY_WEBHOOK_SECRET",
                "Webhook signing secret (optional but recommended)",
                required=False,
            ),
        ],
        note="Without these, NIGHTWATCH replays a fixture PagerDuty alert instead.",
    ),
    Service(
        name="Datadog",
        purpose="Logs and metrics — the Log Detective and Metrics Analyst read these",
        where="https://app.datadoghq.com/organization-settings/api-keys",
        free="14-day free trial (no card required)",
        steps=[
            "Organization Settings -> API Keys -> New Key.",
            "Organization Settings -> Application Keys -> New Key.",
            "The application key needs logs_read_data scope.",
            "If your account is on the EU site, set DD_SITE=datadoghq.eu",
        ],
        keys=[
            Key("DD_API_KEY", "Datadog API key"),
            Key("DD_APP_KEY", "Datadog application key (needs logs_read_data)"),
            Key("DD_SITE", "Datadog site", required=False, default="datadoghq.com", secret=False),
        ],
        note="Without these, log and metric evidence comes from fixtures.",
    ),
    Service(
        name="GitHub",
        purpose="PRs, commits and changed files — the Code Archaeologist",
        where="https://github.com/settings/tokens?type=beta",
        free="Free",
        steps=[
            "Settings -> Developer settings -> Personal access tokens ->",
            "  Fine-grained tokens -> Generate new token.",
            "Select the repository you will demo against.",
            "Repository permissions: Contents = Read, Pull requests = Read,",
            "  Deployments = Read.",
        ],
        keys=[
            Key("GITHUB_TOKEN", "GitHub fine-grained personal access token"),
            Key("GITHUB_REPO", "Repository as owner/name (e.g. acme/platform)", secret=False),
        ],
        scopes=["contents:read", "pull_requests:read", "deployments:read"],
        note="Without these, the PR history comes from fixtures.",
    ),
    Service(
        name="Slack",
        purpose="The approval surface — report, buttons, and the human gate",
        where="https://api.slack.com/apps",
        free="Free workspace is fine",
        steps=[
            "Create New App -> From scratch, pick your workspace.",
            "OAuth & Permissions -> Bot Token Scopes: chat:write, channels:read,",
            "  users:read. Install to Workspace, copy the xoxb- token.",
            "Interactivity & Shortcuts -> turn on -> Request URL:",
            "  https://<your-ngrok>.ngrok.io/webhooks/slack",
            "Basic Information -> copy the Signing Secret.",
            "Invite the bot to the channel:  /invite @your-app",
        ],
        keys=[
            Key("SLACK_BOT_TOKEN", "Slack bot token (starts with xoxb-)"),
            Key("SLACK_CHANNEL", "Channel ID or #name to post incidents to", secret=False),
            Key(
                "SLACK_SIGNING_SECRET",
                "Slack signing secret (optional but recommended)",
                required=False,
            ),
        ],
        note="Without these, the same Block Kit payload renders on the local dashboard.",
    ),
    Service(
        name="LLM provider",
        purpose="Narrative summaries. Scoring is deterministic and never uses the model.",
        where="https://console.groq.com/keys  or  https://aistudio.google.com/apikey",
        free="Both have generous free tiers",
        steps=[
            "Groq is recommended for the demo: inference is fast enough to keep",
            "  the whole investigation under a second.",
            "Gemini works as a drop-in alternative. Set at least one.",
        ],
        keys=[
            Key("GROQ_API_KEY", "Groq API key", required=False),
            Key("GEMINI_API_KEY", "Google AI Studio (Gemini) API key", required=False),
            Key(
                "LLM_PROVIDER",
                "Preferred provider: auto | groq | gemini | mock",
                required=False,
                default="auto",
                secret=False,
            ),
        ],
        note="Without a key, summaries fall back to deterministic templates.",
    ),
]

EXTRA = [
    Key(
        "NIGHTWATCH_APPROVERS",
        "Comma-separated usernames allowed to approve a rollback (blank = anyone)",
        required=False,
        secret=False,
    ),
]


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    lines = [
        "# NIGHTWATCH configuration — generated by setup.py",
        "# Any value left blank keeps that integration on deterministic demo fixtures.",
        "",
    ]
    for service in SERVICES:
        lines.append(f"# --- {service.name}: {service.purpose}")
        for key in service.keys:
            lines.append(f"{key.name}={values.get(key.name, key.default)}")
        lines.append("")
    lines.append("# --- NIGHTWATCH runtime")
    for key in EXTRA:
        lines.append(f"{key.name}={values.get(key.name, key.default)}")
    for name, default in (
        ("NIGHTWATCH_MODE", "auto"),
        ("NIGHTWATCH_HOST", "127.0.0.1"),
        ("NIGHTWATCH_PORT", "8000"),
        ("DEMO_SERVICE_URL", "http://127.0.0.1:8100"),
    ):
        lines.append(f"{name}={values.get(name, default)}")
    lines.append("")
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "*" * max(0, len(value) - 8) + value[-4:] if len(value) > 8 else "****"


def report() -> int:
    values = read_env()
    values = {k: (os.getenv(k) or v) for k, v in values.items()}
    print("\nNIGHTWATCH — required APIs\n" + "=" * 70)
    missing_required = 0
    for service in SERVICES:
        required = [k for k in service.keys if k.required]
        have = [k for k in required if values.get(k.name)]
        if service.name == "LLM provider":
            # No single key here is "required" — any one of Groq/Gemini makes
            # this live, so it needs its own rule instead of the all-required check.
            state = (
                "LIVE"
                if (values.get("GROQ_API_KEY") or values.get("GEMINI_API_KEY"))
                and values.get("LLM_PROVIDER", "auto") != "mock"
                else "DEMO"
            )
        else:
            state = "LIVE" if required and len(have) == len(required) else "DEMO"
        if state == "DEMO":
            missing_required += 1
        print(f"\n  {service.name:<14} [{state}]   {service.purpose}")
        print(f"  {'':<14} get it: {service.where}")
        for key in service.keys:
            value = values.get(key.name, "")
            flag = "set" if value else ("MISSING" if key.required else "optional")
            shown = f"  = {mask(value) if key.secret else value}" if value else ""
            print(f"      {key.name:<26} {flag}{shown}")
        if state == "DEMO" and service.note:
            print(f"      -> {service.note}")
    print("\n" + "=" * 70)
    if missing_required:
        print(f"  {missing_required} integration(s) will run on demo fixtures.")
        print("  That is fine — `python validate.py` still passes all checks.")
        print("  Run `python setup.py` to fill them in.")
    else:
        print("  All integrations configured for live mode.")
    print()
    return 0


def wizard() -> int:
    values = read_env()
    print("\nNIGHTWATCH setup")
    print("=" * 70)
    print("Press Enter to skip any key. Skipped integrations use demo fixtures,")
    print("and the full incident workflow still runs end to end.\n")

    for service in SERVICES:
        print("-" * 70)
        print(f"{service.name} — {service.purpose}")
        print(f"  Where:  {service.where}")
        print(f"  Cost:   {service.free}")
        for step in service.steps:
            print(f"    {step}")
        if service.scopes:
            print(f"  Scopes: {', '.join(service.scopes)}")
        print()
        for key in service.keys:
            existing = values.get(key.name, "") or key.default
            hint = f" [{mask(existing) if key.secret else existing}]" if existing else ""
            tag = "" if key.required else " (optional)"
            try:
                entered = input(f"  {key.prompt}{tag}{hint}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted; nothing written.")
                return 1
            values[key.name] = entered or existing
        print()

    print("-" * 70)
    for key in EXTRA:
        existing = values.get(key.name, "")
        hint = f" [{existing}]" if existing else ""
        try:
            entered = input(f"  {key.prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        values[key.name] = entered or existing

    write_env(values)
    print(f"\nWrote {ENV_PATH}")
    print("Next:")
    print("  python setup.py --verify    check the credentials actually work")
    print("  python validate.py          run the 11 pass/fail checks")
    print("  python main.py serve        dashboard on http://127.0.0.1:8000\n")
    return 0


async def verify() -> int:
    """Call each configured API once so a bad scope surfaces now, not on stage."""
    import httpx

    from config import Settings

    settings = Settings()
    results: list[tuple[str, bool, str]] = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        if settings.pagerduty.live:
            try:
                # /users/me only resolves for a user-level token. Account-level
                # (API key) tokens have no associated user, so that endpoint always
                # 400s for them even when the token is perfectly valid. /abilities
                # authenticates the same way for both token types without needing
                # a user identity, so it is the right endpoint to prove the key works.
                r = await client.get(
                    "https://api.pagerduty.com/abilities",
                    headers={
                        "Authorization": f"Token token={settings.pagerduty.required['PAGERDUTY_API_KEY']}",
                        "Accept": "application/vnd.pagerduty+json;version=2",
                    },
                )
                ok = r.status_code == 200
                detail = (
                    f"{len(r.json().get('abilities', []))} abilities visible to this token"
                    if ok
                    else r.text[:150]
                )
                results.append(("PagerDuty", ok, detail))
            except Exception as exc:
                results.append(("PagerDuty", False, str(exc)[:120]))

        if settings.datadog.live:
            try:
                r = await client.get(
                    f"https://api.{settings.dd_site}/api/v1/validate",
                    headers={
                        "DD-API-KEY": settings.datadog.required["DD_API_KEY"],
                        "DD-APPLICATION-KEY": settings.datadog.required["DD_APP_KEY"],
                    },
                )
                detail = "API key valid" if r.status_code == 200 else r.text[:200]
                results.append(("Datadog", r.status_code == 200, detail))
            except Exception as exc:
                results.append(("Datadog", False, str(exc)[:120]))

        if settings.github.live:
            try:
                r = await client.get(
                    f"https://api.github.com/repos/{settings.github_repo}",
                    headers={
                        "Authorization": f"Bearer {settings.github.required['GITHUB_TOKEN']}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                ok = r.status_code == 200
                detail = r.json().get("full_name", "") if ok else r.text[:120]
                results.append(("GitHub", ok, detail))
            except Exception as exc:
                results.append(("GitHub", False, str(exc)[:120]))

        if settings.slack.live:
            try:
                r = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={
                        "Authorization": f"Bearer {settings.slack.required['SLACK_BOT_TOKEN']}"
                    },
                )
                data = r.json()
                results.append(
                    ("Slack", bool(data.get("ok")), data.get("team", "") or data.get("error", ""))
                )
            except Exception as exc:
                results.append(("Slack", False, str(exc)[:120]))

    if settings.llm_mode != "mock":
        try:
            from LLM_Providers import get_provider

            provider = get_provider()
            text = await provider.complete("Reply with the single word OK.", "ping")
            results.append((f"LLM ({provider.name})", bool(text), (text or "")[:60]))
        except Exception as exc:
            detail = str(exc)[:150]
            # A 404 on a chat completion almost always means the model name is
            # stale, not that the key is bad — list what the key can actually see.
            if settings.llm_mode == "groq" and "does not exist" in detail:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        r = await client.get(
                            "https://api.groq.com/openai/v1/models",
                            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                        )
                    ids = [m["id"] for m in r.json().get("data", [])]
                    detail += f" | available to your key: {', '.join(ids[:8])}"
                except Exception:
                    pass
            results.append((f"LLM ({settings.llm_mode})", False, detail))

    print("\nCredential verification\n" + "=" * 70)
    if not results:
        print("  Nothing configured yet — everything runs on demo fixtures.")
        print("  Run `python setup.py` to add keys.\n")
        return 0
    failed = 0
    for name, ok, detail in results:
        if not ok:
            failed += 1
        print(f"  {name:<18} {'[OK]  ' if ok else '[FAIL]'} {detail}")
    print("=" * 70)
    print(f"  {len(results) - failed}/{len(results)} verified\n")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report status, change nothing")
    parser.add_argument("--verify", action="store_true", help="call each configured API")
    args = parser.parse_args()

    if args.check:
        return report()
    if args.verify:
        return asyncio.run(verify())
    return wizard()


if __name__ == "__main__":
    sys.exit(main())
