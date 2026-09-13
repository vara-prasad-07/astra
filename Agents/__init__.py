"""The NIGHTWATCH swarm roster."""

from .base import Agent
from .code_detective import CodeDetective
from .commander import Commander
from .correlation import CorrelationAgent
from .log_hunter import LogHunter
from .metrics_agent import MetricsAgent
from .notifier import SlackNotifier
from .operator import Operator
from .sentinel import Sentinel
from .timeline import Timeline

ROSTER = [
    ("Incident Investigator", "Sentinel", "PagerDuty"),
    ("Log Detective", "Log Hunter", "Datadog"),
    ("Code Archaeologist", "Code Detective", "GitHub"),
    ("Metrics Analyst", "Metrics", "Datadog"),
    ("Timeline Detective", "Timeline", "Correlated"),
    ("Correlation Agent", "Correlation", "All agents"),
    ("Incident Commander", "Commander", "Correlation"),
    ("Action Agent", "Operator", "Slack approval"),
]

__all__ = [
    "Agent",
    "CodeDetective",
    "Commander",
    "CorrelationAgent",
    "LogHunter",
    "MetricsAgent",
    "Operator",
    "ROSTER",
    "Sentinel",
    "SlackNotifier",
    "Timeline",
]
