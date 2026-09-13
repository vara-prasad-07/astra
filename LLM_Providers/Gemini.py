"""Gemini provider — secondary/fallback model behind the same interface."""

from __future__ import annotations

import asyncio

from config import settings

from .base import LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        self.model = settings.gemini_model
        self._client = None
        self.available = False
        if not settings.gemini_api_key:
            return
        try:
            from google import genai

            self._client = genai.Client(api_key=settings.gemini_api_key)
            self.available = True
        except Exception:
            self.available = False

    def _blocking_complete(self, system: str, prompt: str) -> str:
        response = self._client.models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{prompt}",
        )
        return getattr(response, "text", "") or ""

    async def complete(self, system: str, prompt: str) -> str:
        if not self.available:
            return ""
        return await asyncio.wait_for(
            asyncio.to_thread(self._blocking_complete, system, prompt), timeout=30
        )
