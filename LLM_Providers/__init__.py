"""Provider registry — one swap point for the whole swarm."""

from __future__ import annotations

from functools import lru_cache

from config import settings

from .base import LLMProvider, MockProvider


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    mode = settings.llm_mode
    if mode == "groq":
        from .Groq import GroqProvider

        provider = GroqProvider()
        if provider.available:
            return provider
    if mode == "gemini":
        from .Gemini import GeminiProvider

        provider = GeminiProvider()
        if provider.available:
            return provider
    return MockProvider()


__all__ = ["LLMProvider", "MockProvider", "get_provider"]
