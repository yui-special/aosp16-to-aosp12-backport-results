"""Pluggable LLM access.  Business code never names a concrete model."""

from __future__ import annotations

from pathlib import Path

from .base import LLMClient, LLMResponse, load_prompt
from .mock import HeuristicMockClient, MockLLMClient, heuristic_responder
from .provider import OpenAICompatibleClient

__all__ = [
    "LLMClient",
    "LLMResponse",
    "load_prompt",
    "MockLLMClient",
    "HeuristicMockClient",
    "heuristic_responder",
    "OpenAICompatibleClient",
    "build_llm_client",
]

PROVIDER_KINDS = ("mock", "mock-heuristic", "heuristic-mock", "openai", "openai_compatible")


def build_llm_client(
    provider: str | None,
    *,
    provider_config: dict | None = None,
    responses: list | None = None,
    responder=None,
) -> LLMClient:
    """Create the configured client.

    ``provider`` is a logical name from ``configs/models.yaml`` so that switching
    models never requires touching the analysis code.
    """
    config = dict(provider_config or {})
    kind = str(config.get("kind") or provider or "mock").lower()
    name = str(provider or kind)

    if kind in ("mock",):
        return MockLLMClient(responses=responses, responder=responder, model=name)
    if kind in ("heuristic-mock", "mock-heuristic", "heuristic"):
        return HeuristicMockClient(model=name or "mock-heuristic")
    if kind in ("openai", "openai_compatible", "openai-compatible"):
        from .provider import ProviderSettings

        settings = ProviderSettings.from_dict(config)
        return OpenAICompatibleClient(settings)
    raise ValueError(
        f"unknown LLM provider {provider!r} (kind={kind!r}); expected one of {PROVIDER_KINDS}"
    )


def prompt_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "prompts"
