"""Thin wrapper around the Anthropic Messages API (swappable in tests)."""

from __future__ import annotations

from typing import Any, Protocol

import anthropic


class LLM(Protocol):
    def create(self, system: str, tools: list[dict], messages: list[dict]) -> Any: ...


class AnthropicLLM:
    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 16000):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def create(self, system: str, tools: list[dict], messages: list[dict]):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            # One action per step; "auto" because forced tool_choice is rejected on current models.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": self.effort},
            # Append-only history + automatic caching keeps long runs cheap.
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry server-side on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
