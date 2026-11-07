"""OpenAICompatibleLLM: chat completions over httpx against the configured endpoint only.

Deliberately not the OpenAI SDK: the SDK falls back to the vendor's hosted endpoint when no base URL is given, which
is exactly the silent hosted path CLAUDE.md rule 2 forbids. This client has no default endpoint; the
base_url comes from config, which validation restricts to allowed (loopback / bank) hosts.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

import httpx

from kb.ports.types import ChatMessage

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


class OpenAICompatibleLLM:
    STATUS = "implemented"
    PHASE = 3

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 180.0,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int = 700,
        api_key: str = "local-no-key",  # Ollama/vLLM local endpoints ignore it; never a real cloud key
        **_: Any,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an explicit http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = dict(extra_body or {})
        self.default_max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"})
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: int = 0

    def chat(self, messages: Sequence[ChatMessage], *, temperature: float = 0.0, max_tokens: int | None = None) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": False,
            **self.extra_body,
        }
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"LLM call failed: {e}") from e
        self.last_latency_ms = int((time.monotonic() - t0) * 1000)
        data = r.json()
        self.last_usage = data.get("usage") or {}
        content = data["choices"][0]["message"].get("content") or ""
        return THINK_BLOCK.sub("", content).strip()

    def ping(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/models", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False
