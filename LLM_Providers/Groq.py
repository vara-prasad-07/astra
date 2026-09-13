"""Groq provider — fast inference, which keeps the live demo snappy."""

from __future__ import annotations

import asyncio

from config import settings

from .base import LLMProvider


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self) -> None:
        self.model = settings.groq_model
        self._client = None
        self.available = False
        if not settings.groq_api_key:
            return
        try:
            from groq import Groq

            self._client = Groq(api_key=settings.groq_api_key)
            self.available = True
        except Exception:
            self.available = False

    def _blocking_complete(self, system: str, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        return response.choices[0].message.content or ""

    async def complete(self, system: str, prompt: str) -> str:
        if not self.available:
            return ""
        return await asyncio.wait_for(
            asyncio.to_thread(self._blocking_complete, system, prompt), timeout=30
        )
