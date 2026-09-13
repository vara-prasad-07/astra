"""Thin provider-agnostic LLM interface.

The design record is explicit that the model is an implementation detail: agents
talk to `LLMProvider.analyze(...)` and never to a vendor SDK. The differentiator
is the swarm architecture, not the model, so every agent also carries a
deterministic fallback and stays correct when no model is reachable.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response, tolerating code fences."""
    if not text:
        return None
    candidates = _FENCE.findall(text)
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LLMProvider(ABC):
    name: str = "base"
    available: bool = False

    @abstractmethod
    async def complete(self, system: str, prompt: str) -> str:
        """Return raw text for a single turn."""

    async def analyze(
        self, system: str, prompt: str, fallback: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask for a JSON object; never raise, fall back to deterministic output."""
        fallback = fallback or {}
        if not self.available:
            return dict(fallback)
        try:
            raw = await self.complete(
                system + "\n\nRespond with a single JSON object and nothing else.",
                prompt,
            )
        except Exception as exc:  # network/quota/auth — demo must not die here
            result = dict(fallback)
            result["_llm_error"] = str(exc)[:200]
            return result
        parsed = extract_json(raw)
        if parsed is None:
            return dict(fallback)
        merged = dict(fallback)
        merged.update(parsed)
        return merged


class MockProvider(LLMProvider):
    """Offline provider. Agents supply deterministic fallbacks, so this returns
    them unchanged — the swarm produces identical output with or without a key."""

    name = "mock"
    available = False

    async def complete(self, system: str, prompt: str) -> str:
        return ""
