"""Ships this service's logs straight to Datadog over HTTPS — no Agent required.

Render doesn't run a Datadog Agent sidecar, so instead of APM/StatsD this posts
each log record directly to Datadog's HTTP log intake. Activated only when
DD_API_KEY is set; a background thread does the sending so a slow or
unreachable intake endpoint never blocks a request.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.request
from typing import Any

_QUEUE: "queue.Queue[dict[str, Any]] | None" = None


class DatadogLogHandler(logging.Handler):
    def __init__(self, service: str, api_key: str, site: str, tags: str) -> None:
        super().__init__()
        self._url = f"https://http-intake.logs.{site}/api/v2/logs"
        self._api_key = api_key
        self._service = service
        self._tags = tags
        self._hostname = os.environ.get("RENDER_SERVICE_NAME") or os.environ.get(
            "HOSTNAME", "local"
        )

    def emit(self, record: logging.LogRecord) -> None:
        if _QUEUE is None:
            return
        try:
            body = json.dumps(
                [
                    {
                        "ddsource": "python",
                        "service": self._service,
                        "ddtags": self._tags,
                        "hostname": self._hostname,
                        "message": self.format(record),
                        "status": record.levelname.lower(),
                    }
                ]
            ).encode("utf-8")
            _QUEUE.put_nowait({"body": body})
        except Exception:
            pass  # shipping logs must never break the request path

    def _post(self, body: bytes) -> None:
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "DD-API-KEY": self._api_key},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)


def _worker(handler: DatadogLogHandler) -> None:
    assert _QUEUE is not None
    while True:
        item = _QUEUE.get()
        try:
            handler._post(item["body"])
        except Exception:
            pass


def install(logger: logging.Logger) -> bool:
    """Attach the Datadog handler if DD_API_KEY is present. Returns whether it did."""
    api_key = os.environ.get("DD_API_KEY", "").strip()
    if not api_key:
        return False

    global _QUEUE
    _QUEUE = queue.Queue(maxsize=1000)

    site = os.environ.get("DD_SITE", "datadoghq.com").strip()
    service = os.environ.get("DD_SERVICE", "payment-service").strip()
    env = os.environ.get("DD_ENV", "production").strip()
    version = os.environ.get("DD_VERSION", "").strip()
    tags = f"env:{env}" + (f",version:{version}" if version else "")

    handler = DatadogLogHandler(service, api_key, site, tags)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    threading.Thread(target=_worker, args=(handler,), daemon=True).start()
    return True
