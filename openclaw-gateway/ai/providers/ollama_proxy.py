"""
SKYNET — Ollama Proxy Provider

Routes AI chat requests through the laptop agent's WebSocket connection
to the locally running Ollama server. This makes Ollama appear as a
standard provider to the router, even though it runs on the laptop
(unreachable from EC2 directly).

Flow: ProviderRouter → OllamaProxyProvider → gateway.send_action("ollama_chat")
      → WebSocket → Laptop Agent → http://localhost:11434/api/chat → response
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import BaseProvider, ProviderResponse, ToolCall, QuotaInfo

logger = logging.getLogger("skynet.ai.ollama")


class OllamaProxyProvider(BaseProvider):
    """Proxies AI requests to Ollama running on the laptop via the agent WebSocket."""

    name = "ollama"
    supports_tool_use = True
    context_limit = 32_768     # Model-dependent; conservative default.
    cost_rank = 0              # Free — local inference.
    daily_limit = None         # Unlimited.
    rpm_limit = None           # Limited only by GPU speed.

    def __init__(self, model: str = "qwen2.5-coder:7b"):
        # Still initialize BaseProvider internals (cooldown/error tracking).
        super().__init__(api_key="", model=model)

    @property
    def default_model(self) -> str:
        return "qwen2.5-coder:7b"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ProviderResponse:
        """Route a chat request through the agent to local Ollama."""
        from gateway import send_action, is_agent_connected

        if not is_agent_connected():
            raise RuntimeError("Agent not connected — cannot reach Ollama.")

        # Build the action parameters.
        params: dict[str, Any] = {
            "messages": json.dumps(messages),
            "model": self.model_name,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = json.dumps(tools)

        # Send through the WebSocket to the agent. confirmed=True since
        # ollama_chat is AUTO tier — no terminal prompt needed.
        result = await send_action(
            "ollama_chat",
            params,
            timeout=300,
            confirmed=True,
        )

        # Parse the agent's response.
        status = result.get("status", "error")
        if status == "error":
            error = result.get("error", "Unknown Ollama error")
            raise RuntimeError(f"Ollama error: {error}")

        inner = result.get("result", {})
        returncode = inner.get("returncode", 1)
        stdout = inner.get("stdout", "")
        stderr = inner.get("stderr", "")

        if returncode != 0:
            raise RuntimeError(f"Ollama failed: {stderr}")

        # Parse the JSON response from the agent.
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"Invalid Ollama response JSON: {exc}")

        # Convert to ProviderResponse.
        tool_calls = []
        for tc in data.get("tool_calls", []):
            tool_calls.append(ToolCall(
                id=tc.get("id", f"ollama_{len(tool_calls)}"),
                name=tc.get("name", ""),
                input=tc.get("input", {}),
            ))

        return ProviderResponse(
            text=data.get("text", ""),
            tool_calls=tool_calls,
            stop_reason=data.get("stop_reason", "end_turn"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            provider_name="ollama",
            model=data.get("model", self.model_name),
        )

    def has_quota(self) -> bool:
        """Ollama is always available if the agent is connected."""
        from gateway import is_agent_connected
        return is_agent_connected()

    def remaining_quota(self) -> QuotaInfo:
        return QuotaInfo(
            daily_limit=None,
            daily_used=self._daily_used,
            rpm_limit=None,
            rpm_used=0,
            resets_at=None,
        )
