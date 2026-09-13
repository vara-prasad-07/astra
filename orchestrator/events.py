"""In-process event bus.

Agents publish lifecycle events as they run; the dashboard streams them over SSE
so the parallel investigation is visible rather than asserted.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator


class EventBus:
    def __init__(self, history_size: int = 400) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)

    def publish(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "kind": kind,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **payload,
        }
        self.history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        try:
            for event in list(self.history):
                queue.put_nowait(event)
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def replay(self, incident_id: str | None = None) -> list[dict[str, Any]]:
        events = list(self.history)
        if incident_id:
            events = [e for e in events if e.get("incident_id") == incident_id]
        return events

    def clear(self) -> None:
        self.history.clear()


def to_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


bus = EventBus()
