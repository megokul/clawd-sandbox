"""
SKYNET — Multi-Provider AI Router

Automatically selects the best available free-tier AI provider for
each request.  Tracks per-provider usage, rotates on quota exhaustion,
and retries with fallback providers on errors.

Priority order (configurable):
  1. Gemini Flash  — biggest free tier (1M tokens/day)
  2. Groq           — fast, 14.4K req/day free
  3. OpenRouter     — various free models
  4. DeepSeek       — near-free
  5. OpenAI         — if credits available
  6. Claude         — if credits available
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from .providers.base import BaseProvider, ProviderResponse
from db import store

logger = logging.getLogger("skynet.ai.router")


class ProviderRouter:
    """Routes AI requests to the best available free-tier provider."""

    def __init__(self, providers: list[BaseProvider], db: aiosqlite.Connection):
        self.providers = providers
        self.db = db
        self._error_counts: dict[str, int] = {}

    async def restore_usage(self) -> None:
        """Load today's usage counters from the database."""
        for provider in self.providers:
            usage = await store.get_provider_usage(self.db, provider.name)
            if usage:
                provider.load_usage_from_db(usage["requests_used"], usage["date"])
                logger.info(
                    "Restored %s usage: %d requests today",
                    provider.name, usage["requests_used"],
                )

    # Task-type → preferred provider names.
    TASK_PROVIDER_PREFERENCES: dict[str, list[str]] = {
        "scaffold": ["ollama"],
        "crud": ["ollama"],
        "boilerplate": ["ollama"],
        "fast_patch": ["groq", "ollama"],
        "unit_test": ["groq", "ollama"],
        "planning": ["gemini", "ollama"],
        "readme_polish": ["gemini"],
        "hard_debug": ["deepseek", "claude"],
        "complex_refactor": ["ollama", "claude"],
        "general": [],  # default priority order
        # v3: Agent-role routing
        "agent_architect": ["gemini", "claude"],
        "agent_backend": ["ollama", "groq"],
        "agent_frontend": ["ollama", "groq"],
        "agent_api": ["ollama", "groq"],
        "agent_testing": ["groq", "ollama"],
        "agent_debug": ["deepseek", "claude"],
        "agent_devops": ["ollama", "groq"],
        "agent_research": ["gemini"],
        "agent_optimization": ["deepseek", "claude"],
        "agent_deployment": ["ollama", "groq"],
        "agent_monitoring": ["ollama", "groq"],
    }

    def _ranked_providers(
        self,
        require_tools: bool = False,
        task_type: str = "general",
        preferred_provider: str | None = None,
        preferred_provider_only: bool = False,
    ) -> list[BaseProvider]:
        """
        Return providers sorted by task-aware priority, filtered by
        availability, cooldown, and quota.
        """
        preferences = self.TASK_PROVIDER_PREFERENCES.get(task_type, [])
        scored: list[tuple[BaseProvider, int]] = []

        for p in self.providers:
            if preferred_provider_only and preferred_provider and p.name != preferred_provider:
                continue
            if require_tools and not p.supports_tool_use:
                continue
            if not p.has_quota():
                continue
            if p.is_in_cooldown():
                continue

            # Build a priority score (lower = better).
            if preferred_provider and p.name == preferred_provider:
                score = -1  # Highest priority.
            elif p.name in preferences:
                score = preferences.index(p.name)
            elif p.is_deprioritized():
                score = 100 + p.cost_rank
            else:
                score = 50 + p.cost_rank

            scored.append((p, score))

        scored.sort(key=lambda x: x[1])
        return [p for p, _ in scored]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        require_tools: bool = False,
        task_type: str = "general",
        preferred_provider: str | None = None,
        preferred_provider_only: bool = False,
    ) -> ProviderResponse:
        """
        Send a chat request to the best available provider.

        Selects provider based on task_type routing, quota availability,
        cooldown state, and error history. Falls back through all candidates.
        """
        candidates = self._ranked_providers(
            require_tools=require_tools or bool(tools),
            task_type=task_type,
            preferred_provider=preferred_provider,
            preferred_provider_only=preferred_provider_only,
        )
        if not candidates:
            raise RuntimeError(
                "No AI providers available — all quotas exhausted or in cooldown. "
                "Quotas reset daily; try again later or add more API keys."
            )

        last_error: Exception | None = None
        for provider in candidates:
            try:
                logger.info(
                    "Trying provider: %s (%s) for task_type=%s",
                    provider.name, provider.model_name, task_type,
                )
                response = await provider.chat(
                    messages, tools=tools, system=system, max_tokens=max_tokens,
                )
                # Success — record usage and clear errors.
                provider.record_usage(response.input_tokens + response.output_tokens)
                await store.record_provider_usage(
                    self.db,
                    provider.name,
                    requests=1,
                    tokens=response.input_tokens + response.output_tokens,
                )
                self._error_counts[provider.name] = 0
                return response

            except Exception as exc:
                error_str = str(exc)
                logger.warning(
                    "Provider %s failed: %s — trying next", provider.name, error_str,
                )
                provider.record_error()
                self._error_counts[provider.name] = self._error_counts.get(provider.name, 0) + 1

                # Detect rate limiting (429) and enter cooldown after 2 hits.
                if "429" in error_str or "rate" in error_str.lower():
                    rate_key = f"_429_{provider.name}"
                    count = self._error_counts.get(rate_key, 0) + 1
                    self._error_counts[rate_key] = count
                    if count >= 2:
                        provider.enter_cooldown(300)
                        logger.warning(
                            "Provider %s entering 5-min cooldown after 2x rate limit",
                            provider.name,
                        )

                await store.record_provider_usage(
                    self.db, provider.name, requests=0, tokens=0, error=True,
                )
                last_error = exc
                continue

        raise RuntimeError(
            f"All AI providers failed. Last error: {last_error}"
        )

    async def get_quota_summary(self) -> list[dict[str, Any]]:
        """Return quota info for all providers (for /quota command)."""
        summary = []
        for p in self.providers:
            q = p.remaining_quota()
            summary.append({
                "provider": p.name,
                "model": p.model_name,
                "daily_used": q.daily_used,
                "daily_limit": q.daily_limit,
                "rpm_used": q.rpm_used,
                "rpm_limit": q.rpm_limit,
                "available": p.has_quota(),
            })
        return summary


def build_providers(config: dict[str, str]) -> list[BaseProvider]:
    """
    Instantiate all available providers from config/env vars.

    Only providers with valid API keys are included.
    Ollama is always registered (availability depends on agent connection).
    """
    from .providers.gemini import GeminiProvider
    from .providers.groq import GroqProvider
    from .providers.openai_compat import OpenAICompatProvider
    from .providers.ollama_proxy import OllamaProxyProvider

    providers: list[BaseProvider] = []

    # 0. Ollama — primary (zero cost, runs on laptop)
    ollama_model = config.get("OLLAMA_DEFAULT_MODEL", "qwen2.5-coder:7b")
    providers.append(OllamaProxyProvider(model=ollama_model))
    logger.info("Registered provider: Ollama (model=%s)", ollama_model)

    # 1. Gemini — secondary (biggest free cloud tier)
    if config.get("GOOGLE_AI_API_KEY"):
        providers.append(GeminiProvider(config["GOOGLE_AI_API_KEY"]))
        logger.info("Registered provider: Gemini Flash")

    # 2. Groq — fast secondary
    if config.get("GROQ_API_KEY"):
        providers.append(GroqProvider(config["GROQ_API_KEY"]))
        logger.info("Registered provider: Groq")

    # 3. OpenRouter — free models
    if config.get("OPENROUTER_API_KEY"):
        providers.append(OpenAICompatProvider(
            api_key=config["OPENROUTER_API_KEY"],
            model="google/gemini-2.0-flash-exp:free",
            base_url="https://openrouter.ai/api/v1",
            provider_name="openrouter",
            daily_limit_override=200,
            rpm_limit_override=20,
            context_limit_override=1_000_000,
            cost_rank_override=2,
        ))
        logger.info("Registered provider: OpenRouter")

    # 4. DeepSeek — near-free
    if config.get("DEEPSEEK_API_KEY"):
        providers.append(OpenAICompatProvider(
            api_key=config["DEEPSEEK_API_KEY"],
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            provider_name="deepseek",
            daily_limit_override=500,
            rpm_limit_override=10,
            context_limit_override=64_000,
            cost_rank_override=3,
        ))
        logger.info("Registered provider: DeepSeek")

    # 5. OpenAI — if credits available
    if config.get("OPENAI_API_KEY"):
        providers.append(OpenAICompatProvider(
            api_key=config["OPENAI_API_KEY"],
            model="gpt-4o-mini",
            provider_name="openai",
            daily_limit_override=200,
            rpm_limit_override=10,
            context_limit_override=128_000,
            cost_rank_override=8,
        ))
        logger.info("Registered provider: OpenAI")

    # 6. Claude — if credits available
    if config.get("ANTHROPIC_API_KEY"):
        try:
            from .providers.anthropic_ai import AnthropicProvider
            providers.append(AnthropicProvider(config["ANTHROPIC_API_KEY"]))
            logger.info("Registered provider: Claude")
        except ImportError:
            logger.warning("anthropic package not installed, skipping Claude provider")

    if not providers:
        logger.error(
            "No AI providers configured! Set at least GOOGLE_AI_API_KEY "
            "in environment variables."
        )

    return providers
