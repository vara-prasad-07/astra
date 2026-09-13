"""Central configuration for NIGHTWATCH.

Every external system degrades independently: if the credentials for one
integration are absent, that integration serves deterministic demo fixtures
instead. This is what makes the scripted demo in the design record
reproducible without a live production incident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class IntegrationConfig:
    name: str
    required: dict[str, str] = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return all(self.required.values())

    @property
    def missing(self) -> list[str]:
        return [key for key, value in self.required.items() if not value]

    @property
    def mode(self) -> str:
        return "live" if self.live else "demo"


class Settings:
    def __init__(self) -> None:
        self.force_demo = _env("NIGHTWATCH_MODE", "auto").lower() == "demo"

        self.pagerduty = IntegrationConfig(
            "PagerDuty",
            {
                "PAGERDUTY_API_KEY": _env("PAGERDUTY_API_KEY"),
                "PAGERDUTY_FROM_EMAIL": _env("PAGERDUTY_FROM_EMAIL"),
            },
        )
        self.datadog = IntegrationConfig(
            "Datadog",
            {
                "DD_API_KEY": _env("DD_API_KEY"),
                "DD_APP_KEY": _env("DD_APP_KEY"),
            },
        )
        self.github = IntegrationConfig(
            "GitHub",
            {
                "GITHUB_TOKEN": _env("GITHUB_TOKEN"),
                "GITHUB_REPO": _env("GITHUB_REPO"),
            },
        )
        self.slack = IntegrationConfig(
            "Slack",
            {
                "SLACK_BOT_TOKEN": _env("SLACK_BOT_TOKEN"),
                "SLACK_CHANNEL": _env("SLACK_CHANNEL"),
            },
        )
        self.render = IntegrationConfig(
            "Render",
            {
                "RENDER_API_KEY": _env("RENDER_API_KEY"),
                "RENDER_SERVICE_ID": _env("RENDER_SERVICE_ID"),
            },
        )

        self.pagerduty_webhook_secret = _env("PAGERDUTY_WEBHOOK_SECRET")
        self.slack_signing_secret = _env("SLACK_SIGNING_SECRET")

        self.dd_site = _env("DD_SITE", "datadoghq.com")
        self.github_repo = _env("GITHUB_REPO")
        self.slack_channel = _env("SLACK_CHANNEL")
        self.pagerduty_from_email = _env("PAGERDUTY_FROM_EMAIL")

        self.llm_provider = _env("LLM_PROVIDER", "auto").lower()
        self.groq_api_key = _env("GROQ_API_KEY")
        # Groq's catalog turns over; llama-3.3-70b-versatile is gone as of late
        # 2026. gpt-oss-120b is current at time of writing but override via
        # GROQ_MODEL if your key's available models (setup.py --verify prints
        # them on a 404) differ.
        self.groq_model = _env("GROQ_MODEL", "openai/gpt-oss-120b")
        self.gemini_api_key = _env("GEMINI_API_KEY")
        self.gemini_model = _env("GEMINI_MODEL", "gemini-2.0-flash")

        # Only these identities may approve a production action.
        self.approvers = _env_list("NIGHTWATCH_APPROVERS")

        self.db_path = _env("NIGHTWATCH_DB", str(ROOT / "nightwatch.db"))
        self.demo_service_url = _env("DEMO_SERVICE_URL", "http://127.0.0.1:8100")
        self.host = _env("NIGHTWATCH_HOST", "127.0.0.1")
        self.port = int(_env("NIGHTWATCH_PORT", "8000"))

    @property
    def integrations(self) -> list[IntegrationConfig]:
        return [self.pagerduty, self.datadog, self.github, self.slack, self.render]

    def is_live(self, integration: IntegrationConfig) -> bool:
        return integration.live and not self.force_demo

    @property
    def llm_mode(self) -> str:
        if self.llm_provider == "mock":
            return "mock"
        if self.llm_provider == "groq" and self.groq_api_key:
            return "groq"
        if self.llm_provider == "gemini" and self.gemini_api_key:
            return "gemini"
        if self.llm_provider == "auto":
            if self.groq_api_key:
                return "groq"
            if self.gemini_api_key:
                return "gemini"
        return "mock"

    def summary(self) -> list[tuple[str, str, list[str]]]:
        rows = [
            (item.name, "live" if self.is_live(item) else "demo", item.missing)
            for item in self.integrations
        ]
        llm = self.llm_mode
        rows.append(("LLM", llm, [] if llm != "mock" else ["GROQ_API_KEY or GEMINI_API_KEY"]))
        return rows


settings = Settings()
